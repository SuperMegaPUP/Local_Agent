#!/usr/bin/env python3
"""Review fastlane: автоприёмка review-карточек с свежим зелёным гейтом.

Правила (консервативно, решение старшего 2026-08-26):
  1. status == review, kind != epic
  2. Последнее гейт-событие = gates-passed и ему <= 15 минут (свежая зелень)
  3. Менее 2 gates-failed за последние 60 минут (карточка не «качает»)
  4. Есть ARTIFACTS-комментарий (воркер соблюдает ритуал финализации)
  5. Название НЕ попадает в чёрный список чувствительных областей
     (секур/секреты/auth/docker/tsconfig/schema/миграции) — такие закрывают вручную
Лимит: не более 3 автозакрытий за один прогон.
Выход: тихий (пустой stdout), если ничего не сделано; иначе — сводка.
"""
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta

BASE = os.path.expanduser("~/agent-pipeline")
DB = os.path.join(BASE, "kanban", "boards", "main.sqlite3")
LOG = os.path.join(BASE, "fastlane.log")

FRESH_GATE_MIN = 15      # гейт-зелень не старше
FLAP_WINDOW_MIN = 60     # окно детекции качаний
FLAP_MAX_FAILED = 2      # больше — не трогаем
PER_RUN_CAP = 3

# ---- SIMBIOSIS W2: слой C (интеллектуальный TraceGuard) ------------------
# Уровень 1 (жёсткий, всегда): ARTIFACTS заявляет коммиты -> каждый хеш
# обязан существовать в объектном хранилище репо (git rev-parse). Хеш из
# worktree виден из центрального репо (общий объект-стор) — ложных срабатываний
# нет. Карточка без заявленных коммитов (доклады/отчёты) — проверка не применяется.
# Уровень 2 (опциональный, FASTLANE_SEMANTIC_VERIFY=1): дешёвый LLM-вызов
# сверяет финальный комментарий с реально произошедшими событиями карточки.
# Fail-open: любая ошибка верификатора = пропуск уровня (не блокируем приёмку).
PROJECT_DIR = os.path.expanduser(os.environ.get("FASTLANE_PROJECT_DIR", "~/gorizont-sobytij_test"))
SEMANTIC_VERIFY = os.environ.get("FASTLANE_SEMANTIC_VERIFY", "0") == "1"
VLLM_CHAT_URL = os.environ.get("FASTLANE_VLLM_URL", "http://192.168.31.52:8000/v1/chat/completions")
COMMIT_RE = re.compile(r"\b[0-9a-f]{7,40}\b")


def extract_claimed_commits(body):
    """Хеши коммитов, заявленные в тексте ARTIFACTS (7+ hex)."""
    return COMMIT_RE.findall(body or "")


def verify_commits(repo, hashes):
    """Уровень 1: все заявленные коммиты существуют в репо? (repo, [плохие])."""
    bad = []
    for h in dict.fromkeys(hashes):  # уникальные, сохраняя порядок
        try:
            r = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", h + "^{commit}"],
                               capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                bad.append(h)
        except Exception:
            bad.append(h)
    return bad


def semantic_verify(card_title, draft, events_tail):
    """Уровень 2: LLM сверяет черновик с реальными событиями. (ok, issues) или None."""
    import json as _json
    import urllib.request
    prompt = (
        "Ты — верификатор отчёта рабочего агента. Ответь СТРОГО JSON: "
        "{\"ok\": true/false, \"issues\": [\"...\"]}.\n"
        f"ЗАДАЧА: {card_title}\n"
        f"ОТЧЁТ АГЕНТА (черновик):\n{draft[:3000]}\n"
        f"РЕАЛЬНО ПРОИСХОДИВШИЕ СОБЫТИЯ (журнал):\n{events_tail[:2000]}\n"
        "ok=true, если каждое существенное заявление отчёта подтверждается журналом "
        "или является разумным следствием. ok=false, если есть заявления о действиях "
        "(зашёл/проверил/вызвал/добавил), отсутствующих в журнале. issues — конкретика."
    )
    body = _json.dumps({
        "model": "qwen3.8-27b",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0, "max_tokens": 400,
    }).encode()
    req = urllib.request.Request(VLLM_CHAT_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = _json.loads(resp.read())
        txt = data["choices"][0]["message"]["content"].strip()
        m = re.search(r"\{.*\}", txt, re.S)
        verdict = _json.loads(m.group(0) if m else txt)
        return bool(verdict.get("ok")), verdict.get("issues", [])
    except Exception as e:
        log_line("semantic_verify: fail-open (%s)" % repr(e)[:120])
        return None

MANUAL_RE = re.compile(
    r"(секрет|secret|token|парол|password|(?<!-)auth|crypto|шифр|key-?rot|rotaci|ротаци|"
    r"migrat|миграц|docker|deploy|инфра|infra|tsconfig|schema|cert|ssl|jwt)",
    re.IGNORECASE,
)


def parse_ts(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def log_line(msg):
    line = "[%s] %s" % (datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), msg)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def _run_once(now, dry):
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    closed, skipped_manual, skipped_flap, skipped_stale, skipped_trace = [], [], [], [], []

    cards = list(c.execute("SELECT * FROM cards WHERE status='review' AND kind!='epic'"))
    for card in cards:
        cid, title = card["id"], card["title"]
        # SIMBIOSIS W3 (слой J): consult принимается по ANSWER:-комментарию
        # именно ВОΡКЕРА (author=assignee), а не по ARTIFACTS. Механический
        # гейт к консультации неприменим (gateway ставит light-gate штамп).
        is_consult = card["kind"] == "consult"
        evs = list(c.execute(
            "SELECT type, payload, created_at FROM events WHERE card_id=? ORDER BY id ASC", (cid,)))
        gates = [(e, parse_ts(e["created_at"])) for e in evs if e["type"].startswith("gates")]
        gates = [(e, t) for e, t in gates if t]
        if not gates:
            continue
        last_ev, last_ts = gates[-1]
        if last_ev["type"] != "gates-passed":
            continue
        if now - last_ts > timedelta(minutes=FRESH_GATE_MIN):
            skipped_stale.append(cid)
            continue
        recent_failed = sum(1 for e, t in gates
                            if e["type"] == "gates-failed"
                            and now - t <= timedelta(minutes=FLAP_WINDOW_MIN))
        if recent_failed >= FLAP_MAX_FAILED:
            skipped_flap.append(cid)
            continue
        if is_consult:
            ans_rows = list(c.execute(
                "SELECT body FROM comments WHERE card_id=? AND author=? AND body LIKE ? ORDER BY id DESC",
                (cid, card["assignee"], "%ANSWER%")))
        else:
            ans_rows = list(c.execute(
                "SELECT body FROM comments WHERE card_id=? AND body LIKE ? ORDER BY id DESC",
                (cid, "%ARTIFACTS%")))
        if not ans_rows:
            skipped_stale.append(cid)  # нет артефактов/ответа — ждём
            continue
        # ---- SIMBIOSIS W2 (слой C): TraceGuard --------------------------------
        # Уровень 1: заявленные коммиты обязаны существовать в репо.
        claimed = extract_claimed_commits(ans_rows[0]["body"])
        if claimed:
            bad = verify_commits(PROJECT_DIR, claimed)
            if bad:
                skipped_trace.append(cid)
                log_line("TRACEGUARD %s :: несуществующие коммиты %s — приёмка отложена"
                         % (cid, ",".join(bad[:5])))
                continue
        # Уровень 2 (опционально): семантическая сверка отчёта с журналом.
        if SEMANTIC_VERIFY:
            events_tail = "\n".join(
                "[%s] %s %s" % ((parse_ts(e["created_at"]) or now).strftime("%H:%M:%S"),
                                e["type"], (e["payload"] or "")[:100])
                for e in evs[-15:])
            verdict = semantic_verify(title, ans_rows[0]["body"], events_tail)
            if verdict is not None and not verdict[0]:
                skipped_trace.append(cid)
                log_line("TRACEGUARD %s :: семантика: %s — приёмка отложена"
                         % (cid, "; ".join(str(i) for i in verdict[1][:3])[:200]))
                continue
        # ------------------------------------------------------------------------
        if MANUAL_RE.search(title):
            skipped_manual.append(cid)
            continue
        if len(closed) >= PER_RUN_CAP:
            break
        if dry:
            closed.append(cid)
            continue
        note = ("fastlane: автоприёмка. Гейт PASSED @%s (%s), ARTIFACTS на месте, "
                "правила fastlane v1 (поручение старшего 2026-08-26)."
                % (last_ts.strftime("%H:%M:%S"), (last_ev["payload"] or "")[:120]))
        c.execute("UPDATE cards SET status='done', finished_at=? WHERE id=? AND status='review'",
                  (now.strftime("%Y-%m-%d %H:%M:%S"), cid))
        c.execute("INSERT INTO events(card_id,type,payload,created_at) VALUES(?,?,?,?)",
                  (cid, "done", "fastlane auto-accept", now.strftime("%Y-%m-%d %H:%M:%S")))
        c.execute("INSERT INTO comments(card_id,author,body,created_at) VALUES(?,?,?,?)",
                  (cid, "senior", note, now.strftime("%Y-%m-%d %H:%M:%S")))
        c.commit()
        closed.append(cid)
        log_line("AUTO-CLOSE %s :: %s" % (cid, title[:60]))

    c.close()
    parts = []
    if closed:
        parts.append("closed=%d %s%s" % (len(closed), ",".join(closed), " (DRY)" if dry else ""))
    if skipped_manual:
        parts.append("manual_hold=%s" % ",".join(skipped_manual))
    if skipped_flap:
        parts.append("flap_hold=%s" % ",".join(skipped_flap))
    if skipped_stale:
        parts.append("stale_no_artifacts=%s" % ",".join(skipped_stale))
    if skipped_trace:
        parts.append("trace_hold=%s" % ",".join(skipped_trace))
    if parts:
        msg = "FASTLANE: " + "; ".join(parts)
        log_line(msg)
        print(msg)


if __name__ == "__main__":
    # Ретраи: шлюз держит транзакцию во время гейтов (tsc/lint/jest — минуты),
    # fastlane может попасть на 'database is locked'. Без обработки ошибка
    # убивала cron-тик (инцидент 2026-08-26: last_status=error, review копился).
    import time
    dry = "--dry" in sys.argv
    for attempt in range(4):
        try:
            _run_once(datetime.utcnow(), dry)
            break
        except sqlite3.OperationalError as e:
            if "locked" in str(e) and attempt < 3:
                time.sleep(10 * (attempt + 1))
                continue
            log_line("FASTLANE ERROR: %s" % e)
            raise