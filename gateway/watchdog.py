#!/usr/bin/env python3
"""
watchdog.py — watchdog LLM-движка на CONTROL-NODE (наша виртуалка).

Ходит по HTTP к watchdog-agent на GPU-ноде, анализирует срезы, ведёт cooldown
в канбан-БД (gateway его читает перед запуском воркеров) и запрашивает рестарт
при систематических hang'ах.

Логику детекции:
  A. engine_down     — /v1/models не отвечает (проба 5с)
  B. engine_stuck    — запросы в работе, но 0 gen-токенов дольше STUCK_WINDOW
  C. kv_thrash       — KV > 95% и preemption'ы растут (предвестник деградации)

ENV:
  WATCHDOG_AGENT_URL   http://<gpu-node>:9100 (ОБЯЗАТЕЛЬНО)
  WATCHDOG_TOKEN       токен агента (для /restart)
  PIPELINE_DB          путь к канбан-БД (default ~/agent-pipeline/kanban/boards/main.sqlite3)
  POLL_SEC             период опроса (default 15)
  STUCK_WINDOW_SEC     сколько секунд нулевых gen-токенов считать hang (default 120)
  HANG_COOLDOWN_SEC    cooldown после единичного hang (default 60)
  RESTART_AFTER_HANGS  сколько hang'ов за час до рестарта (default 2)
  RESTART_COOLDOWN_SEC cooldown после рестарта (default 180)
  ALERT_WEBHOOK        (опц.) URL для POST-алертов JSON
"""
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

AGENT_URL = os.environ.get("WATCHDOG_AGENT_URL", "").rstrip("/")
TOKEN = os.environ.get("WATCHDOG_TOKEN", "")
DB = os.environ.get("PIPELINE_DB",
                    os.path.expanduser("~/agent-pipeline/kanban/boards/main.sqlite3"))
LOG_PATH = os.environ.get("PIPELINE_WATCHDOG_LOG",
                          os.path.expanduser("~/agent-pipeline/watchdog.log"))
POLL_SEC = int(os.environ.get("POLL_SEC", "15"))
STUCK_WINDOW = int(os.environ.get("STUCK_WINDOW_SEC", "120"))
HANG_CD = int(os.environ.get("HANG_COOLDOWN_SEC", "60"))
RESTART_AFTER = int(os.environ.get("RESTART_AFTER_HANGS", "2"))
RESTART_CD = int(os.environ.get("RESTART_COOLDOWN_SEC", "180"))
WEBHOOK = os.environ.get("ALERT_WEBHOOK", "")

ENGINE_KEY = "engine"
_alerted_states = {}  # state -> ts последнего алерта (анти-spam: 10 мин)


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt=None):
    return (dt or now_utc()).strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    line = f"[{iso()}] [watchdog] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def con():
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def set_cooldown(c, key, reason, secs):
    until = iso(now_utc() + timedelta(seconds=secs))
    c.execute("INSERT OR REPLACE INTO cooldowns(key, reason, until) VALUES(?,?,?)",
              (key, reason, until))
    c.commit()
    log(f"COOLDOWN {key}: {reason} (до {until})")


def clear_expired(c):
    gone = c.execute("DELETE FROM cooldowns WHERE until <= ?", (iso(),)).rowcount
    if gone:
        c.commit()
        log(f"снято просроченных cooldown'ов: {gone}")


def engine_cooled_down(c):
    row = c.execute("SELECT reason, until FROM cooldowns WHERE key=? AND until > ?",
                    (ENGINE_KEY, iso())).fetchone()
    return row["reason"] if row else None


def fetch_snapshot():
    try:
        req = urllib.request.Request(AGENT_URL + "/metrics")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"_error": str(e)}


def request_restart():
    try:
        req = urllib.request.Request(AGENT_URL + "/restart", method="POST",
                                     headers={"Authorization": f"Bearer {TOKEN}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
            return bool(data.get("restarted")), data.get("error", "")
    except urllib.error.HTTPError as e:
        return False, f"http {e.code}"
    except Exception as e:
        return False, str(e)


def alert(state, detail):
    last = _alerted_states.get(state, 0)
    if time.time() - last < 600:
        return
    _alerted_states[state] = time.time()
    msg = f"⚠️ vLLM watchdog: {state} — {detail}"
    log("ALERT: " + msg)
    if WEBHOOK:
        try:
            payload = json.dumps({"text": msg}).encode()
            req = urllib.request.Request(WEBHOOK, data=payload,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            log(f"webhook error: {e}")


def hang_count_recent(c, hours=1):
    cutoff = iso(now_utc() - timedelta(hours=hours))
    row = c.execute("SELECT COUNT(*) n FROM hang_log WHERE ts > ? AND action IN "
                    "('engine_down','engine_stuck')", (cutoff,)).fetchone()
    return row["n"] if row else 0


def log_hang(c, action, snap):
    c.execute("INSERT INTO hang_log(ts, action, gpu_util, kv_cache_pct, gen_tokens_60s) "
              "VALUES(?,?,?,?,?)",
              (iso(), action,
               snap.get("gpus", [{}])[0].get("util") if snap.get("gpus") else None,
               snap.get("vllm", {}).get("kv_cache_pct"),
               snap.get("vllm", {}).get("gen_tokens_60s")))
    c.commit()


def iterate(c):
    clear_expired(c)
    snap = fetch_snapshot()

    # --- случай 0: сам агент недоступен ---
    if "_error" in snap:
        if not engine_cooled_down(c):
            set_cooldown(c, ENGINE_KEY, f"agent_unreachable: {snap['_error'][:80]}", 30)
            alert("agent_unreachable", snap["_error"][:120])
        return

    v = snap.get("vllm", {})
    eng = snap.get("engine", {})
    gpus = snap.get("gpus", [])
    util = gpus[0]["util"] if gpus else None
    kv = v.get("kv_cache_pct")
    running = v.get("running", 0)
    gen60 = v.get("gen_tokens_60s")

    # --- случай A: движок не отвечает ---
    if not eng.get("healthy"):
        if not engine_cooled_down(c):
            log_hang(c, "engine_down", snap)
            set_cooldown(c, ENGINE_KEY, "engine_down", HANG_CD)
            alert("engine_down", f"vLLM не отвечает (util={util}%)")
            if hang_count_recent(c) >= RESTART_AFTER:
                ok, err = request_restart()
                log(f"restart запрошен (down): ok={ok} err={err}")
                if ok:
                    set_cooldown(c, ENGINE_KEY, "restarting", RESTART_CD)
        return

    # --- случай B: застрял (запросы есть, токенов нет) ---
    if running > 0 and gen60 is not None and gen60 == 0:
        stuck_since = getattr(iterate, "_stuck_since", None)
        if stuck_since is None:
            iterate._stuck_since = time.time()
            log(f"возможный stuck: running={running}, gen60=0 (наблюдаю)")
        elif time.time() - stuck_since > STUCK_WINDOW:
            if not engine_cooled_down(c):
                log_hang(c, "engine_stuck", snap)
                set_cooldown(c, ENGINE_KEY,
                             f"engine_stuck: {running} req, 0 tok/{STUCK_WINDOW}s", HANG_CD)
                alert("engine_stuck", f"{running} запросов без генерации >{STUCK_WINDOW}s")
                if hang_count_recent(c) >= RESTART_AFTER:
                    ok, err = request_restart()
                    log(f"restart запрошен (stuck): ok={ok} err={err}")
                    if ok:
                        set_cooldown(c, ENGINE_KEY, "restarting", RESTART_CD)
            iterate._stuck_since = time.time()  # не спамим повторно
    else:
        iterate._stuck_since = None

    # --- случай C: KV-трэш (предвестник) ---
    if kv is not None and kv > 95 and (v.get("preemptions_60s") or 0) > 0:
        alert("kv_thrash", f"KV={kv:.0f}%, preemptions_60s={v.get('preemptions_60s')}")

    # --- норма: heartbeat раз в 10 мин ---
    hb = getattr(iterate, "_hb", 0)
    if time.time() - hb > 600:
        iterate._hb = time.time()
        log(f"heartbeat: util={util}% kv={kv if kv is not None else '?'}% "
            f"running={running} waiting={v.get('waiting', 0)} gen60={gen60}")


def main():
    if not AGENT_URL:
        raise SystemExit("WATCHDOG_AGENT_URL не задан")
    log(f"start: agent={AGENT_URL} poll={POLL_SEC}s stuck_window={STUCK_WINDOW}s "
        f"restart_after={RESTART_AFTER}")
    while True:
        c = con()
        try:
            iterate(c)
        except Exception as e:
            log(f"[error] {repr(e)}")
        finally:
            c.close()
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()