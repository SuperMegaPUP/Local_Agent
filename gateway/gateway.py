#!/usr/bin/env python3
"""Gateway — диспетчер конвейера Горизонт Событий.

Запуск:
  PIPELINE_PROJECT_DIR=/home/g/gorizont-sobytij_test python3 gateway/gateway.py
  python3 gateway/gateway.py --once          # одна итерация
  PIPELINE_DRY_RUN=1 ... --once              # сухой прогон (без запуска воркеров)

Пауза: touch ~/agent-pipeline/PAUSE   (снять: rm ~/agent-pipeline/PAUSE)

Параметры (env):
  PIPELINE_MAX_PARALLEL   макс. одновременных воркеров (по умолчанию 3)
  PIPELINE_REPO_LOCK_ROLES роли, берющие эксклюзивный замок репо (coder,qa)
  PIPELINE_HEARTBEAT_MIN  минут без heartbeat -> воркер считается зависшим
"""
import fcntl, json, os, re, shutil, signal, sqlite3, subprocess, sys, time, uuid
from datetime import datetime, timezone

BASE = os.path.expanduser(os.environ.get("PIPELINE_HOME", "~/agent-pipeline"))
DB = os.path.join(BASE, "kanban", "boards", "main.sqlite3")
PROJECT_DIR = os.path.expanduser(os.environ.get("PIPELINE_PROJECT_DIR", "~/gorizont-sobytij_test"))
OPENCODE = os.path.expanduser("~/.opencode/bin/opencode")
MODEL = os.environ.get("PIPELINE_MODEL", "vllm/qwen3.8-27b")
POLL_SEC = int(os.environ.get("PIPELINE_POLL_SEC", "90"))
# Аудит 2026-08-28 (итерация 8): адаптивный поллинг — плотнее при активности,
# реже в простое. ПАУЗА распознаётся не позже ADAPT_IDLE_SEC.
ADAPT_POLL = os.environ.get("PIPELINE_ADAPT_POLL", "1") == "1"
ADAPT_ACTIVE_SEC = int(os.environ.get("PIPELINE_POLL_ACTIVE", "30"))   # есть running
ADAPT_READY_SEC = int(os.environ.get("PIPELINE_POLL_READY", "15"))    # есть ready
ADAPT_IDLE_SEC = int(os.environ.get("PIPELINE_POLL_IDLE", "120"))     # доска пуста
MAX_PARALLEL = int(os.environ.get("PIPELINE_MAX_PARALLEL", "4"))
LOCK_ROLES = set(filter(None, os.environ.get("PIPELINE_REPO_LOCK_ROLES", "coder,qa").split(",")))
HB_LIMIT_MIN = int(os.environ.get("PIPELINE_HEARTBEAT_MIN", "20"))
DRY_RUN = os.environ.get("PIPELINE_DRY_RUN", "0") == "1"
PAUSE = os.path.join(BASE, "PAUSE")
RUNDIR = os.path.join(BASE, "runs")
PIDFILE = os.path.join(RUNDIR, "workers.json")
LOCKFILE = os.path.join(RUNDIR, "repo.lock")
# ПЛАН B: worktree-ферма. Каждый writer получает свой git worktree + ветку wt/<cid>;
# центральный чекаут остаётся зоной гейтов и мержей.
# Тумблер (динамический, hot-switch без рестарта):
#   включение: touch ~/agent-pipeline/PLAN_B   (действует с следующего цикла)
#   выключение: rm ~/agent-pipeline/PLAN_B
#   env PIPELINE_WORKTREES=1/0 имеет приоритет (статический переопределитель).
PLAN_B_FLAG = os.path.join(BASE, "PLAN_B")
MERGE_TIMEOUT = int(os.environ.get("PIPELINE_MERGE_TIMEOUT", "180"))
MERGE_TRIES = int(os.environ.get("PIPELINE_MERGE_TRIES", "3"))
MERGE_RETRY_DELAY = int(os.environ.get("PIPELINE_MERGE_RETRY_DELAY", "30"))
# SIMBIOSIS W1: мягкий пинок перед жёстким retry (слой E) и loop-детектор (слой F).
MAX_NUDGES = int(os.environ.get("PIPELINE_MAX_NUDGES", "2"))   # сколько пинков дать, прежде чем тратить retry
LOOP_KILL = int(os.environ.get("PIPELINE_LOOP_KILL", "5"))    # одинаковых команд подряд в хвосте лога -> kill
LOOP_TAIL_BYTES = 64 * 1024                                   # сколько байтов хвоста лога смотреть
# SIMBIOSIS W3 (слой K): детерминированный security-скан (gitleaks + npm audit),
# baseline-aware как механические гейты. Тумблер по умолчанию ВЫКЛ.
SECURITY_SCAN_ON = os.environ.get("PIPELINE_SECURITY_SCAN", "0") == "1"
GITLEAKS_BIN = os.path.join(BASE, "tools", "gitleaks")


def worktrees_enabled():
    v = os.environ.get("PIPELINE_WORKTREES")
    if v in ("1", "0"):
        return v == "1"
    return os.path.exists(PLAN_B_FLAG)

ROLE_TEMPS = {"controller": 0.3, "architect": 0.2, "coder": 0.2, "qa": 0.2,
              "code-reviewer": 0.0, "ui-reviewer": 0.0, "e2e": 0.0, "docs": 0.3, "orchestrator": 0.2}


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ---------- SIMBIOSIS W2: слой B (декларативные required_gates) -----------
# Архитектор в корне задачи (kind='task') может задать required_gates — CSV
# ролей-гейтеров (например "code-reviewer,qa"). Перед закрытием РОДИТЕЛЯ
# (epic/task) gateway проверяет: на каждого требуемого гейтера есть дочерняя
# карточка с этим assignee в done. Нет — родитель НЕ закрывается, недостающая
# гейт-карточка создаётся (kind='task', status='todo', parent=родитель).
# Это переносит фиксированный конвейер qa->review из кода в план.

def check_required_gates(c, parent):
    """Возвращает список недостающих гейт-ролей для parent. Пусто = все на месте."""
    rg = (parent["required_gates"] or "").strip()
    if not rg or parent["kind"] not in ("epic", "task"):
        return []
    wanted = [r.strip() for r in rg.split(",") if r.strip()]
    kids = c.execute(
        "SELECT assignee, status FROM cards WHERE parent_id=?", (parent["id"],)).fetchall()
    done_roles = {k["assignee"] for k in kids if k["status"] == "done"}
    missing = [r for r in wanted if r not in done_roles]
    return missing


def ensure_missing_gates(c, parent, missing):
    """Создаёт гейт-карточки для недостающих ролей. Возвращает созданные id."""
    created = []
    for role in missing:
        gid = "t_" + uuid.uuid4().hex[:8]
        c.execute(
            """INSERT INTO cards(id,title,description,kind,assignee,status,parent_id,
                                 depends_on,timeout_minutes,max_retries,created_at)
               VALUES(?,?,?,'task',?,'todo',?,'',90,3,?)""",
            (gid, f"[gate:{role}] {parent['title'][:60]}",
             f"Декларативный гейт (симбиоз, слой B): роль {role} требуется по required_gates "
             f"родителя {parent['id']}. Выполни свой протокол и закрой карточку.",
             role, parent["id"], now()))
        ev(c, gid, "created", f"слой B: декларативный гейт {role} (из required_gates {parent['id']})")
        created.append(gid)
    return created


def log(msg):
    print(f"[{now()}] {msg}", flush=True)


def con():
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def ev(c, cid, t, p=""):
    c.execute("INSERT INTO events(card_id,type,payload,created_at) VALUES(?,?,?,?)", (cid, t, p, now()))


def get_card(c, cid):
    return c.execute("SELECT * FROM cards WHERE id=?", (cid,)).fetchone()


def load_workers():
    try:
        with open(PIDFILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_workers(ws):
    os.makedirs(RUNDIR, exist_ok=True)
    tmp = PIDFILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ws, f)
    os.rename(tmp, PIDFILE)


def kill_worker(pid_str):
    """Убивает группу процессов воркера (start_new_session=True -> своя pgid).
    Без этого 'dead'-воркер продолжает жить сиротой и писать в репо параллельно с новым."""
    try:
        pid = int(pid_str)
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        time.sleep(3)
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        log(f"[kill] воркер pid={pid} (группа) остановлен")
    except (ProcessLookupError, ValueError, OSError):
        pass


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


# ---------- SIMBIOSIS W1: слой F (loop-детектор) и слой E (soft-nudge) ----------

def detect_loop(cid):
    """Слой F: цикл в логе воркера — одна и та же команда вызвана >= LOOP_KILL раз
    подряд в хвосте лога (opencode рендерит bash-вызовы строками '$ <cmd>').
    Возвращает команду-цикл или None. Консервативно: только '$ ' префикс,
    только хвост — пропущенный цикл всё равно поймает heartbeat."""
    try:
        logs = [os.path.join(RUNDIR, f) for f in os.listdir(RUNDIR)
                if f.startswith(cid + "-") and f.endswith(".log")]
        if not logs:
            return None
        path = max(logs, key=os.path.getmtime)
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > LOOP_TAIL_BYTES:
                fh.seek(size - LOOP_TAIL_BYTES)
            data = fh.read().decode("utf-8", "replace")
        cmds = []
        for line in data.splitlines():
            s = line.strip()
            if s.startswith("$ "):
                cmds.append(re.sub(r"\s+", " ", s[2:])[:200])
        if len(cmds) < LOOP_KILL:
            return None
        last = cmds[-1]
        streak = sum(1 for cm in reversed(cmds) if cm == last)
        return last if streak >= LOOP_KILL else None
    except Exception:
        return None


def _latest_log_path(cid):
    """Путь к самому свежему логу воркера (None, если нет)."""
    try:
        logs = [os.path.join(RUNDIR, f) for f in os.listdir(RUNDIR)
                if f.startswith(cid + "-") and f.endswith(".log")]
        if not logs:
            return None
        return max(logs, key=os.path.getmtime)
    except Exception:
        return None


def extract_diag(cid, max_lines=6, max_chars=1200):
    """Аудит 2026-08-28 (итерация 2): диагностичный экстракт из хвоста лога.
    Берёт последние строки с маркерами ошибок (error/fail/exception/EACCES/ENOENT/
    ERESOLVE/non-zero exit) — бесплатно (regex, 0 LLM). Пусто, если ошибок нет."""
    path = _latest_log_path(cid)
    if not path:
        return ""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > LOOP_TAIL_BYTES:
                fh.seek(size - LOOP_TAIL_BYTES)
            data = fh.read().decode("utf-8", "replace")
    except Exception:
        return ""
    pat = re.compile(r"(error|failed|failure|exception|eresolve|enoent|eacces|"
                     r"cannot find module|syntaxerror|typeerror|referenceerror|"
                     r"segfault|core dumped|panic|nonzero|exit code [1-9])", re.I)
    errs = []
    for ln in data.splitlines():
        s = ln.strip()
        if not s or len(s) < 8:
            continue
        if pat.search(s):
            errs.append(s[:300])
    if not errs:
        return ""
    return "\n".join(errs[-max_lines:])[:max_chars]


INSTALL_CMD_RE = re.compile(
    r"(?:^\$\s*yarn\s*$"
    r"|^\$\s*(?:cd\s+\S+\s*(?:&&\s*)?)?(?:npm\s+(?:install|add|ci|i\b)"
    r"|yarn\s+(?:add|install)\b|pnpm\s+(?:add|install)\b|bun\s+add\b))"
    r"(?!.*--dry-run)", re.M)


def detect_install_violation(cid):
    """Аудит 2026-08-28 (итерация 7, вариант B): перехват npm/yarn/pnpm install
    в логе worktree-воркера. node_modules — symlink на центральный, install
    ломает ВСЕХ остальных воркеров. Возвращает команду-нарушитель или None.
    Механическая защита вместо «промпт-просьбы» (принцип P2)."""
    path = _latest_log_path(cid)
    if not path:
        return None
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > LOOP_TAIL_BYTES:
                fh.seek(size - LOOP_TAIL_BYTES)
            data = fh.read().decode("utf-8", "replace")
    except Exception:
        return None
    m = INSTALL_CMD_RE.search(data)
    return m.group(0).strip()[:200] if m else None


def adaptive_sleep_sec(nr_running, nr_ready):
    """Аудит 2026-08-28 (итерация 8): выбор интервала сна главного цикла.
    Вынесено в функцию для тестируемости (условие одобрения Wave 2:
    каждый staged-путь покрыт тестом). running>0 -> 30с (heartbeat-контроль);
    ready>0 -> 15с (быстрый старт); иначе -> 120с (простой)."""
    if not ADAPT_POLL:
        return POLL_SEC
    if nr_running:
        return ADAPT_ACTIVE_SEC
    if nr_ready:
        return ADAPT_READY_SEC
    return ADAPT_IDLE_SEC


def inject_nudge_comment(c, cid, reason, diag=""):
    """Слой E: комментарий-пинок на карточке — воркер прочитает его при перезапуске
    (протокол требует начинать с чтения карточки).
    Аудит 2026-08-28 (итерация 2): diag — извлечённые из лога последние ошибки;
    превращает абстрактный «смени подход» в конкретную целевую установку."""
    body = (f"NUDGE (gateway): предыдущая попытка завершилась: {reason}.\n"
            "Смени подход — НЕ повторяй ту же последовательность действий. "
            "Упрости шаг, изолируй проблему, проверяй инкрементально.\n"
            "Протокол: комментарий ARTIFACTS + move --status review. "
            "Не решаешь — move --status blocked с причиной.")
    if "INSTALL-VIOLATION" in reason:
        body += ("\nВажно: установка пакетов (npm install/yarn add/pnpm add) в worktree "
                 "ЗАПРЕЩЕНА — node_modules общий (symlink), ты сломаешь других воркеров. "
                 "Новая зависимость = отдельная карточка, а не самостоятельный install.\n")
    if diag:
        body += ("\nДИАГНОСТИКА (последние ошибки из лога — начни с их устранения):\n"
                 + diag + "\n")
    c.execute("INSERT INTO comments(card_id,author,body,created_at) VALUES(?,?,?,?)",
              (cid, "gateway", body, now()))


held_locks = {}  # cid -> lf (fd с flock). Должен ЖИТЬ до смерти воркера, иначе замок снимется.


def acquire_repo_lock(role):
    """Возвращает fd-файл, если замок взят, иначе None."""
    if role not in LOCK_ROLES:
        return None
    os.makedirs(RUNDIR, exist_ok=True)
    lf = open(LOCKFILE, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lf.write(str(os.getpid()))
        lf.flush()
        return lf
    except BlockingIOError:
        lf.close()
        return None


def release_repo_lock(lf):
    if lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_UN)
            lf.close()
        except Exception:
            pass


# ------------------------- ПЛАН B: worktree-ферма -------------------------

def wt_dir(cid):
    """Путь к worktree карточки: <project>_wt/<cid>."""
    return PROJECT_DIR.replace(".git", "") + "_wt/" + cid


def wt_branch(cid):
    return f"wt/{cid}"


def prepare_worktree(cid):
    """Создаёт worktree на HEAD main. Возвращает (dir, err). Идемпотентно.

    Создаёт symlink node_modules -> центральный node_modules, чтобы воркер
    мог локально гонять tsc/jest. Известный компромисс: npm install внутри
    worktree затронет общих модулей — запрещаем в промпте, арбитр — центральный гейт.
    """
    d = wt_dir(cid)
    if os.path.isdir(os.path.join(d, ".git")) or os.path.isfile(os.path.join(d, ".git")):
        return d, None
    os.makedirs(os.path.dirname(d), exist_ok=True)
    r = subprocess.run(["git", "-C", PROJECT_DIR, "worktree", "add", "-b", wt_branch(cid), d, "main"],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return None, (r.stderr or r.stdout).strip()[:300]
    nm_src = os.path.join(PROJECT_DIR, "node_modules")
    nm_dst = os.path.join(d, "node_modules")
    if os.path.isdir(nm_src) and not os.path.lexists(nm_dst):
        try:
            os.symlink(nm_src, nm_dst)
        except OSError:
            pass  # symlink не критичен: воркер сможет работать без локальных тестов
    return d, None


def cleanup_worktree(cid):
    """Удаляет worktree и ветку (после успешного мержа)."""
    d = wt_dir(cid)
    try:
        subprocess.run(["git", "-C", PROJECT_DIR, "worktree", "remove", "--force", d],
                       capture_output=True, text=True, timeout=120)
        subprocess.run(["git", "-C", PROJECT_DIR, "branch", "-D", wt_branch(cid)],
                       capture_output=True, text=True, timeout=60)
        log(f"[wt] {cid}: worktree+ветка удалены")
    except Exception as e:
        log(f"[wt] {cid}: cleanup error {repr(e)[:150]}")


def merge_worktree(cid):
    """Мержит wt/<cid> в main из центрального чекаута.

    Возвращает (ok, msg, deferred):
      ok=True  — мерж применён
      ok=False, deferred=True — центральный чекаут занят (грязный/занят другим),
                                мерж откладывается на следующий цикл (НЕ конфликт!)
      ok=False, deferred=False — настоящий конфликт (merge --abort выполнен)
    """
    st = subprocess.run(["git", "-C", PROJECT_DIR, "status", "--porcelain"],
                        capture_output=True, text=True, timeout=60)
    if st.returncode == 0 and st.stdout.strip():
        return False, "central checkout dirty (deferred)", True
    r = subprocess.run(["git", "-C", PROJECT_DIR, "merge", "--no-edit", wt_branch(cid)],
                       capture_output=True, text=True, timeout=MERGE_TIMEOUT)
    out = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()[:500]
    if r.returncode == 0:
        return True, out, False
    # конфликт: откатываем мерж, оставляем ветку для разбора
    subprocess.run(["git", "-C", PROJECT_DIR, "merge", "--abort"],
                   capture_output=True, text=True, timeout=60)
    return False, out, False


def recover_orphan_worktrees():
    """Старт: для running writer-карточек восстанавливает/проверяет worktree."""
    if not worktrees_enabled():
        return
    c = con()
    for r in c.execute("SELECT id, assignee FROM cards WHERE status='running'").fetchall():
        if r["assignee"] in LOCK_ROLES:
            d, err = prepare_worktree(r["id"])
            if d:
                log(f"[wt-restore] {r['id']}: {d}")
            else:
                log(f"[wt-restore] {r['id']}: FAIL {err}")
    c.close()


def build_prompt(card, workdir=None):
    c = con()
    desc = card["description"] or ""
    plan_hint = ""
    if card["parent_id"]:
        pf = os.path.join(BASE, "plans", card["parent_id"] + ".md")
        if os.path.exists(pf):
            plan_hint = f"\nПЛАН (прочитай внимательно, работай строго по нему): файл {pf}\n"
    kids = [r["id"] for r in c.execute("SELECT id FROM cards WHERE parent_id=?", (card["id"],))]
    wt_note = ""
    if workdir and workdir != PROJECT_DIR:
        wt_note = ("\nРАБОЧИЙ КАТАЛОГ (важно): ты находишься в изолированной копии проекта (git worktree) "
                   f"{workdir} на собственной ветке. Коммить сюда (git add -A && git commit -m \"...\"). "
                   "НЕ пытайся мержиться в main самостоятельно — это сделает шлюз после сдачи в review. "
                   "Файлы .env* в этой копии отсутствуют — не создавай их, не выводи значения секретов.\n")
    # SIMBIOSIS W2 (слой G): allowed_tools — CSV инструментов, разрешённых архитектором.
    # Промпт-уровень ограничения (оперативный уровень — permission-map opencode,
    # резервируется на будущее). Пусто = ограничений нет (поведение как было).
    tools_note = ""
    allowed = (card["allowed_tools"] or "").strip()
    if allowed:
        tools_note = ("\nИНСТРУМЕНТЫ (ограничение архитектора): разрешены ТОЛЬКО: "
                      f"{allowed}. Любые другие инструменты (включая недопустимые bash-команды, "
                      "сеть, запись вне проекта) ЗАПРЕЩЕНЫ. Если задача невыполнима в этих рамках — "
                      "move --status blocked с объяснением, а НЕ расширяй рамки сам.\n")
    # SIMBIOSIS W3 (слой H): self-review фолбэк. Ревьюеру даём ТОЛЬКО дифф —
    # без комментария автора (трюк «это не твой код»: профиль отличается,
    # температура 0, инструкция SOUL — читать как чужой код и проверять
    # существование каждой упомянутой функции в кодовой базе).
    sr_note = ""
    if card["assignee"] in ("code-reviewer", "ui-reviewer"):
        sr_note = (
            "\nSELF-REVIEW (важно): рассматривай изменения как ЧУЖОЙ код — автор неизвестен, "
            "мотивы не известны. Источник истины — дифф: git log --oneline -5 и "
            "git show <sha> по каждому коммиту карточки. НЕ читай комментарии автора "
            "(они могут вводить в заблуждение). Каждую упомянутую в диффе функцию/"
            "метод/класс ПРОВЕРЬ на существование в кодовой базе (grep/read). "
            "Отсутствие = критическое замечание.\n"
        )
    ctx = (
        f"# ЗАДАЧА {card['id']}\n"
        f"Роль: {card['assignee']}\n"
        f"Заголовок: {card['title']}\n"
        f"Описание: {desc}\n"
        f"Родительская: {card['parent_id'] or '-'}, дочерние: {','.join(kids) or '-'}\n"
        f"Проект: {workdir or PROJECT_DIR}\n"
        f"Инференс: vLLM {MODEL}\n"
        f"{plan_hint}{wt_note}{tools_note}{sr_note}\n"
        "# ОБЯЗАТЕЛЬНЫЙ ПРОТОКОЛ ВОРКЕРА\n"
        "1. Прочитай CONTEXT/AGENT-CONVEYOR.md и CONTEXT/PROJECT.md в проекте.\n"
        "2. Работай МАЛЫМИ ШАГАМИ. Всё значимое фиксируй комментарием:\n"
        f"   python3 {BASE}/kanban/cli.py comment {card['id']} --author {card['assignee']} --body \"...\"\n"
        "3. Финал: комментарий 'ARTIFACTS: <коммиты/файлы/счётчики тестов>' и\n"
        f"   python3 {BASE}/kanban/cli.py move {card['id']} --status review\n"
        "4. Не справляешься: move --status blocked + комментарий с причиной. НЕ имитируй успех.\n"
        "5. НИКОГДА не ставь своей карточке done. Не трогай чужие карточки.\n"
        "6. Работай только в каталоге проекта.\n"
        "7. ФАЙЛЫ .env*: инструмент Read их АВТО-ОТКЛОНЯЕТ (защита opencode). Работай с ними ТОЛЬКО через bash: cat/grep/sed. Никогда не выводи полные значения секретов в комментарии/лог — только имена ключей и длины.\n"
    )
    c.close()
    return ctx


def _gate_baseline_dir():
    d = os.path.join(BASE, "gates")
    os.makedirs(d, exist_ok=True)
    return d


def _load_baseline(fname):
    p = os.path.join(_gate_baseline_dir(), fname)
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return set(l.rstrip("\n") for l in f if l.strip())


def _both(r):
    """tsc/eslint/jest пишут ошибки в stderr — читаем оба потока."""
    return (r.stdout or "") + (r.stderr or "")


def _capture_current_gate_states():
    """Снимает текущее состояние гейтов. Возвращает (tsc_set, lint_set, jest_failed_suites)."""
    tsc_set, lint_set, jest_failed = set(), set(), set()
    try:
        # REVIZIA 2026-08-28 Q2: --incremental кэш type-info между прогонами.
        # Измерено на боевом проекте (tsc 5.9.3): cold 15.1s -> warm 4.7s (3.2x).
        # tsbuildinfo живёт рядом с baseline-файлами; при revert/checkout tsc сам
        # пересобирает изменившиеся файлы (hash+mtime контроль), ложных PASS нет.
        incr_args = ["--incremental", "--tsBuildInfoFile",
                     os.path.join(_gate_baseline_dir(), ".tsbuildinfo")]
        r = subprocess.run(["npx", "tsc", "--noEmit"] + incr_args,
                           cwd=PROJECT_DIR, capture_output=True, text=True, timeout=1800)
        for ln in _both(r).splitlines():
            m = re.match(r"^([^(:]+)\((\d+),\d+\): error (TS\d+)", ln.strip())
            if m:
                tsc_set.add(f"{m.group(1)}:{m.group(2)}: {m.group(3)}")
    except Exception:
        pass
    try:
        r = subprocess.run(["npm", "run", "lint"], cwd=PROJECT_DIR, capture_output=True, text=True, timeout=1800)
        for ln in _both(r).splitlines():
            m = re.match(r"^([^(:]+)\((\d+),\d+\): error ", ln.strip())
            if m:
                lint_set.add(f"{m.group(1)}:{m.group(2)}")
    except Exception:
        pass
    try:
        r = subprocess.run(["npm", "run", "test:ci"], cwd=PROJECT_DIR, capture_output=True, text=True, timeout=1800)
        for ln in _both(r).splitlines():
            if ln.startswith("FAIL "):
                jest_failed.add(ln.split()[1].rstrip(":"))
    except Exception:
        pass
    return tsc_set, lint_set, jest_failed


# ---------- SIMBIOSIS W3: слой K (security_gate, детерминированный скан) -----
# gitleaks (секреты, working-tree, --no-git) + npm audit --omit=dev (уязвимости
# рантайм-зависимостей). BASELINE-AWARE ровно как механические гейты: проходим,
# если нет НОВЫХ находок относительно baseline (gates/sec_gl.txt, gates/sec_npm.txt).
# Строгий режим (baseline отсутствует) — любые находки = FAIL. Обновление
# baseline — только явно: refresh_security_baseline() (старший/orchestrator).

def _gl_findings():
    """Fingerprints gitleaks по working-tree. (set, error_str)."""
    import tempfile
    rp = os.path.join(tempfile.mkdtemp(prefix="gl_"), "rep.json")
    try:
        r = subprocess.run(
            [GITLEAKS_BIN, "detect", "--no-git", "--source", PROJECT_DIR,
             "--report-format", "json", "--report-path", rp, "--no-banner"],
            capture_output=True, text=True, timeout=180)
        if r.returncode not in (0, 1):  # 1 = найдены утечки (штатно)
            return set(), f"gitleaks rc={r.returncode}: {_both(r)[:300]}"
        import json as _j
        with open(rp) as f:
            det = _j.load(f)
        fp = {d.get("Fingerprint") or f"{d.get('File')}:{d.get('StartLine')}:{d.get('RuleID')}"
              for d in det}
        return fp, None
    except FileNotFoundError:
        return set(), "gitleaks не найден: " + GITLEAKS_BIN
    except Exception as e:
        return set(), f"gitleaks error: {repr(e)[:200]}"


def _npm_audit_findings():
    """Уязвимости npm audit --omit=dev. (set 'pkg:severity', error_str)."""
    import json as _j
    try:
        r = subprocess.run(["npm", "audit", "--omit=dev", "--json"],
                           cwd=PROJECT_DIR, capture_output=True, text=True, timeout=180)
        d = _j.loads(r.stdout or "{}")
        out = set()
        for name, v in (d.get("vulnerabilities") or {}).items():
            out.add(f"{name}:{v.get('severity', 'unknown')}")
        return out, None
    except Exception as e:
        return set(), f"npm audit error: {repr(e)[:200]}"


def run_security_scan(card):
    """(ok, report) — security-гейт для карточки с security_gate=1."""
    gl, gl_err = _gl_findings()
    np_, np_err = _npm_audit_findings()
    base_gl = _load_baseline("sec_gl.txt")
    base_np = _load_baseline("sec_npm.txt")
    parts, ok = [], True
    if gl_err:
        ok = False
        parts.append("gitleaks: ОШИБКА СКРИПАТА — " + gl_err)
    elif base_gl is None:
        ok = ok and not gl
        parts.append(f"gitleaks: {len(gl)} находок (NO BASELINE — строгий режим)")
    else:
        new = sorted(gl - base_gl)
        ok = ok and not new
        parts.append(f"gitleaks: {len(gl)} находок (baseline {len(base_gl)}), новых: {len(new)}")
        for e in new[:10]:
            parts.append("  NEW_LEAK: " + e)
    if np_err:
        ok = False
        parts.append("npm audit: ОШИБКА СКРИПАТА — " + np_err)
    elif base_np is None:
        crit_high = {x for x in np_ if x.endswith(":critical") or x.endswith(":high")}
        ok = ok and not crit_high
        parts.append(f"npm audit: {len(np_)} уязвимостей (NO BASELINE — строгий режим: high/critical)")
    else:
        new = sorted(np_ - base_np)
        ok = ok and not new
        parts.append(f"npm audit: {len(np_)} уязвимостей (baseline {len(base_np)}), новых: {len(new)}")
        for e in new[:10]:
            parts.append("  NEW_VULN: " + e)
    return ok, "\n".join(parts)


def refresh_security_baseline():
    """Явное обновление security-baseline (старший/orchestrator)."""
    gl, gl_err = _gl_findings()
    np_, np_err = _npm_audit_findings()
    bd = _gate_baseline_dir()
    if not gl_err:
        with open(os.path.join(bd, "sec_gl.txt"), "w") as f:
            f.write("\n".join(sorted(gl)) + "\n")
    if not np_err:
        with open(os.path.join(bd, "sec_npm.txt"), "w") as f:
            f.write("\n".join(sorted(np_)) + "\n")
    log(f"[sec-baseline] refreshed: gitleaks={len(gl)}{' (ERR '+gl_err+')' if gl_err else ''}, "
        f"npm={len(np_)}{' (ERR '+np_err+')' if np_err else ''}")


def refresh_gate_baseline(force=False):
    """Сохраняет текущее состояние как baseline (вызывается явнно старшим/orchestrator)."""
    tsc_set, lint_set, jest_failed = _capture_current_gate_states()
    bd = _gate_baseline_dir()
    with open(os.path.join(bd, "baseline_tsc.txt"), "w") as f:
        f.write("\n".join(sorted(tsc_set)) + "\n")
    with open(os.path.join(bd, "baseline_lint.txt"), "w") as f:
        f.write("\n".join(sorted(lint_set)) + "\n")
    with open(os.path.join(bd, "baseline_jest.txt"), "w") as f:
        f.write("\n".join(sorted(jest_failed)) + "\n")
    log(f"[baseline] refreshed: tsc={len(tsc_set)}, lint={len(lint_set)}, jest_failed={len(jest_failed)}")


def run_gates(card):
    """Механические гейты после coder/qa: tsc + eslint + jest.

    BASELINE-AWARE: проходим, если нет НОВЫХ ошибок относительно baseline
    (~/agent-pipeline/gates/baseline_*.txt). Регрессия = новая строка в tsc/lint
    или новый падающий суит в jest. Улучшение (меньше ошибок) — тоже PASS.
    Если baseline отсутствует — гейт строгий (rc==0).
    """
    base_tsc = _load_baseline("baseline_tsc.txt")
    base_lint = _load_baseline("baseline_lint.txt")
    base_jest = _load_baseline("baseline_jest.txt")
    cur_tsc, cur_lint, cur_jest = _capture_current_gate_states()
    parts = []
    ok = True
    if base_tsc is None:
        ok = len(cur_tsc) == 0
        parts.append(f"types: {len(cur_tsc)} ошибок (NO BASELINE — строгий режим)")
    else:
        new_errs = sorted(cur_tsc - base_tsc)
        fixed = len(base_tsc - cur_tsc)
        ok = not new_errs
        parts.append(f"types: {len(cur_tsc)} ошибок (baseline {len(base_tsc)}), новых: {len(new_errs)}, исправлено: {fixed}")
        for e in new_errs[:15]:
            parts.append(f"  NEW: {e}")
    if base_lint is None:
        ok = ok and len(cur_lint) == 0
        parts.append(f"lint: {len(cur_lint)} ошибок (NO BASELINE — строгий режим)")
    else:
        new_errs = sorted(cur_lint - base_lint)
        ok = ok and not new_errs
        parts.append(f"lint: {len(cur_lint)} ошибок (baseline {len(base_lint)}), новых: {len(new_errs)}")
        for e in new_errs[:15]:
            parts.append(f"  NEW: {e}")
    if base_jest is None:
        ok = ok and not cur_jest
        parts.append(f"tests: {len(cur_jest)} падающих суитов (NO BASELINE — строгий режим)")
    else:
        new_fails = sorted(cur_jest - base_jest)
        healed = sorted(base_jest - cur_jest)
        ok = ok and not new_fails
        parts.append(f"tests: {len(cur_jest)} падающих (baseline {len(base_jest)}), новых: {len(new_fails)}, вылечено: {len(healed)}")
        for e in new_fails[:10]:
            parts.append(f"  NEW_FAIL: {e}")
    return ok, "\n".join(parts)


def launch_worker(card, workdir=None):
    """Запускает opencode-воркера в фоне. Возвращает pid или None."""
    if DRY_RUN:
        log(f"[dry-run] launch {card['id']} ({card['assignee']}): {card['title'][:50]}")
        return "dry"
    ws = load_workers()
    if card["id"] in ws and pid_alive(ws[card["id"]]):
        return ws[card["id"]]
    prompt = build_prompt(card, workdir=workdir)
    logfile = os.path.join(RUNDIR, f"{card['id']}-{ws.get(card['id'], 0)}.log")
    lf = open(logfile, "wb")
    argv = [OPENCODE, "run", "--agent", card["assignee"], "-m", MODEL, prompt]
    p = subprocess.Popen(argv, cwd=(workdir or PROJECT_DIR), stdout=lf, stderr=subprocess.STDOUT,
                         start_new_session=True)
    ws[card["id"]] = str(p.pid)
    save_workers(ws)
    log(f"[launch] {card['id']} ({card['assignee']}) pid={p.pid} dir={(workdir or PROJECT_DIR).rsplit('/', 1)[-1]} log={logfile}")
    return str(p.pid)


def iter_cycle(oneshot=False):
    c = con()
    # 0. эпики: не воркеры. Если все дети done -> эпик done.
    # SIMBIOSIS W2 (слой B): перед закрытием — декларативные required_gates.
    for r in c.execute("SELECT * FROM cards WHERE kind='epic' AND status IN ('todo','ready')"):
        kids = c.execute("SELECT status FROM cards WHERE parent_id=?", (r["id"],)).fetchall()
        if kids and all(k["status"] == "done" for k in kids):
            missing = check_required_gates(c, r)
            if missing:
                created = ensure_missing_gates(c, r, missing)
                ev(c, r["id"], "gates-blocked",
                   f"слой B: не закрыт — нужны гейты {','.join(missing)}; созданы {','.join(created)}")
                log(f"[gates] {r['id']}: HOLD — декларативные гейты {missing} (созданы {created})")
                continue
            c.execute("UPDATE cards SET status='done' WHERE id=?", (r["id"],))
            ev(c, r["id"], "done", "все дочерние карточки done")
            log(f"[epic] {r['id']} -> done (все дети выполнены)")
    c.commit()

    # 1. promote: todo -> ready, если все deps done (эпики пропускаем)
    for r in c.execute("SELECT * FROM cards WHERE status='todo' AND kind!='epic'").fetchall():
        deps = [d.strip() for d in (r["depends_on"] or "").split(",") if d.strip()]
        ok = all(get_card(c, d)["status"] == "done" for d in deps if get_card(c, d))
        if ok and r["parent_id"]:
            par = get_card(c, r["parent_id"])
            if par and par["status"] == "blocked":
                ok = False
        if ok:
            c.execute("UPDATE cards SET status='ready' WHERE id=?", (r["id"],))
            ev(c, r["id"], "ready", "gateway promote")
            log(f"[promote] {r['id']} -> ready ({r['title'][:50]})")
    c.commit()

    # 2. живые воркеры: проверить смерть/зависание
    ws = load_workers()
    # Краш-реконсилиация: карточки 'running' без записи в workers.json возникают,
    # если шлюз убили между fs-записью реестра и commit БД (рестарт посреди цикла).
    # Подбираем их с мёртвым pid -> штатный путь смерти (retry/escalation).
    for o in c.execute("SELECT id FROM cards WHERE status='running' AND kind!='epic'").fetchall():
        if o["id"] not in ws:
            ws[o["id"]] = "dead"
            log(f"[adopt] {o['id']}: running без регистрации (краш-рассинхрон) -> путь смерти")
    for cid, pid in list(ws.items()):
        card = get_card(c, cid)
        if not card or card["status"] != "running":
            del ws[cid]
            if cid in held_locks:
                release_repo_lock(held_locks.pop(cid))
            continue
        if pid == "dry":
            continue
        alive = pid_alive(pid)
        # Heartbeat = mtime лог-файла воркера (opencode пишет в него постоянно).
        # Это честная метрика: воркер жив и печатает -> не stuck, даже часами.
        hb_age = None
        try:
            logs = [os.path.join(RUNDIR, f) for f in os.listdir(RUNDIR)
                    if f.startswith(cid + "-") and f.endswith(".log")]
            if logs:
                newest = max(os.path.getmtime(p) for p in logs)
                hb_age = (time.time() - newest) / 60
        except Exception:
            hb_age = None
        if hb_age is None:
            ref = card["heartbeat"] or card["started_at"]
            if ref:
                try:
                    hb_age = (datetime.now(timezone.utc) - datetime.strptime(ref, "%Y-%m-%d %H:%M:%S")).total_seconds() / 60
                except Exception:
                    pass
        stuck = hb_age is not None and hb_age > HB_LIMIT_MIN
        # Слой F: loop-детектор — одна и та же команда >= LOOP_KILL раз в хвосте лога.
        # Циклющий воркер жив и печатает (heartbeat свежий!), обычный stuck-путь его
        # не возьмёт — тут явная сигнатура. Kill группы, дальше штатный путь смерти.
        loop_cmd = detect_loop(cid) if alive else None
        if loop_cmd:
            log(f"[loop] {cid}: обнаружен цикл '{loop_cmd[:80]}' — kill группы")
            kill_worker(pid)
            alive = False
        if not alive or stuck:
            reason = "dead" if not alive else f"stuck>{HB_LIMIT_MIN}min"
            if loop_cmd:
                reason += f"; LOOP: '{loop_cmd[:80]}'"
            # Аудит 2026-08-28 (итерация 7, вар. B): перехват install в worktree.
            # node_modules — symlink на центральный: install одного воркера ломает
            # всех остальных. Kill + специфичный nudge (механика, не промпт).
            inst = None
            if alive and worktrees_enabled() and card["assignee"] in LOCK_ROLES \
                    and os.path.isdir(wt_dir(cid)):
                inst = detect_install_violation(cid)
                if inst:
                    log(f"[install-guard] {cid}: запрещённый install в worktree: '{inst[:80]}' — kill")
                    kill_worker(pid)
                    alive = False
                    reason = "INSTALL-VIOLATION: '" + inst[:120] + "'"
            if alive:
                kill_worker(pid)  # зависший воркер жив — убиваем группу, иначе сирота пишет в репо
            # Аудит 2026-08-28 (итерация 2): диагностичный nudge — последние
            # ошибки из лога попадают в комментарий-пинок (regex, 0 LLM).
            diag = extract_diag(cid)
            rc = c.execute("SELECT COUNT(*) n FROM runs WHERE card_id=?", (cid,)).fetchone()["n"]
            nudges = card["nudged"] or 0
            if rc < card["max_retries"]:
                if nudges < MAX_NUDGES:
                    # Слой E: мягкий пинок — дешёвый перезапуск БЕЗ расхода retry.
                    # Комментарий-пинок воркер прочитает при старте (протокол: начать с чтения карточки).
                    c.execute("UPDATE cards SET status='ready', nudged=nudged+1, heartbeat=NULL WHERE id=?", (cid,))
                    inject_nudge_comment(c, cid, reason, diag)
                    ev(c, cid, "nudge", f"{nudges + 1}/{MAX_NUDGES}: {reason}")
                    log(f"[nudge] {cid}: {reason} -> ready (пинок {nudges + 1}, retry не потрачен)")
                else:
                    c.execute("UPDATE cards SET status='ready', retry_count=retry_count+1, nudged=0, heartbeat=NULL WHERE id=?", (cid,))
                    ev(c, cid, "retry", f"{reason}; attempt {rc + 1}/{card['max_retries']}")
                    log(f"[retry] {cid}: {reason} -> ready (попытка {rc + 1})")
            else:
                c.execute("UPDATE cards SET status='blocked' WHERE id=?", (cid,))
                if worktrees_enabled() and card["assignee"] in LOCK_ROLES:
                    cleanup_worktree(cid)  # карточка уходит в blocked — worktree не нужен
                esc_title = f"РАЗБОР БЛОКИРОВКИ: {card['title'][:60]}"
                import uuid
                eid = "t_" + uuid.uuid4().hex[:8]
                c.execute("""INSERT INTO cards(id,title,description,kind,assignee,status,created_at)
                              VALUES(?,?,?,?,?,'todo',?)""",
                          (eid, esc_title, f"Карточка {cid} упала {rc} раз ({reason}). Разобрать и продвинуть.",
                           "escalation", "orchestrator", now()))
                ev(c, eid, "created", f"escalation for {cid}")
                ev(c, cid, "blocked", f"escalated -> {eid}")
                log(f"[escalate] {cid} -> blocked, создана {eid}")
            del ws[cid]
            if cid in held_locks:
                release_repo_lock(held_locks.pop(cid))
    save_workers(ws)

    # 3. гейты: карточки в review -> механическая проверка.
    # ПЛАН B: writer-роли (coder/qa) сначала мержим worktree в main, потом валидируем
    # центральный чекаут. Фейл гейта -> revert мержа, карточка в todo (worktree сохраняется).
    # non-writer роли (docs/reviewer/e2e) и orchestrator работают в центральном чекауте —
    # для них мерж пропускается (guard: assignee in LOCK_ROLES), гейт валидирует main.
    # (дыра 2026-08-26: роли вне списка висели в review без штампа и блокировали этап)
    for r in c.execute("SELECT * FROM cards WHERE status='review'").fetchall():
        if DRY_RUN:
            log(f"[dry-run] gates for {r['id']}")
            continue
        wt_conflict = False
        wt_deferred = False
        if worktrees_enabled() and r["assignee"] in LOCK_ROLES:
            if os.path.isdir(wt_dir(r["id"])):
                mok, mout, deferred = merge_worktree(r["id"])
                if not mok and deferred:
                    wt_deferred = True
                    log(f"[wt] {r['id']}: мерж отложен (центральный чекаут занят) — повторим в следующем цикле")
                elif not mok:
                    wt_conflict = True
                    log(f"[wt] {r['id']}: CONFLICT при мерже: {mout[:200]}")
        if wt_deferred:
            continue  # карточка остаётся в review, мерж добьётся успеха в следующем цикле
        if wt_conflict:
            c.execute("UPDATE cards SET status='blocked' WHERE id=?", (r["id"],))
            import uuid
            eid = "t_" + uuid.uuid4().hex[:8]
            c.execute("""INSERT INTO cards(id,title,description,kind,assignee,status,created_at)
                          VALUES(?,?,?,?,?,'todo',?)""",
                      (eid, f"РАЗБОР КОНФЛИКТА МЕРЖА: {r['title'][:60]}",
                       f"Карточка {r['id']}: конфликт мержа ветки {wt_branch(r['id'])} в main. "
                       f"Разобрать вручную (merge-reconciler). Детали: {mout[:800]}",
                       "escalation", "orchestrator", now()))
            ev(c, eid, "created", f"merge-conflict for {r['id']}")
            ev(c, r["id"], "blocked", f"merge conflict escalated -> {eid}")
            log(f"[wt] {r['id']} -> blocked (конфликт мержа)")
            continue
        if r["kind"] == "consult":
            # SIMBIOSIS W3 (слой J): консультация — механический гейт (tsc/lint/jest)
            # к ответу неприменим. Лёгкий штамп: fastlane принимает consult по
            # ANSWER:-комментарию воркера (ритуал финализации).
            c.execute("INSERT INTO runs(card_id,attempt,command,exit_code,output,started_at,finished_at) VALUES(?,?,?,?,?,?,?)",
                      (r["id"], 1, "consult-light-gate", 0, "consult: механический гейт не применяется (слой J)", now(), now()))
            # ВАЖНО: ARTIFACTS-штамп НЕ ставим — fastlane принимает consult
            # только по ANSWER:-комментарию воркера (иначе автозакрытие до ответа).
            ev(c, r["id"], "gates-passed", "consult light-gate (слой J)")
            log(f"[gates] {r['id']} CONSULT light-gate PASSED")
            if worktrees_enabled() and r["assignee"] in LOCK_ROLES:
                cleanup_worktree(r["id"])
            continue
        # REVIZIA 2026-08-28 Q5: честный тайминг гейтов (было now()/now() — 0.0s,
        # overhead слоёв было не измерить). Данных для per-card overhead теперь
        # хватает: runs.started_at/finished_at по механическим гейтам.
        _gt0 = now()
        ok, report = run_gates(r)
        # SIMBIOSIS W3 (слой K): security_gate=1 — детерминированный скан
        # (gitleaks + npm audit, baseline-aware). Тумблер PIPELINE_SECURITY_SCAN.
        if ok and SECURITY_SCAN_ON and r["security_gate"]:
            sok, srep = run_security_scan(r)
            report = report + "\n[SECURITY SCAN]\n" + srep
            if not sok:
                ok = False
                log(f"[sec] {r['id']}: security-скан FAIL")
        c.execute("INSERT INTO runs(card_id,attempt,command,exit_code,output,started_at,finished_at) VALUES(?,?,?,?,?,?,?)",
                  (r["id"], 1, "mechanical-gates", 0 if ok else 1, report[:4000], _gt0, now()))
        if ok:
            c.execute("INSERT INTO comments(card_id,author,body,created_at) VALUES(?,?,?,?)",
                      (r["id"], "gateway", "ARTIFACTS: mechanical gates PASSED (tsc+eslint+jest)", now()))
            ev(c, r["id"], "gates-passed", report[:300])
            log(f"[gates] {r['id']} PASSED")
            if worktrees_enabled() and r["assignee"] in LOCK_ROLES:
                cleanup_worktree(r["id"])  # мерж применён, worktree больше не нужен
        else:
            if worktrees_enabled() and r["assignee"] in LOCK_ROLES:
                # откатываем мерж плохой ветки, main возвращается к прежнему состоянию
                rv = subprocess.run(["git", "-C", PROJECT_DIR, "revert", "-m", "1", "--no-edit", "HEAD"],
                                    capture_output=True, text=True, timeout=MERGE_TIMEOUT)
                if rv.returncode == 0:
                    log(f"[wt] {r['id']}: мерж откатан (revert), main чист")
                else:
                    log(f"[wt] {r['id']}: REVERT FAILED — требуется ручной разбор: {(rv.stderr or '')[:200]}")
            c.execute("UPDATE cards SET status='todo' WHERE id=?", (r["id"],))
            c.execute("INSERT INTO comments(card_id,author,body,created_at) VALUES(?,?,?,?)",
                      (r["id"], "gateway", "GATES FAILED, возврат в todo:\n" + report[:1500], now()))
            ev(c, r["id"], "gates-failed", report[:300])
            log(f"[gates] {r['id']} FAILED -> todo")
    c.commit()

    # 4. запуск ready-карточек с учётом параллелизма и repo-lock
    # SIMBIOSIS W1: cooldown движка (watchdog) — не запускаем НИКАКИХ воркеров,
    # пока vLLM в карантине. Активные воркеры не трогаем (они сами докрутят
    # или упрутся в heartbeat-limit); новые не стартуют.
    cd_row = c.execute(
        "SELECT reason, until FROM cooldowns WHERE key='engine' AND until > ?",
        (now(),)).fetchone()
    if cd_row:
        log(f"[cooldown] engine в карантине до {cd_row['until']} ({cd_row['reason'][:60]}) — запуск новых воркеров приостановлен")
        c.close()
        return
    running_now = [cid for cid, pid in ws.items() if pid != "dry" and pid_alive(pid)]
    slots = MAX_PARALLEL - len(running_now)
    if slots <= 0:
        c.close()
        return
    ready_rows = c.execute(
        "SELECT * FROM cards WHERE status='ready' AND kind!='epic' ORDER BY CASE assignee "
        "WHEN 'orchestrator' THEN 0 WHEN 'architect' THEN 1 ELSE 2 END, created_at").fetchall()
    locked_role_held = False
    for r in ready_rows:
        if slots <= 0:
            break
        # ПЛАН B: writer-роли получают свой worktree (параллельные coder'ы без конфликта).
        # Центральный чекаут остаётся зоной гейтов/мержей (мержи сериализованы самим
        # однопоточным циклом шлюза). Lock нужен ТОЛЬКО в fallback-режиме (общий чекаут).
        workdir = None
        if worktrees_enabled() and r["assignee"] in LOCK_ROLES:
            wd, werr = prepare_worktree(r["id"])
            if not wd:
                # не удалось создать worktree — деградируем в старый режим (общий чекаут + lock)
                log(f"[wt] {r['id']}: создание worktree не удалось ({werr}), fallback на общий чекаут")
            else:
                workdir = wd
        lf = None
        if r["assignee"] in LOCK_ROLES and workdir is None:
            lf = acquire_repo_lock(r["assignee"])
            if lf is None:
                continue  # репо занято другим writer'ом (fallback-режим)
        if lf is not None:
            locked_role_held = True
            held_locks[r["id"]] = lf  # держим fd до смерти воркера
        c.execute("UPDATE cards SET status='running', started_at=?, heartbeat=? WHERE id=?", (now(), now(), r["id"]))
        ev(c, r["id"], "started", f"attempt {r['retry_count'] + 1}" + (f" [worktree]" if workdir else ""))
        c.commit()
        pid = launch_worker(r, workdir=workdir)
        if pid:
            ws[r["id"]] = pid
            slots -= 1
        else:
            c.execute("UPDATE cards SET status='ready' WHERE id=?", (r["id"],))
            c.commit()
            if r["id"] in held_locks:
                release_repo_lock(held_locks.pop(r["id"]))
            if workdir:
                cleanup_worktree(r["id"])
    save_workers(ws)
    c.close()


def wait_and_collect(max_wait_sec=1800):
    """Режим --once: ждать завершения запущенных воркеров, затем финальный цикл сбора."""
    deadline = time.time() + max_wait_sec
    while time.time() < deadline:
        ws = load_workers()
        alive = [cid for cid, pid in ws.items() if pid != "dry" and pid_alive(pid)]
        if not alive:
            break
        time.sleep(10)
    iter_cycle()  # финальный проход: снять мёртвых, прогнать гейты


def main():
    oneshot = "--once" in sys.argv
    if "--refresh-sec-baseline" in sys.argv:
        # SIMBIOSIS W3 (слой K): явное обновление security-baseline (старший).
        refresh_security_baseline()
        return
    if not os.path.exists(DB):
        log("БД не найдена — запусти сначала: python3 kanban/cli.py init")
        sys.exit(1)
    log(f"gateway start: project={PROJECT_DIR} model={MODEL} max_parallel={MAX_PARALLEL} dry={DRY_RUN}")
    # Восстановление repo-lock после рестарта: для каждого running writer-воркера
    # занимаем замок заново (flock держится на fd процесса).
    # ПЛАН B: если у воркера есть worktree — lock НЕ восстанавливаем (он работает в
    # изоляции, lock ему не нужен; восстановление заблокировало бы fallback-writers).
    if not DRY_RUN:
        c = con()
        for r in c.execute("SELECT id, assignee FROM cards WHERE status='running'").fetchall():
            if r["assignee"] in LOCK_ROLES:
                if worktrees_enabled() and os.path.isdir(wt_dir(r["id"])):
                    log(f"[wt-restore] {r['id']}: worktree найден, lock не нужен")
                    continue
                lf = acquire_repo_lock(r["assignee"])
                if lf:
                    held_locks[r["id"]] = lf
                    log(f"[lock-restore] {r['id']} ({r['assignee']})")
        c.close()
        recover_orphan_worktrees()
    while True:
        paused = os.path.exists(PAUSE)
        try:
            if not paused:
                iter_cycle()
            else:
                log("PAUSED (файл PAUSE существует)")
        except Exception as e:
            log(f"CYCLE ERROR: {repr(e)[:300]}")
        if oneshot:
            if not DRY_RUN:
                wait_and_collect()
            break
        # Аудит 2026-08-28 (итерация 8): адаптивный интервал сна.
        # Выбор вынесен в adaptive_sleep_sec() (тестируемое ядро); здесь —
        # сбор входных данных (running/ready) с fallback на POLL_SEC.
        if ADAPT_POLL:
            try:
                cc = con()
                nr = cc.execute("SELECT COUNT(*) n FROM cards WHERE status='running' AND kind!='epic'").fetchone()["n"]
                ny = cc.execute("SELECT COUNT(*) n FROM cards WHERE status='ready' AND kind!='epic'").fetchone()["n"]
                cc.close()
                sleep_s = adaptive_sleep_sec(nr, ny)
            except Exception:
                sleep_s = POLL_SEC
        else:
            sleep_s = POLL_SEC
        time.sleep(sleep_s)


if __name__ == "__main__":
    main()