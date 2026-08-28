#!/usr/bin/env python3
"""SIMBIOSIS Wave 3 — изолированные тесты слоёв H/J/K.

РЕЦЕПТ ИЗОЛЯЦИИ (урок инцидента 2026-08-27):
  1. env ДО импорта модулей (gateway читает env при импорте).
  2. gw.con = lambda: KA(file-db) с no-op close.
  3. fastlane: monkeypatch fl.DB/fl.LOG/fl.PROJECT_DIR после импорта.
  4. assert gw.DB.startswith(td) сразу после импорта.
  5. gw.launch_worker заглушен — реальные воркеры физически невозможны.
Боевая БД и репо НЕ трогаются ни одним путём.
"""
import importlib.util
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime

TD = tempfile.mkdtemp(prefix="w3test_")
PROJ = os.path.join(TD, "proj")
ROLES = os.path.join(TD, "roles")
DBPATH = os.path.join(TD, "kanban", "boards", "main.sqlite3")
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("  | " + str(detail)[:160]) if detail and not cond else ""))


class FakeCard(dict):
    def __getitem__(self, k):
        return super().__getitem__(k)


class ConnProxy:
    """Обёртка: no-op close (iter_cycle в конце вызывает c.close()),
    остальные атрибуты делегируются настоящему соединению."""
    def __init__(self, conn):
        self._conn = conn
    def close(self):
        pass  # intentionally no-op: соединение живо для следующих циклов теста
    def __getattr__(self, name):
        return getattr(self._conn, name)


def KA(db=DBPATH):
    c = sqlite3.connect(db, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return ConnProxy(c)


def load_mod(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def seed_db():
    os.makedirs(os.path.dirname(DBPATH), exist_ok=True)
    os.makedirs(os.path.join(TD, "runs"), exist_ok=True)
    os.makedirs(PROJ, exist_ok=True)
    shutil.copytree("/home/g/agent-pipeline/roles", ROLES)
    clib = load_mod("clib_seed", "/home/g/agent-pipeline/kanban/cli.py")
    c = sqlite3.connect(DBPATH)
    c.executescript(clib.SCHEMA)
    c.executescript(open("/home/g/agent-pipeline/kanban/migrations/001_symbiosis.sql").read())
    c.executescript(open("/home/g/agent-pipeline/kanban/migrations/002_consult_kind.sql").read())
    c.commit()
    c.close()


def utcnow_str():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def ins_card(**kw):
    d = dict(id="t_x", title="x", description="", kind="task", assignee="coder",
             status="todo", parent_id=None, depends_on="", timeout_minutes=90,
             retry_count=0, max_retries=3, created_at=utcnow_str(), started_at=None,
             finished_at=None, heartbeat=None, required_gates="", allowed_tools="",
             security_gate=0, nudged=0)
    d.update(kw)
    c = KA()
    c.execute("""INSERT INTO cards(id,title,description,kind,assignee,status,parent_id,depends_on,
                 timeout_minutes,retry_count,max_retries,created_at,started_at,finished_at,heartbeat,
                 required_gates,allowed_tools,security_gate,nudged)
                 VALUES(:id,:title,:description,:kind,:assignee,:status,:parent_id,:depends_on,
                 :timeout_minutes,:retry_count,:max_retries,:created_at,:started_at,:finished_at,
                 :heartbeat,:required_gates,:allowed_tools,:security_gate,:nudged)""", d)
    c.commit()
    return d["id"]


def ins_event(cid, typ, payload=""):
    c = KA()
    c.execute("INSERT INTO events(card_id,type,payload,created_at) VALUES(?,?,?,?)",
              (cid, typ, payload, utcnow_str()))
    c.commit()


def ins_comment(cid, author, body):
    c = KA()
    c.execute("INSERT INTO comments(card_id,author,body,created_at) VALUES(?,?,?,?)",
              (cid, author, body, utcnow_str()))
    c.commit()


def get(cid, col="status"):
    c = KA()
    r = c.execute(f"SELECT {col} FROM cards WHERE id=?", (cid,)).fetchone()
    return r[col] if r else None


def events_of(cid):
    c = KA()
    return [e["type"] for e in c.execute("SELECT type FROM events WHERE card_id=? ORDER BY id", (cid,))]


def comments_of(cid, author=None):
    c = KA()
    q = "SELECT body FROM comments WHERE card_id=?"
    p = [cid]
    if author:
        q += " AND author=?"
        p.append(author)
    return [r["body"] for r in c.execute(q, p)]


seed_db()

# ---- импорт gateway С ПОСЛЕДУЮЩИМ ПАТЧЕМ (env уже стоит) --------------------
os.environ.update({
    "PIPELINE_HOME": TD,
    "PIPELINE_PROJECT_DIR": PROJ,
    "PIPELINE_DRY_RUN": "0",
    "PIPELINE_WORKTREES": "0",
    "PIPELINE_SECURITY_SCAN": "1",
    "PIPELINE_MAX_PARALLEL": "3",
})
gw = load_mod("gw", "/home/g/agent-pipeline/gateway/gateway.py")
check("iso: gw.DB в tempdir", gw.DB.startswith(TD), gw.DB)
gw.con = lambda: KA()
LAUNCH_CALLS = []
gw.launch_worker = lambda card, workdir=None: LAUNCH_CALLS.append(card["id"]) or "stubpid"
gw.MAX_PARALLEL = 0  # страховка: слотов нет — запуск невозможен даже при ошибке логики

# ============================ СЛОЙ H: self-review =============================
cr = FakeCard(id="t_sr1", title="[gate:code-reviewer] X", description="review",
              parent_id=None, allowed_tools="", assignee="code-reviewer")
cod = FakeCard(id="t_sr2", title="fix", description="d", parent_id=None,
               allowed_tools="", assignee="coder")
p_rev = gw.build_prompt(cr)
p_cod = gw.build_prompt(cod)
check("H: промпт ревьюера содержит SELF-REVIEW", "SELF-REVIEW" in p_rev)
check("H: промпт ревьюера — запрет читать комментарии автора", "НЕ читай комментарии автора" in p_rev)
check("H: промпт кодера БЕЗ SELF-REVIEW", "SELF-REVIEW" not in p_cod)

# ===================== СЛОЙ J: cli ask ========================================
env_cli = dict(os.environ)
r = subprocess.run(["python3", "/home/g/agent-pipeline/kanban/cli.py", "ask", "architect",
                    "Какой стек у проекта?"], capture_output=True, text=True, env=env_cli)
cid_ask = r.stdout.strip()
check("J: kb ask создал карточку", r.returncode == 0 and cid_ask.startswith("t_"), r.stdout + r.stderr)
check("J: kind=consult", get(cid_ask, "kind") == "consult", get(cid_ask, "kind"))
check("J: assignee=architect", get(cid_ask, "assignee") == "architect")
check("J: timeout 45 мин", get(cid_ask, "timeout_minutes") == 45)
r_bad = subprocess.run(["python3", "/home/g/agent-pipeline/kanban/cli.py", "ask", "wizard", "hi"],
                       capture_output=True, text=True, env=env_cli)
check("J: неизвестная роль отклонена", r_bad.returncode == 1 and "неизвестная роль" in r_bad.stdout,
      r_bad.stdout)

# ===================== СЛОЙ J: gateway light-gate ==============================
qc = ins_card(id="t_consult1", title="[ask:architect] Какой стек?", kind="consult",
              assignee="architect", status="review")
gw.iter_cycle()
check("J: consult получил gates-passed (light-gate)", "gates-passed" in events_of(qc), events_of(qc))
check("J: consult остался в review (ждём ANSWER)", get(qc) == "review", get(qc))
gw_art = [b for b in comments_of(qc, "gateway") if "ARTIFACTS" in b]
check("J: gateway НЕ поставил ARTIFACTS-штамп", not gw_art, gw_art)
check("J: реальные воркеры не запускались", not LAUNCH_CALLS, LAUNCH_CALLS)

# ===================== СЛОЙ J: fastlane приёмка ================================
fl = load_mod("fl", "/home/g/agent-pipeline/fastlane.py")
fl.DB = DBPATH
fl.LOG = os.path.join(TD, "fastlane.log")
fl.PROJECT_DIR = PROJ

# A: consult + ANSWER от воркера -> автозакрытие
qa = ins_card(id="t_consultA", title="[ask:architect] Какой стек?", kind="consult",
              assignee="architect", status="review")
ins_event(qa, "gates-passed", "consult light-gate (слой J)")
ins_comment(qa, "architect", "ANSWER: Стек — Next.js 16 + Prisma (CONTEXT/STACK.md:12).")
fl._run_once(datetime.utcnow(), dry=False)
check("J-fl: consult с ANSWER закрыт", get(qa) == "done", get(qa))

# B: consult без ANSWER -> hold
qb = ins_card(id="t_consultB", title="[ask:architect] Второй вопрос?", kind="consult",
              assignee="architect", status="review")
ins_event(qb, "gates-passed", "consult light-gate (слой J)")
fl._run_once(datetime.utcnow(), dry=False)
check("J-fl: consult без ANSWER в review (hold)", get(qb) == "review", get(qb))

# C: ANSWER от постороннего автора -> hold
qc2 = ins_card(id="t_consultC", title="[ask:architect] Третий вопрос?", kind="consult",
               assignee="architect", status="review")
ins_event(qc2, "gates-passed", "consult light-gate (слой J)")
ins_comment(qc2, "gateway", "ANSWER: поддельный ответ не того автора.")
fl._run_once(datetime.utcnow(), dry=False)
check("J-fl: ANSWER не от воркера -> hold", get(qc2) == "review", get(qc2))

# D: регрессия — обычный task с ARTIFACTS закрывается как раньше
qt = ins_card(id="t_taskreg", title="fix button", kind="task", assignee="coder", status="review")
ins_event(qt, "gates-passed", "tsc+eslint+jest ok")
ins_comment(qt, "coder", "ARTIFACTS: файл src/x.ts")
fl._run_once(datetime.utcnow(), dry=False)
check("J-fl: регрессия — task с ARTIFACTS закрывается", get(qt) == "done", get(qt))

# ===================== СЛОЙ K: security-скан ===================================
# unit: строгий режим (нет baseline)
gw.GITLEAKS_BIN = "/nonexistent/gl"  # не важно — findings мокаем
gw._gl_findings = lambda: ({"fp_secret_1"}, None)
gw._npm_audit_findings = lambda: ({"lodash:high"}, None)
ok, rep = gw.run_security_scan(FakeCard(security_gate=1))
check("K: строгий режим — находки = FAIL", not ok, rep)
check("K: отчёт содержит gitleaks+audit", "gitleaks:" in rep and "npm audit:" in rep, rep)

# unit: baseline совпадает -> PASS
bd = os.path.join(TD, "gates")
os.makedirs(bd, exist_ok=True)
open(os.path.join(bd, "sec_gl.txt"), "w").write("fp_secret_1\n")
open(os.path.join(bd, "sec_npm.txt"), "w").write("lodash:high\n")
ok, rep = gw.run_security_scan(FakeCard(security_gate=1))
check("K: baseline совпадает -> PASS", ok, rep)

# unit: новая находка -> FAIL
open(os.path.join(bd, "sec_gl.txt"), "w").write("fp_old\n")
ok, rep = gw.run_security_scan(FakeCard(security_gate=1))
check("K: новая утечка против baseline -> FAIL", not ok and "NEW_LEAK" in rep, rep)

# unit: ошибка скрипта -> FAIL (fail-closed)
gw._gl_findings = lambda: (set(), "boom")
ok, rep = gw.run_security_scan(FakeCard(security_gate=1))
check("K: ошибка сканера -> FAIL (fail-closed)", not ok and "ОШИБКА" in rep, rep)
gw._gl_findings = lambda: ({"fp_secret_1"}, None)
open(os.path.join(bd, "sec_gl.txt"), "w").write("fp_secret_1\n")

# интеграция: security_gate=1 + новая находка -> gates-failed, карточка в todo
SCAN_CALLED = []
orig_run_gates = gw.run_gates
gw.run_gates = lambda card: (True, "mock mechanical ok")
gw._gl_findings = lambda: ({"fp_NEW_leak"}, None)
qs = ins_card(id="t_seccard", title="feat login", kind="task", assignee="coder",
              status="review", security_gate=1)
gw.iter_cycle()
check("K-int: security_gate=1 + новая утечка -> todo", get(qs) == "todo", get(qs))
check("K-int: событие gates-failed", "gates-failed" in events_of(qs), events_of(qs))
gc = [b for b in comments_of(qs, "gateway") if "SECURITY SCAN" in b]
check("K-int: комментарий с деталями скана", gc, gc)

# интеграция: security_gate=0 -> скан не вызывается
CALLS = []
gw._gl_findings = lambda: (CALLS.append(1) or set(), None)
qn = ins_card(id="t_noseccard", title="feat btn", kind="task", assignee="coder",
              status="review", security_gate=0)
gw.iter_cycle()
check("K-int: security_gate=0 -> скан не вызывается", not CALLS, CALLS)
check("K-int: обычная карточка прошла гейт (mock)", get(qn) == "review", get(qn))
gw.run_gates = orig_run_gates

print("\n===== ИТОГ =====")
print(f"PASS: {len(PASS)}  FAIL: {len(FAIL)}")
if FAIL:
    print("провалились:", FAIL)
    sys.exit(1)
print("ALL GREEN")