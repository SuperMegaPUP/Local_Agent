#!/usr/bin/env python3
"""
watchdog-agent.py — сенсорный узел на GPU-ноде (рядом с vLLM).

Отдаёт агрегированные метрики движка и выполняет рестарт по команде.
Защищён bearer-токеном. Только чтение метрик — без права на внешнюю сеть.

ENV:
  WATCHDOG_AGENT_PORT      порт HTTP (default 9100)
  WATCHDOG_TOKEN           токен для /restart (ОБЯЗАТЕЛЬНО задать)
  PIPELINE_ENGINE_URL      адрес vLLM на этой ноде (default http://127.0.0.1:8000)
  PIPELINE_ENGINE_METRICS  адрес /metrics vLLM (default: ENGINE_URL + /metrics)
  PIPELINE_ENGINE_LOG      файл лога vLLM (для возраста записи; пусто = не следим)
  PIPELINE_ENGINE_RESTART  systemctl | docker | kill_pid (default systemctl)
  PIPELINE_ENGINE_UNIT     имя юнита (default vllm)
  PIPELINE_ENGINE_CONTAINER имя контейнера (для docker)
  PIPELINE_ENGINE_PIDFILE  путь к pid-файлу (для kill_pid)
  RESTART_MIN_INTERVAL_SEC минимум между рестартами (default 300)
"""
import hashlib
import hmac
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("WATCHDOG_AGENT_PORT", "9100"))
TOKEN = os.environ.get("WATCHDOG_TOKEN", "")
ENGINE_URL = os.environ.get("PIPELINE_ENGINE_URL", "http://127.0.0.1:8000")
METRICS_URL = os.environ.get("PIPELINE_ENGINE_METRICS", ENGINE_URL.rstrip("/") + "/metrics")
ENGINE_LOG = os.environ.get("PIPELINE_ENGINE_LOG", "")
RESTART_METHOD = os.environ.get("PIPELINE_ENGINE_RESTART", "systemctl")
UNIT = os.environ.get("PIPELINE_ENGINE_UNIT", "vllm")
CONTAINER = os.environ.get("PIPELINE_ENGINE_CONTAINER", "")
PIDFILE = os.environ.get("PIPELINE_ENGINE_PIDFILE", "")
MIN_RESTART_GAP = float(os.environ.get("RESTART_MIN_INTERVAL_SEC", "300"))


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    print(f"[{now_iso()}] [agent] {msg}", flush=True)


# ---------- сенсоры ----------

def gpu_metrics():
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        gpus = []
        for line in r.stdout.strip().splitlines():
            idx, name, util, mem_u, mem_t = [x.strip() for x in line.split(",")]
            gpus.append({"index": int(idx), "name": name, "util": int(util),
                         "mem_used_mib": int(mem_u), "mem_total_mib": int(mem_t)})
        return gpus
    except Exception as e:
        log(f"gpu_metrics error: {e}")
        return []


_counter_lock = threading.Lock()
_counters_prev = {}  # name -> (value, ts)


_WANTED = ("vllm:generation_tokens_total", "vllm:num_requests_running",
           "vllm:num_requests_waiting", "vllm:kv_cache_usage_perc",
           "vllm:num_preemptions_total", "vllm:request_success_total",
           "vllm:time_to_first_token_seconds_count")
_GAUGES = {"vllm:kv_cache_usage_perc", "vllm:num_requests_running",
           "vllm:num_requests_waiting"}  # гауги берём max, счётчики — sum


def scrape_vllm():
    """Читает /metrics vLLM, возвращает агрегированные значения ключевых метрик.
    Строки могут иметь лейблы (engine/model_name) — агрегируем по всем инстансам."""
    try:
        r = subprocess.run(["curl", "-s", "-m", "8", METRICS_URL],
                           capture_output=True, text=True, timeout=10)
        acc = {}
        for line in r.stdout.splitlines():
            if line.startswith("#"):
                continue
            head = line.split("{", 1)[0]
            if head not in _WANTED:
                continue
            try:
                val = float(line.rsplit(" ", 1)[1])
            except (ValueError, IndexError):
                continue
            if head in _GAUGES:
                acc[head] = max(acc.get(head, 0.0), val)
            else:
                acc[head] = acc.get(head, 0.0) + val
        return acc
    except Exception as e:
        log(f"scrape_vllm error: {e}")
        return None


def delta_since(prev_val, prev_ts, cur_val, window=60.0):
    """Дельта счётчика за window секунд (None если данных мало)."""
    if prev_val is None or prev_ts is None:
        return None
    dt = time.time() - prev_ts
    if dt < min(window, 15):
        return None
    return max(0.0, cur_val - prev_val)


def collect():
    """Собирает полный срез: GPU + vLLM + дельты + здоровье."""
    gpus = gpu_metrics()
    cur = scrape_vllm()
    out = {"ts": now_iso(), "gpus": gpus, "engine": {}, "vllm": {}}

    # здоровье движка: /v1/models отвечает быстро
    eng_ok = False
    tt = time.time()
    try:
        r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                            "-m", "5", ENGINE_URL.rstrip("/") + "/v1/models"],
                           capture_output=True, text=True, timeout=8)
        eng_ok = r.returncode == 0 and r.stdout.strip() == "200"
    except Exception:
        pass
    out["engine"]["healthy"] = eng_ok
    out["engine"]["probe_ms"] = int((time.time() - tt) * 1000)

    if ENGINE_LOG and os.path.exists(ENGINE_LOG):
        try:
            out["engine"]["log_age_sec"] = int(time.time() - os.stat(ENGINE_LOG).st_mtime)
        except OSError:
            out["engine"]["log_age_sec"] = -1
    else:
        out["engine"]["log_age_sec"] = -1

    if cur is None:
        out["vllm"]["reachable"] = False
        return out
    out["vllm"]["reachable"] = True

    with _counter_lock:
        prev = dict(_counters_prev)
        for k, v in cur.items():
            _counters_prev[k] = (v, time.time())

    gt_key = "vllm:generation_tokens_total"
    pt_key = "vllm:num_preemptions_total"
    st_key = "vllm:request_success_total"
    out["vllm"].update({
        "gen_tokens_60s": delta_since(*prev.get(gt_key, (None, None)), cur.get(gt_key)),
        "preemptions_60s": delta_since(*prev.get(pt_key, (None, None)), cur.get(pt_key)),
        "requests_done_60s": delta_since(*prev.get(st_key, (None, None)), cur.get(st_key)),
        "running": int(cur.get("vllm:num_requests_running", 0)),
        "waiting": int(cur.get("vllm:num_requests_waiting", 0)),
        "kv_cache_pct": cur.get("vllm:kv_cache_usage_perc"),
    })
    return out


# ---------- актуатор ----------

_last_restart = 0.0


def do_restart():
    global _last_restart
    if not TOKEN:
        return False, "token not configured"
    if time.time() - _last_restart < MIN_RESTART_GAP:
        return False, f"too soon (gap<{int(MIN_RESTART_GAP)}s)"
    try:
        if RESTART_METHOD == "systemctl" and UNIT:
            r = subprocess.run(["systemctl", "restart", UNIT],
                               capture_output=True, text=True, timeout=60)
            ok, msg = r.returncode == 0, r.stderr.strip()
        elif RESTART_METHOD == "docker" and CONTAINER:
            r = subprocess.run(["docker", "restart", CONTAINER],
                               capture_output=True, text=True, timeout=120)
            ok, msg = r.returncode == 0, r.stderr.strip()
        elif RESTART_METHOD == "kill_pid" and PIDFILE and os.path.exists(PIDFILE):
            with open(PIDFILE) as f:
                pid = f.read().strip()
            r = subprocess.run(["kill", "-9", pid], capture_output=True, text=True)
            ok, msg = r.returncode == 0, r.stderr.strip()
        else:
            return False, f"unknown/empty restart method: {RESTART_METHOD}"
        if ok:
            _last_restart = time.time()
        log(f"restart ({RESTART_METHOD}): ok={ok} msg={msg[:200]}")
        return ok, msg[:500]
    except Exception as e:
        log(f"restart exception: {e}")
        return False, str(e)[:500]


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/metrics":
            self._json(200, collect())
        elif self.path == "/health":
            self._json(200, {"ts": now_iso(), "alive": True,
                             "method": RESTART_METHOD, "unit": UNIT})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/restart":
            self._json(404, {"error": "not found"})
            return
        got = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not TOKEN or not hmac.compare_digest(got.encode(), TOKEN.encode()):
            self._json(403, {"ts": now_iso(), "error": "forbidden"})
            return
        ok, msg = do_restart()
        self._json(200 if ok else 500, {"ts": now_iso(), "restarted": ok, "error": msg})

    def log_message(self, fmt, *args):
        log(fmt % args)


def main():
    if not TOKEN:
        raise SystemExit("WATCHDOG_TOKEN не задан — отказ в запуске")
    log(f"start: port={PORT} engine={ENGINE_URL} restart={RESTART_METHOD}:{UNIT or CONTAINER or PIDFILE}")
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.serve_forever()


if __name__ == "__main__":
    main()