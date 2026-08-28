#!/usr/bin/env python3
"""Kanban CLI — единая точка правды конвейера Горизонт Событий. Stdlib only."""
import argparse, os, sqlite3, sys, uuid
from datetime import datetime, timezone

BASE = os.path.expanduser(os.environ.get("PIPELINE_HOME", "~/agent-pipeline"))
DB = os.path.join(BASE, "kanban", "boards", "main.sqlite3")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def con():
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS cards (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT DEFAULT '',
  kind TEXT DEFAULT 'task' CHECK(kind IN ('task','epic','escalation','intervention','report','consult')),
  assignee TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'todo' CHECK(status IN ('todo','ready','running','review','done','blocked')),
  parent_id TEXT REFERENCES cards(id), depends_on TEXT DEFAULT '',
  timeout_minutes INTEGER DEFAULT 90, retry_count INTEGER DEFAULT 0, max_retries INTEGER DEFAULT 3,
  created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, heartbeat TEXT);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, card_id TEXT NOT NULL REFERENCES cards(id),
  type TEXT NOT NULL, payload TEXT DEFAULT '', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT, card_id TEXT NOT NULL REFERENCES cards(id),
  author TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, card_id TEXT NOT NULL REFERENCES cards(id),
  attempt INTEGER NOT NULL, command TEXT, exit_code INTEGER, output TEXT DEFAULT '',
  started_at TEXT NOT NULL, finished_at TEXT);
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL CHECK(kind IN ('memory','lesson')),
  content TEXT NOT NULL, project TEXT DEFAULT '', tags TEXT DEFAULT '', created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_cards_status ON cards(status);
CREATE INDEX IF NOT EXISTS idx_events_card ON events(card_id);
CREATE INDEX IF NOT EXISTS idx_comments_card ON comments(card_id);
"""

TRANSITIONS = {
    "todo": {"ready", "blocked"},
    "ready": {"running", "blocked", "todo"},
    "running": {"review", "blocked", "ready"},
    "review": {"done", "blocked", "todo"},
    "blocked": {"ready", "todo", "done"},
    "done": {"todo"},
}


def ev(c, cid, t, p=""):
    c.execute("INSERT INTO events(card_id,type,payload,created_at) VALUES(?,?,?,?)", (cid, t, p, now()))


def cmd_init(a):
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB)
    c.executescript(SCHEMA)
    c.commit(); c.close()
    print("OK init:", DB)


def cmd_create(a):
    cid = "t_" + uuid.uuid4().hex[:8]
    c = con()
    c.execute("""INSERT INTO cards(id,title,description,kind,assignee,status,parent_id,
                 depends_on,timeout_minutes,max_retries,created_at)
                 VALUES(?,?,?,?,?,'todo',?,?,?,?,?)""",
              (cid, a.title, a.description or "", a.kind, a.assignee, a.parent or None,
               (a.depends or "").strip(), a.timeout, a.retries, now()))
    ev(c, cid, "created", f"assignee={a.assignee}; parent={a.parent or '-'}; deps={a.depends or '-'}")
    c.commit(); c.close()
    print(cid)


def fmt_row(r):
    dur = ""
    if r["started_at"] and r["finished_at"]:
        try:
            f = "%Y-%m-%d %H:%M:%S"
            d = (datetime.strptime(r["finished_at"], f) - datetime.strptime(r["started_at"], f)).total_seconds() / 60
            dur = f" ({d:.0f}м)"
        except Exception:
            pass
    return f"{r['id']:<12}{r['status']:<9}{r['assignee']:<15}{r['retry_count']}/{r['max_retries']}  {r['title'][:60]}{dur}"


def cmd_list(a):
    c = con()
    q, w, p = "SELECT * FROM cards", [], []
    if a.status:
        w.append("status=?"); p.append(a.status)
    if a.assignee:
        w.append("assignee=?"); p.append(a.assignee)
    if w:
        q += " WHERE " + " AND ".join(w)
    rows = c.execute(q + " ORDER BY created_at", p).fetchall()
    if not rows:
        print("(доска пуста)")
        return
    print(f"{'ID':<12}{'STATUS':<9}{'ASSIGNEE':<15}{'RETRY':<7}TITLE")
    for r in rows:
        print(fmt_row(r))


def cmd_show(a):
    c = con()
    r = c.execute("SELECT * FROM cards WHERE id=?", (a.id,)).fetchone()
    if not r:
        print("не найдено:", a.id); sys.exit(1)
    print(fmt_row(r))
    print("kind:", r["kind"], "| parent:", r["parent_id"] or "-", "| deps:", r["depends_on"] or "-")
    print("desc:", r["description"])
    print("-- events:")
    for e in c.execute("SELECT * FROM events WHERE card_id=? ORDER BY id", (a.id,)):
        print(f"  [{e['created_at']}] {e['type']}: {e['payload'][:120]}")
    print("-- comments:")
    for cm in c.execute("SELECT * FROM comments WHERE card_id=? ORDER BY id", (a.id,)):
        print(f"  [{cm['created_at']}] {cm['author']}: {cm['body'][:300]}")
    print("-- runs:")
    for rn in c.execute("SELECT * FROM runs WHERE card_id=? ORDER BY id DESC LIMIT 5", (a.id,)):
        print(f"  #{rn['attempt']} rc={rn['exit_code']} [{rn['started_at']}] {(rn['output'] or '')[:150]}")


def cmd_move(a):
    c = con()
    r = c.execute("SELECT * FROM cards WHERE id=?", (a.id,)).fetchone()
    if not r:
        print("не найдено:", a.id); sys.exit(1)
    cur, tgt = r["status"], a.status
    if tgt not in TRANSITIONS[cur]:
        print(f"НЕДОПУСТИМО: {cur} -> {tgt}")
        sys.exit(1)
    if tgt == "done" and not a.verified:
        print("done требует --verified <почему>")
        sys.exit(1)
    ts = {"started_at": tgt == "running", "finished_at": tgt in ("done", "blocked", "review")}
    if tgt == "running":
        c.execute("UPDATE cards SET status=?, started_at=? WHERE id=?", (tgt, now(), a.id))
    elif tgt in ("done", "blocked", "review"):
        c.execute("UPDATE cards SET status=?, finished_at=? WHERE id=?", (tgt, now(), a.id))
    else:
        c.execute("UPDATE cards SET status=? WHERE id=?", (tgt, a.id))
    ev(c, a.id, "move", f"{cur}->{tgt}" + (f" verified={a.verified}" if a.verified else ""))
    c.commit(); c.close()
    print(f"OK {a.id}: {cur} -> {tgt}")


def cmd_comment(a):
    c = con()
    c.execute("INSERT INTO comments(card_id,author,body,created_at) VALUES(?,?,?,?)", (a.id, a.author, a.body, now()))
    c.commit(); c.close()
    print("OK comment")


def cmd_mem(a):
    c = con()
    c.execute("INSERT INTO memories(kind,content,project,tags,created_at) VALUES(?,?,?,?,?)",
              (a.kind, a.content, a.project or "horizon", a.tags or "", now()))
    c.commit(); c.close()
    print("OK memory")


def cmd_recall(a):
    c = con()
    q, p = "SELECT * FROM memories WHERE content LIKE ?", [f"%{a.q}%"]
    if a.kind:
        q += " AND kind=?"
        p.append(a.kind)
    for m in c.execute(q + " ORDER BY id DESC LIMIT 20", p):
        print(f"[{m['kind']}] {m['content']}")


def known_roles():
    """Роли берутся из каталога roles/ (динамически, без хардкода)."""
    rd = os.path.join(BASE, "roles")
    try:
        return {d for d in os.listdir(rd) if os.path.isdir(os.path.join(rd, d))}
    except OSError:
        return set()


def cmd_metrics(a):
    """Внешний аудит 2026-08-28 (итерация 6): метрики эффективности конвейера.
    Чистый SQL по events/cards/runs — 0 LLM. Показывает, РАБОТАЮТ ЛИ слои:
    nudge-success, gate-FAIL-rate, среднее время цикла, эскалации, причины смертей."""
    c = con()
    FMT = "%Y-%m-%d %H:%M:%S"

    def dt(s):
        try:
            return datetime.strptime(s, FMT)
        except Exception:
            return None

    total_done = c.execute("SELECT COUNT(*) n FROM cards WHERE status='done' AND kind!='epic'").fetchone()["n"]
    total_all = c.execute("SELECT COUNT(*) n FROM cards WHERE kind!='epic'").fetchone()["n"]

    # Nudge economics: cards that got nudged and eventually finished vs died.
    nudged_cards = c.execute("""
        SELECT DISTINCT e.card_id FROM events e JOIN cards k ON k.id=e.card_id
        WHERE e.type='nudge'""").fetchall()
    nu_total = len(nudged_cards)
    nu_survived = 0
    for row in nudged_cards:
        st = c.execute("SELECT status FROM cards WHERE id=?", (row["card_id"],)).fetchone()
        if st and st["status"] == "done":
            nu_survived += 1

    # Gate statistics: passed vs failed stamps.
    gp = c.execute("SELECT COUNT(*) n FROM events WHERE type='gates-passed'").fetchone()["n"]
    gf = c.execute("SELECT COUNT(*) n FROM events WHERE type='gates-failed'").fetchone()["n"]
    gate_rate = (gf / (gp + gf) * 100) if (gp + gf) else 0.0

    # Cycle time: created_at -> finished_at for done cards.
    durations = []
    for r in c.execute("SELECT created_at, finished_at FROM cards WHERE status='done' AND kind!='epic'"):
        a_, b_ = dt(r["created_at"]), dt(r["finished_at"])
        if a_ and b_:
            durations.append((b_ - a_).total_seconds() / 3600)
    avg_hours = sum(durations) / len(durations) if durations else 0.0

    # Escalations: created vs resolved(done).
    esc_created = c.execute("SELECT COUNT(*) n FROM cards WHERE kind='escalation'").fetchone()["n"]
    esc_done = c.execute("SELECT COUNT(*) n FROM cards WHERE kind='escalation' AND status='done'").fetchone()["n"]

    # Death reasons distribution (from retry/blocked event payloads).
    reasons = {}
    for e in c.execute("SELECT payload FROM events WHERE type IN ('retry','blocked')"):
        p = (e["payload"] or "")[:40]
        if "LOOP" in p.upper():
            key = "loop"
        elif "stuck>" in p:
            key = "stuck/heartbeat"
        elif "dead" in p:
            key = "crash(dead)"
        else:
            key = "other:" + p[:20]
        reasons[key] = reasons.get(key, 0) + 1

    # Retries per card histogram.
    retried = c.execute("SELECT COUNT(*) n FROM cards WHERE retry_count>0 AND kind!='epic'").fetchone()["n"]

    print("=" * 62)
    print("METRICS (аудит 2026-08-28, итерация 6) — %s" % now())
    print("=" * 62)
    print("Cards: total=%d done=%d retried=%d" % (total_all, total_done, retried))
    print("-" * 62)
    print("[NUDGE ECONOMICS]")
    print("  cards ever nudged: %d" % nu_total)
    if nu_total:
        print("  survived to done: %d (%.0f%%)" % (nu_survived, nu_survived / nu_total * 100))
    print("[GATES]")
    print("  passed=%d failed=%d  FAIL-rate=%.1f%%" % (gp, gf, gate_rate))
    print("[THROUGHPUT]")
    print("  avg cycle time (created->done): %.1fh over %d cards" % (avg_hours, len(durations)))
    print("[ESCALATIONS]")
    print("  created=%d resolved=%d" % (esc_created, esc_done))
    print("[DEATH REASONS (retry+blocked events)]")
    if reasons:
        for k, v in sorted(reasons.items(), key=lambda x: -x[1]):
            print("  %-22s %d" % (k, v))
    else:
        print("  (нет)")
    c.close()


def cmd_ask(a):
    """SIMBIOSIS W3 (слой J): консультация у роли. kind=consult, без декомпозии.
    Воркер отвечает комментарием ANSWER: + move review; fastlane принимает
    (consult исключён из механического гейта)."""
    role = a.role.strip()
    if role not in known_roles():
        print("неизвестная роль:", role, "| варианты:", ", ".join(sorted(known_roles())))
        sys.exit(1)
    q = a.question.strip()
    if not q:
        print("пустой вопрос"); sys.exit(1)
    cid = "t_" + uuid.uuid4().hex[:8]
    c = con()
    desc = (
        f"КОНСУЛЬТАЦИЯ (симбиоз, слой J). Вопрос старшего:\n\n{q}\n\n"
        "Протокол: изучи (kb show/kb recall, CONTEXT/ проекта, репо) и дай ОТВЕТ С "
        "ДОКАЗАТЕЛЬСТВАМИ (файл:строка, данные, ссылки). Продакшн-код НЕ менять. "
        "Финал: комментарий, начинающийся с 'ANSWER: ', и move --status review."
    )
    c.execute("""INSERT INTO cards(id,title,description,kind,assignee,status,parent_id,
                 depends_on,timeout_minutes,max_retries,created_at)
                 VALUES(?,?,?,?,?,'todo',?,'',45,3,?)""",
              (cid, f"[ask:{role}] {q[:70]}", desc, "consult", role, a.parent or None, now()))
    ev(c, cid, "created", f"consult for {role}")
    c.commit(); c.close()
    print(cid)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sp = sub.add_parser("create")
    sp.add_argument("--title", required=True); sp.add_argument("--description", default="")
    sp.add_argument("--kind", default="task"); sp.add_argument("--assignee", required=True)
    sp.add_argument("--parent"); sp.add_argument("--depends"); sp.add_argument("--timeout", type=int, default=90)
    sp.add_argument("--retries", type=int, default=3)
    sp = sub.add_parser("list"); sp.add_argument("--status"); sp.add_argument("--assignee")
    sp = sub.add_parser("show"); sp.add_argument("id")
    sp = sub.add_parser("move"); sp.add_argument("id"); sp.add_argument("--status", required=True); sp.add_argument("--verified", default="")
    sp = sub.add_parser("comment"); sp.add_argument("id"); sp.add_argument("--author", required=True); sp.add_argument("--body", required=True)
    sp = sub.add_parser("mem"); sp.add_argument("--kind", default="memory"); sp.add_argument("--content", required=True); sp.add_argument("--project"); sp.add_argument("--tags")
    sp = sub.add_parser("recall"); sp.add_argument("q"); sp.add_argument("--kind")
    sp = sub.add_parser("ask"); sp.add_argument("role"); sp.add_argument("question"); sp.add_argument("--parent")
    sub.add_parser("metrics")
    a = ap.parse_args()
    {"init": cmd_init, "create": cmd_create, "list": cmd_list, "show": cmd_show,
     "move": cmd_move, "comment": cmd_comment, "mem": cmd_mem, "recall": cmd_recall,
     "ask": cmd_ask, "metrics": cmd_metrics}[a.cmd](a)


if __name__ == "__main__":
    main()