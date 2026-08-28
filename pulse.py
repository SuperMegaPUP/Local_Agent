#!/usr/bin/env python3
"""Пульс конвейера: короткая сводка состояния. Печатает в stdout И дописывает в pulses.log.
Лёгкий (без tsc — он тяжёлый и конкурирует с кодерами за CPU; tsc меряется гейтами)."""
import os, sqlite3, subprocess, time
from datetime import datetime, timezone

BASE = os.path.expanduser("~/agent-pipeline")
DB = os.path.join(BASE, "kanban", "boards", "main.sqlite3")
PROJ = os.path.expanduser("~/gorizont-sobytij_test")
LOG = os.path.join(BASE, "pulses.log")
VLLM_METRICS = "http://192.168.31.52:8000/metrics"


def now_ms():
    return datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")


def kanban():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    cnt = {r["s"]: r["n"] for r in c.execute("SELECT status s, COUNT(*) n FROM cards GROUP BY status")}
    running = [r for r in c.execute("SELECT id, title, started_at, assignee FROM cards WHERE status='running'")]
    review = [r for r in c.execute("SELECT id, title FROM cards WHERE status='review'")]
    ready_n = cnt.get("ready", 0)
    done_recent = [r["title"] for r in c.execute("SELECT title FROM cards WHERE status='done' ORDER BY finished_at DESC LIMIT 3")]
    blocked = [r["title"] for r in c.execute("SELECT title FROM cards WHERE status='blocked'")]
    c.close()
    return cnt, running, review, ready_n, done_recent, blocked


def vllm_queue():
    try:
        out = subprocess.run(["curl", "-s", "--max-time", "4", VLLM_METRICS], capture_output=True, text=True, timeout=6).stdout
        run = wait = "?"
        for ln in out.splitlines():
            if ln.startswith("vllm:num_requests_running{"):
                run = ln.rsplit(" ", 1)[-1]
            elif ln.startswith("vllm:num_requests_waiting{"):
                wait = ln.rsplit(" ", 1)[-1]
        return run, wait
    except Exception:
        return "?", "?"


def git_activity():
    try:
        st = subprocess.run(["git", "status", "-s"], cwd=PROJ, capture_output=True, text=True, timeout=10).stdout
        dirty = sum(1 for l in st.splitlines() if l and not l.startswith("??"))
        lg = subprocess.run(["git", "log", "--oneline", "-1"], cwd=PROJ, capture_output=True, text=True, timeout=10).stdout.strip()
        return dirty, lg
    except Exception:
        return "?", "?"


def pulse():
    cnt, running, review, ready_n, done_recent, blocked = kanban()
    vr, vw = vllm_queue()
    dirty, last_commit = git_activity()
    L = []
    L.append(f"=== ПУЛЬС {now_ms()} ===")
    L.append(f"доска: running={cnt.get('running',0)} ready={ready_n} review={cnt.get('review',0)} done={cnt.get('done',0)} todo={cnt.get('todo',0)} blocked={cnt.get('blocked',0)}")
    if running:
        for r in running:
            age = ""
            try:
                st = datetime.strptime(r["started_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                age = f" ({int((datetime.now(timezone.utc)-st).total_seconds()//60)}м)"
            except Exception:
                pass
            L.append(f"  ▶ {r['title'][:52]}{age}")
    if review:
        for r in review:
            L.append(f"  ◼ review: {r['title'][:52]}")
    if blocked:
        for b in blocked:
            L.append(f"  ✖ blocked: {b[:52]}")
    L.append(f"vLLM: running={vr} waiting={vw} | repo dirty={dirty} | last: {last_commit[:40]}")
    if done_recent:
        L.append("recent done: " + "; ".join(t[:30] for t in done_recent))
    return "\n".join(L)


if __name__ == "__main__":
    msg = pulse()
    print(msg)
    try:
        with open(LOG, "a") as f:
            f.write(msg + "\n\n")
    except Exception:
        pass