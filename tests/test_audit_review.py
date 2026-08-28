#!/usr/bin/env python3
"""Регрессионные тесты: внешняя ревизия 2026-08-28 (итерации 2, 3, 4, 6, 7, 8).

Изоляция по рецепту (урок инцидента 2026-08-27):
  - env ДО импорта модулей (gateway читает env при импорте)
  - assert gw.DB.startswith(td) сразу после импорта
  - fastlane: monkeypatch fl.DB/fl.LOG/fl.PROJECT_DIR после импорта
  - git-репо для TraceGuard — git init в tmpdir
  - launch_worker НЕ вызывается (MAX_PARALLEL=0, DRY_RUN=1)
"""
import importlib.util
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

TD = tempfile.mkdtemp(prefix="audit_rev_")
PIPELINE_HOME = TD
PROJECT_DIR = TD + "/proj"
BOARD_DB = TD + "/board.sqlite3"

os.environ.update({
    "PIPELINE_HOME": PIPELINE_HOME,
    "PIPELINE_PROJECT_DIR": PROJECT_DIR,
    "PIPELINE_MAX_PARALLEL": "0",
    "PIPELINE_DRY_RUN": "1",
    "PIPELINE_WORKTREES": "0",
    "FASTLANE_PROJECT_DIR": PROJECT_DIR,
    "FASTLANE_LOG": TD + "/fastlane.log",
    "FASTLANE_SEMANTIC_VERIFY": "0",
    "FASTLANE_L2_RETRY_ONLY": "1",
})

PASSED, FAILED = [], []


def check(name, cond, detail=""):
    if cond:
        PASSED.append(name)
        print("PASS %s" % name)
    else:
        FAILED.append(name)
        print("FAIL %s :: %s" % (name, detail))


def load(mod, path):
    spec = importlib.util.spec_from_file_location(mod, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


STAGE = os.path.dirname(os.path.abspath(__file__))
gw = load("gw_under_test", STAGE + "/gateway.py")
assert gw.DB.startswith(TD), "gateway попал не в тестовую БД: " + gw.DB
fl = load("fl_under_test", STAGE + "/fastlane.py")
fl.DB = BOARD_DB
fl.LOG = TD + "/fastlane.log"
fl.PROJECT_DIR = PROJECT_DIR

# ---------- git-репо для TraceGuard L1.5 ----------
subprocess.run(["git", "init", "-q", "-b", "main", PROJECT_DIR], check=True)
subprocess.run(["git", "-C", PROJECT_DIR, "config", "user.email", "t@t"], check=True)
subprocess.run(["git", "-C", PROJECT_DIR, "config", "user.name", "t"], check=True)
with open(PROJECT_DIR + "/a.txt", "w") as f:
    f.write("hello\n")
subprocess.run(["git", "-C", PROJECT_DIR, "add", "-A"], check=True)
subprocess.run(["git", "-C", PROJECT_DIR, "commit", "-qm", "real commit"], check=True)
REAL_SHA = subprocess.run(["git", "-C", PROJECT_DIR, "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
# пустой коммит
EMPTY_SHA = subprocess.run(["git", "-C", PROJECT_DIR, "commit", "-q", "--allow-empty", "-m", "empty"],
                           capture_output=True, text=True, check=True).returncode
EMPTY_SHA = subprocess.run(["git", "-C", PROJECT_DIR, "rev-parse", "HEAD"],
                           capture_output=True, text=True, check=True).stdout.strip()
# wt-ветка с коммитом (симуляция незавершённого worktree)
subprocess.run(["git", "-C", PROJECT_DIR, "checkout", "-qb", "wt/card_x"], check=True)
with open(PROJECT_DIR + "/b.txt", "w") as f:
    f.write("bbb\n")
subprocess.run(["git", "-C", PROJECT_DIR, "add", "-A"], check=True)
subprocess.run(["git", "-C", PROJECT_DIR, "commit", "-qm", "wt commit"], check=True)
WT_SHA = subprocess.run(["git", "-C", PROJECT_DIR, "rev-parse", "HEAD"],
                        capture_output=True, text=True, check=True).stdout.strip()
subprocess.run(["git", "-C", PROJECT_DIR, "checkout", "-q", "main"], check=True)

# ================= fastlane: L1.5 =================
# 1. Реальный коммит на main — проходит
probs = fl.verify_commit_content(PROJECT_DIR, [REAL_SHA], "card_x")
check("L1.5 real-commit-ok", probs == [], str(probs))
# 2. Пустой коммит — отклоняется
probs = fl.verify_commit_content(PROJECT_DIR, [EMPTY_SHA], "card_x")
check("L1.5 empty-commit-reject", any("пустой" in w for _, w in probs), str(probs))
# 3. Коммит на wt-ветке (недостигаемый из main) — проходит (ветка легитимна)
probs = fl.verify_commit_content(PROJECT_DIR, [WT_SHA], "card_x")
check("L1.5 wt-branch-ok", probs == [], str(probs))
# 4. Несуществующий хеш — L1 уже ловит, L1.5 не должен падать
bad = fl.verify_commits(PROJECT_DIR, ["deadbeefdeadbeefdeadbeefdeadbeefdeadbee"])
check("L1 fake-hash-still-caught", len(bad) == 1, str(bad))

# ================= fastlane: L2 селекция (логика) =================
class FakeCard(dict):
    def __getitem__(self, k):
        return super().__getitem__(k)


fc_retry = FakeCard({"retry_count": 1})
fc_first = FakeCard({"retry_count": 0})
l2_retry = fl.SEMANTIC_VERIFY and ((not fl.L2_RETRY_ONLY) or (fc_retry["retry_count"] or 0) >= 1)
l2_first = fl.SEMANTIC_VERIFY and ((not fl.L2_RETRY_ONLY) or (fc_first["retry_count"] or 0) >= 1)
check("L2 gating constants", fl.L2_RETRY_ONLY is True and fl.SEMANTIC_VERIFY is False)
# при SEMANTIC_VERIFY=1: retry-карточка верифицируется, первая — нет
fl.SEMANTIC_VERIFY = True
l2_retry = fl.SEMANTIC_VERIFY and ((not fl.L2_RETRY_ONLY) or (fc_retry["retry_count"] or 0) >= 1)
l2_first = fl.SEMANTIC_VERIFY and ((not fl.L2_RETRY_ONLY) or (fc_first["retry_count"] or 0) >= 1)
check("L2 selective", l2_retry is True and l2_first is False, "retry=%s first=%s" % (l2_retry, l2_first))
check("L2 limits", fl.L2_MAX_TOKENS == 200 and fl.L2_TIMEOUT == 30,
      "tok=%s tmo=%s" % (fl.L2_MAX_TOKENS, fl.L2_TIMEOUT))

# ================= gateway: extract_diag (итерация 2) =================
os.makedirs(gw.RUNDIR, exist_ok=True)
lp = os.path.join(gw.RUNDIR, "c_diag-0.log")
with open(lp, "w") as f:
    f.write("$ npm run build\nAll good so far\n"
            "src/x.ts(10,5): error TS2304: Cannot find name 'foo'\n"
            "some normal line\n"
            "TypeError: undefined is not a function\n"
            "more noise\n")
diag = gw.extract_diag("c_diag")
check("diag finds-errors", "TS2304" in diag and "TypeError" in diag, diag[:200])
check("diag excludes-noise", "normal line" not in diag and "good so far" not in diag, diag[:200])
# чистый лог — пусто
lp2 = os.path.join(gw.RUNDIR, "c_clean-0.log")
with open(lp2, "w") as f:
    f.write("$ ls\nfile1\nfile2\n")
check("diag clean-log-empty", gw.extract_diag("c_clean") == "", gw.extract_diag("c_clean")[:100])

# ================= gateway: detect_install_violation (итерация 7) =================
cases_hit = [
    "$ npm install lodash",
    "$ npm i express",
    "$ npm add axios",
    "$ npm ci",
    "$ cd /somewhere && npm install foo",
    "$ yarn add react",
    "$ yarn",
    "$ pnpm add vite",
    "$ bun add zstd",
]
cases_miss = [
    "$ npm info lodash",
    "$ npm run build",
    "$ npm test",
    "$ npm ls",
    "$ yarn build",
    "$ yarn install --dry-run",
    "$ pnpm list",
    "$ echo npm install",
]
for cmd in cases_hit:
    lp3 = os.path.join(gw.RUNDIR, "c_inst-0.log")
    with open(lp3, "w") as f:
        f.write("working...\n" + cmd + "\nok\n")
    got = gw.detect_install_violation("c_inst")
    check("install-hit [%s]" % cmd[:24], got is not None, "expected hit, got None")
for cmd in cases_miss:
    lp3 = os.path.join(gw.RUNDIR, "c_inst-0.log")
    with open(lp3, "w") as f:
        f.write("working...\n" + cmd + "\nok\n")
    got = gw.detect_install_violation("c_inst")
    check("install-miss [%s]" % cmd[:24], got is None, "false positive: %s" % got)

# ================= gateway: inject_nudge_comment (итерация 2) =================
conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
conn.execute("CREATE TABLE comments(card_id,author,body,created_at)")
gw.inject_nudge_comment(conn, "c1", "dead", "src/a.ts(1,1): error TS9999: boom")
rows = conn.execute("SELECT body FROM comments").fetchall()
check("nudge-carries-diag", rows and "TS9999" in rows[0]["body"], rows[0]["body"][:200] if rows else "no row")
gw.inject_nudge_comment(conn, "c2", "INSTALL-VIOLATION: 'npm install x'", "")
rows = conn.execute("SELECT body FROM comments WHERE card_id='c2'").fetchall()
check("nudge-install-warning", rows and "ЗАПРЕЩЕНА" in rows[0]["body"], rows[0]["body"][:200] if rows else "no row")

# ================= gateway: адаптивный поллинг (итерация 8) =================
check("adapt consts", gw.ADAPT_POLL is True and gw.ADAPT_ACTIVE_SEC == 30
      and gw.ADAPT_READY_SEC == 15 and gw.ADAPT_IDLE_SEC == 120,
      "poll=%s a=%s r=%s i=%s" % (gw.ADAPT_POLL, gw.ADAPT_ACTIVE_SEC, gw.ADAPT_READY_SEC, gw.ADAPT_IDLE_SEC))

# ================= cli: metrics (итерация 6) =================
cli_db = TD + "/cli_board.sqlite3"
os.environ["PIPELINE_HOME"] = PIPELINE_HOME
cl = load("cli_under_test", STAGE + "/cli.py")
cl.DB = cli_db
cc = sqlite3.connect(cli_db)
cc.executescript(cl.SCHEMA)
cc.commit()
cc.close()
# синтезируем: 2 done (одна с nudge), 1 blocked, гейты 3 pass / 1 fail
cl.con = lambda: (lambda c: c.__setattr__("row_factory", sqlite3.Row) or c)(sqlite3.connect(cli_db))
c2 = sqlite3.connect(cli_db)
c2.row_factory = sqlite3.Row
c2.execute("INSERT INTO cards(id,title,kind,assignee,status,retry_count,created_at,finished_at) "
           "VALUES('m1','t','task','coder','done',0,'2026-08-28 08:00:00','2026-08-28 09:00:00')")
c2.execute("INSERT INTO cards(id,title,kind,assignee,status,retry_count,created_at,finished_at) "
           "VALUES('m2','t','task','coder','done',1,'2026-08-28 08:00:00','2026-08-28 10:00:00')")
c2.execute("INSERT INTO cards(id,title,kind,assignee,status,retry_count,created_at,finished_at) "
           "VALUES('m3','t','task','coder','blocked',3,'2026-08-28 08:00:00',NULL)")
c2.execute("INSERT INTO cards(id,title,kind,assignee,status,created_at) "
           "VALUES('m4','esc','escalation','orchestrator','done','2026-08-28 08:00:00')")
for t in ("gates-passed", "gates-passed", "gates-passed", "gates-failed"):
    c2.execute("INSERT INTO events(card_id,type,payload,created_at) VALUES('m1',?,?, '2026-08-28 08:30:00')", (t, ""))
c2.execute("INSERT INTO events(card_id,type,payload,created_at) VALUES('m2','nudge','1/2: dead','2026-08-28 08:40:00')")
c2.execute("INSERT INTO events(card_id,type,payload,created_at) VALUES('m3','retry','dead; attempt 1/3','2026-08-28 08:50:00')")
c2.execute("INSERT INTO events(card_id,type,payload,created_at) VALUES('m3','blocked','escalated -> m4','2026-08-28 09:00:00')")
c2.commit()
c2.close()
import io
from contextlib import redirect_stdout
buf = io.StringIO()
with redirect_stdout(buf):
    cl.cmd_metrics(type("A", (), {})())
out = buf.getvalue()
check("metrics runs", "METRICS" in out, out[:200])
check("metrics gate-rate", "FAIL-rate=25.0%" in out, out[out.find("[GATES]"):out.find("[GATES]")+80])
check("metrics nudge", "survived to done: 1 (100%)" in out, out[out.find("[NUDGE"):out.find("[NUDGE")+120])
check("metrics cycle-time", "avg cycle time" in out and "1.5h" in out, out[out.find("[THRU"):out.find("[THRU")+100])
check("metrics escalations", "created=1 resolved=1" in out, out[out.find("[ESC"):out.find("[ESC")+60])
check("metrics death-reasons", "crash(dead)" in out, out[out.find("[DEATH"):out.find("[DEATH")+150])

# ================= ИТОГ =================
print("\n" + "=" * 50)
print("TOTAL: %d passed, %d failed" % (len(PASSED), len(FAILED)))
if FAILED:
    print("FAILED:", ", ".join(FAILED))
    sys.exit(1)
print("ALL GREEN")