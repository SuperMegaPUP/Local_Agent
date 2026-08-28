# ПЛАН СИМБИОЗА: kanban-скелет + пер-ходовая защита (Tier 1→4)

Дата: 2026-08-26. Автор: молодой, по материалам анализа переписки (East Man / Григорий Рылов).
Статус: утверждён старшим к реализации.

## 0. Исходные позиции (проверено фактами 2026-08-26)

| Факт | Значение для плана |
|---|---|
| vLLM на отдельной машине (192.168.31.52), SSH-ключа нет, tailscale-ноды нет | **Авто-рестарт vLLM невозможен сегодня**. Watchdog делает: детекция + cooldown + алерт. Рестарт — ручной (пока) или через будущего агента на GPU-ноде (Фаза 4) |
| `http://192.168.31.52:8000/metrics` доступен с нашей ноды, есть `vllm:generation_tokens_total`, `vllm:num_requests_running`, `vllm:kv_cache_usage_perc` | **Детекция hang'а возможна чисто по HTTP-метрикам** — без nvidia-smi и без агента на GPU-ноде. Hang = requests_running>0 И счётчик токенов не растёт >120с |
| Наша схема cards: id,title,description,kind,assignee,status,parent_id,depends_on,timeout_minutes,retry_count,max_retries,created_at,started_at,finished_at,heartbeat; таблицы: events,comments,runs,memories,cards | Миграция адаптирована под нашу реальную схему (их пример предполагал другую) |
| Наш gateway ASYNC (fire-and-forget воркеры, опрос по циклам, Plan B worktree-ферма) | **Их gateway.py-фрагмент (sync-launch) НЕ переносится как файл** — переносим концепции (nudge, loop-detector, traceguard, cooldowns) в нашу архитектуру. Их код — референс поведения, не drop-in |
| Одна модель (qwen3.8-27b на vLLM), второго дешёвого верификатора нет | Семантический уровень TraceGuard (уровень 3) — плагируемый: stub сейчас, активируется при появлении VERIFY_MODEL (2-я карта / облако) |
| opencode: глобальный permission-map в ~/.config/opencode/opencode.json, роли через `--agent <role>` | allowed_tools (Слой G) — через генерацию per-card конфига opencode (нужен маленький spike) |
| Дисциплина дня: рестарты только в холодные окна, всё за тумблерами-env, регресс-тесты перед каждым патчем | Каждая фаза: тумблер ENV (по умолчанию ВЫКЛ) + тесты + применение в холодное окно + откат одной командой |

## 1. Архитектура целевого состояния

```
┌─ control-node (эта виртуалка) ────────────────────────────────┐
│ kanban SQLite (SSRoT)                                          │
│  +cooldowns +tool_calls +hang_log +failed_drafts               │
│                                                                │
│ gateway.py (async, Plan B)                                     │
│  ├─ читает cooldowns: engine в cooldown → не запускает воркеров│
│  ├─ nudge→retry→blocked экономика (Слой E)                     │
│  ├─ loop-detector по логам воркеров (Слой F)                   │
│  ├─ декларативные required_gates при закрытии эпиков (Слой B)  │
│  └─ TraceGuard L1/L2 детерминированный, L3 plug-in (Слой C)   │
│                                                                │
│ watchdog.py (новый процесс, systemd)                           │
│  ├─ поллинг /metrics vLLM каждые 15с                           │
│  ├─ hang: requests>0 ∧ Δtokens=0 >120с → cooldown 60с + алерт │
│  ├─ kv_pressure: kv_cache>0.9 >5мин → алерт (предвестник)      │
│  └─ 2+ hang/час → алерт «рестартуй vLLM» (авто — в Фазе 4)    │
│                                                                │
│ fastlane.py (cron */5) — без изменений, кроме совместимости    │
│   с новыми колонками                                            │
└────────────────────────────────────────────────────────────────┘
        ▲ HTTP :8000/metrics (read-only, уже доступно)
┌────────────────────────────────────────────────────────────────┐
│ GPU-node (192.168.31.52)                                       │
│ vLLM (qwen3.8-27b INT8, 64GB CMP 170HX)                        │
│ [Фаза 4, опционально] watchdog-agent.py :9100                  │
│   /metrics (nvidia-smi) + /restart (токен, ufw по IP)          │
└────────────────────────────────────────────────────────────────┘
```

Принцип: **детекция — наша (HTTP-метрики), исполнение — чужое (агент на GPU-ноде, позже)**.
Пока агента нет: watchdog ставит cooldown (gateway перестаёт жечь воркеров) и шлёт
алерт в Telegram — человек рестартит vLLM одной командой. Это уже закрывает 90% боли
«ночной цикл умирает на зависшем движке»: вместо N×таймаутов по всем карточкам —
один паузный период до 60с.

## 2. Миграция схемы (адаптированная под нашу реальную)

Файл: ~/agent-pipeline/kanban/migrations/001_symbiosis.sql
Применение: сначала на копии БД + прогон регресса, потом в живую в холодное окно.

```sql
ALTER TABLE cards ADD COLUMN required_gates TEXT DEFAULT '';   -- CSV тегов: "reviewer,qa,ui"
ALTER TABLE cards ADD COLUMN allowed_tools  TEXT DEFAULT '';   -- CSV: "bash,edit,read"
ALTER TABLE cards ADD COLUMN security_gate  INTEGER DEFAULT 0; -- 1 = red-team-скан перед done
ALTER TABLE cards ADD COLUMN nudged         INTEGER DEFAULT 0; -- счётчик мягких пинков

CREATE TABLE IF NOT EXISTS cooldowns (
  key    TEXT PRIMARY KEY,   -- engine:vllm | tool:<name> | worker:<card_id>
  reason TEXT,
  until  TEXT               -- UTC "%Y-%m-%d %H:%M:%S"
);
CREATE TABLE IF NOT EXISTS tool_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id TEXT, sig TEXT, result_status TEXT, ts TEXT
);
CREATE TABLE IF NOT EXISTS hang_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, action TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS failed_drafts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id TEXT, body TEXT, reason TEXT, ts TEXT
);
```

Совместимость: все новые колонки имеют DEFAULT — старый код (cli.py, fastlane.py,
gateway.py) продолжает работать без изменений. Тумблеры фаз не затрагивают схему.

## 3. Фазы реализации

### ФАЗА 1 — Надёжность (Tier 1, максимальный ROI)

#### 1.1 Watchdog движка (Слой D, адаптация)
Файл: ~/agent-pipeline/gateway/watchdog.py (новый), systemd-юnit agent-watchdog.service.
Логика (цикл 15с):
1. GET http://192.168.31.52:8000/metrics (timeout 10с).
2. Считать: `num_requests_running`, `generation_tokens_total`, `kv_cache_usage_perc`.
3. **HANG**: running>0 И Δ(tokens)=0持续 >120с → `cooldowns(engine:vllm, 60с)` +
   запись hang_log + алерт.
4. **PRESSURE** (предвестник KV-thrash, урок дня): kv>0.90 >5мин подряд → алерт
   (без cooldown — ещё не hang).
5. **UNREACHABLE**: metrics не отвечает >30с → cooldown 30с + алерт.
6. 2+ hang за час → усиленный алерт «требуется рестарт vLLM» (действие — человека).
7. Expired cooldowns удаляются автоматически.
Алерты: watchdog пишет строки `ALERT ...` в watchdog.log; существующий паттерн
(no_agent cron */5, как Proxy Watcher) доставляет новые ALERT-строки в Telegram.
ENV-тумблер: WATCHDOG_ENABLE_HANG_COOLDOWN=1 (по умолчанию 1 — это сердце фазы).

Тесты:
- unit: парсинг metrics (fixture-ответ), логика hang/pressure/expiry
- интеграция: мок-эндпоинт (python http.server) с «замороженным» счётчиком →
  watchdog ставит cooldown → gateway (DRY) не запускает воркеров
- живой: остановить vLLM (по согласованию со старшим) → алерт в TG за <60с

#### 1.2 Мягкий пинок (Слой E)
Место: секция смерти/зависания воркеров в gateway.py (путь death→retry).
Логика: воркер умер/завис, карточка НЕ в review:
```
if nudged < MAX_NUDGES(=2):
    комментарий-напоминание (протокол: ARTIFACTS + move review)
    UPDATE cards SET nudged+=1, status='ready'   # retry_count НЕ трогается
elif retry_count < max_retries:
    штатный retry (retry_count += 1)
else:
    blocked + escalation (как сейчас)
```
Reset nudged=0 при успешном прохождении в review.
ENV-тумблер: PIPELINE_SOFT_NUDGE=1 (по умолчанию 0 до применения фазы).
Тесты: unit на экономику (nudge×2 → retry×N → blocked), интеграция с фейковым
opencode, который умирает без move (расширение test_plan_b_it.py).

#### 1.3 Loop-детектор (Слой F)
Место: секция проверки живых воркеров (каждый цикл).
Логика: для каждого живого воркера читать хвост runs/t_X-N.log (последние ~200 строк),
строить сигнатуры команд (normalize: убрать пути/цифры), искать:
- одна и та же сигнатура ≥4 раза подряд → LOOP: kill процесса, событие loop_detected,
  путь смерти (nudge/retry по экономике 1.2)
- одна и та же ошибка (stderr-сигнатура) ≥5 раз → ESCALATE (blocked + escalation-карточка)
Таблица tool_calls — опциональный аудит-лог (запись сигнатур), не обязателен для MVP.
ENV-тумблер: PIPELINE_LOOP_DETECT=1.
Тесты: fixture-логи (цикл/ошибка/норма), интеграция: фейковый opencode, пишущий
одну и ту же команду в лог → kill за <2 циклов.

**Применение Фазы 1**: холодное окно (running=0), миграция + юниты + тумблеры в юнит
gateway. Откат: тумблеры=0 (код инертен), watchdog — systemctl disable.

### ФАЗА 2 — Гибкость (Tier 2)

#### 2.1 Декларативные required_gates (Слой B)
Место: секция закрытия эпиков (iter_cycle шаг 0) + promote.
Логика: эпик закрывается, если (а) все дети done И (б) для каждого тега из
required_gates родителя найдена дочерняя (любая глубина) done-карточка с этим
assignee. Нарушение (б) → эпик НЕ закрывается + авто-создание недостающей
карточки (kind=task, assignee=тег, parent=эпик, заголовок «ГЕЙТ: <тег> для <эпик>»)
+ событие gate-auto-created.
Architect-промпт (roles/architect): новые поля в плане — required_gates на корне
эпика, allowed_tools на рабочих карточках.
ENV-тумблер: PIPELINE_DECL_GATES=1.
Тесты: unit (эпик с required_gates=reviewer без reviewer-детей не закрывается,
авто-карточка создана), интеграция на песочнице.

#### 2.2 TraceGuard L1+L2 (Слой C, детерминированная часть)
Место: гейт-секция (перед/после механических гейтов).
Уровень 1 (cross-reference): парсить ARTIFACTS-комментарий (commits SHA, файлы):
- каждый заявленный commit существует в main (git cat-file -e)
- каждый заявленный файл существует в central checkout
Уровень 2 (source-count): если заявлен счётчик тестов — прогнать
`npx jest --silent 2>&1 | tail` и сравнить (tolerance: заявленное ≤ реального
и Δ объяснимо пресуществующими фейлами из baseline).
FAIL → карточка в todo + комментарий с деталями (как сейчас gates-failed).
ENV-тумблер: PIPELINE_TRACEGUARD=1.
Тесты: unit парсера ARTIFACTS (форматы наших воркеров), интеграция: карточка с
выдуманным коммитом → FAIL.

#### 2.3 allowed_tools per-card (Слой G)
Spike (0.5 дня): проверить, поддерживает ли opencode per-invocation конфиг
(`--config` / OPENCODE_CONFIG env) с permission-map. Варианты:
(a) генерация временного opencode.json per launch (копия base + фильтр permission
по allowed_tools) — надёжно, немного медленнее старт;
(b) per-agent permission в ~/.config/opencode/agent/<role>.json — если поддерживается,
проще, но ограничение на роль, не на карточку.
Выбираем по результату spike. ENV-тумблер: PIPELINE_ALLOWED_TOOLS=1.
Тесты: воркер с allowed_tools="read" пытается bash → отказ (проверка по логу).

**Применение Фазы 2**: холодное окно, тумблеры по одному (B → C → G), каждый с
прогоном регресса.

### ФАЗА 3 — Обогащение (Tier 3)

#### 3.1 Self-review фолбэк (Слой H) — 1 час ✅ РЕАЛИЗОВАНО (2026-08-28)
SOUL code-reviewer: инструкция «пересматривай как чужой код, проверяй существование
каждой функции из диффа в кодовой базе». Gateway: в промпт ревьюера — только дифф,
без комментария автора (трюк «не говори, что это его код»).
Тест: fixture-дифф с несуществующей функцией → ревьюер её ловит.
РЕАЛИЗАЦИЯ: блок SELF-REVIEW в build_prompt (code-reviewer/ui-reviewer): источник
истины = дифф (git log/show), комментарии автора запрещены к чтению, каждая
упомянутая функция проверяется на существование. SOUL.md — текст подготовлен,
применение ждёт одобрения старшего (protected file). Тесты: 3 PASS (test_wave3.py).

#### 3.2 ask_role (Слой J) — 2 часа ✅ РЕАЛИЗОВАНО (2026-08-28)
cli.py: `ask <role> <вопрос>` → карточка kind=consult, assignee=роль, без декомпозии;
воркер отвечает комментарием + move review; fastlane закрывает (consult не требует
гейтов — исключить из механической валидации, достаточно ARTIFACTS-ответа).
Тест: сквозной ask architect на песочнице.
РЕАЛИЗАЦИЯ: `kb ask <role> "<вопрос>" [--parent]` (роли динамически из roles/);
kind='consult' (миграция 002 — rebuild таблицы, проверена на scratch); timeout 45м;
gateway: consult получает LIGHT-GATE (gates-passed без tsc/lint/jest, БЕЗ
ARTIFACTS-штампа — иначе автозакрытие до ответа); fastlane: приёмка consult по
ANSWER:-комментарию именно воркера (author=assignee). Тесты: 11 PASS.

#### 3.3 security_gate (Слой K) —半天 ✅ РЕАЛИЗОВАНО (2026-08-28)
Детерминированный сканер (не LLM): gitleaks (секреты, один бинарник) +
npm audit --omit=dev (уже в CI) + eslint-security-правила (если есть).
Запуск: при security_gate=1 на карточке — дополнительный шаг гейта перед done;
finding HIGH/CRITICAL → gates-failed.
Установка: gitleaks бинаром в ~/agent-pipeline/tools/.
Тест: fixture с закоммиченным секретом → FAIL.
РЕАЛИЗАЦИЯ: gitleaks v8.30.1 в ~/agent-pipeline/tools/; run_security_scan() —
BASELINE-AWARE (gates/sec_gl.txt, sec_npm.txt) ровно как механические гейты:
строгий режим без baseline, новые находки против baseline = FAIL, ошибка сканера
= FAIL (fail-closed). Тумблер PIPELINE_SECURITY_SCAN (по умолчанию 0). Обновление
baseline: `gateway.py --refresh-sec-baseline`. Факты боевого проекта: gitleaks
(working-tree) = 38 находок в .env.*, npm audit = 15 (9 high) — baseline ОБЯЗАТЕЛЕН.
Тесты: 8 PASS (unit + интеграция).

#### 3.4 TraceGuard L3 (семантический верификатор) — stub
Конфиг PIPELINE_VERIFY_MODEL (пусто сейчас). Когда появится вторая модель
(2-я карта: Qwen2.5-7B или облако) — реализовать: дешёвый LLM-вызов сверяет
черновик с фактическими действиями (из events/tool_calls), SOFT_FAIL → nudge
(экономика Фазы 1), повтор → FAIL. Плохой черновик → failed_drafts (не в comments).
Сейчас: только таблица failed_drafts + интерфейс (функция-заглушка с логированием).

#### 3.5 Vision для ui-reviewer (Слой I) — отложено до VLM
Stub в SOUL. Активация: установка Qwen2-VL (2-я карта) → img2text + pixelmatch diff.

**Применение Фазы 3**: элементы независимы — по одному, каждый с тестом, без
глобального окна (кроме 3.3 — трогает гейт-секцию).

### ФАЗА 4 — Масштаб (привязано к железу/доступу)

#### 4.1 watchdog-agent на GPU-ноде (авто-рестарт vLLM)
Требуется от старшего: доступ на GPU-ноду (scp файла + права).
- watchdog-agent.py (:9100, токен, ufw allow from <control-ip>): /metrics
  (nvidia-smi + health) и /restart (systemctl, NOPASSWD-visudo только для vllm)
- watchdog.py (control-node) переключается на агента: 2+ hang/час → POST /restart
- До этого момента: алерт в Telegram + ручной рестарт (одна команда)

#### 4.2 Cloud fallback (Слой L) — опционально
Пулы ключей + cooldown'ы для тяжёлых ролей при недоступности локалки. Только если
появится потребность (ночные циклы без дежурства человека).

#### 4.3 MAX_PARALLEL 5+ (после 2-й карты, TP=2)
Уже запланировано ранее; watchdog PRESSURE-алерт (1.1) станет главным индикатором
безопасного уровня параллелизма.

## 4. Регресс-база (расширение существующей)

| Тест | Файл | Что охраняет |
|---|---|---|
| Unit worktree | /tmp/test_plan_b.py → перенести в ~/agent-pipeline/tests/ | мерж/конфликт/cleanup |
| IT жизненный цикл | /tmp/test_plan_b_it.py → ditto | параллельные coder'ы, гейты |
| Crash-adopt | /tmp/test_crash_adopt.py → ditto | краш-рассинхрон |
| **Ново**: watchdog | tests/test_watchdog.py | hang/pressure/cooldown/expiry |
| **Ново**: nudge-экономика | tests/test_nudge.py | nudge→retry→blocked |
| **Ново**: loop-detector | tests/test_loop_detect.py | kill зацикленного |
| **Ново**: decl-gates | tests/test_decl_gates.py | авто-гейт-карточки |
| **Ново**: traceguard | tests/test_traceguard.py | выдуманные коммиты/файлы |

Перенос тестов из /tmp в ~/agent-pipeline/tests/ — первая задача Фазы 1
(они сейчас живут во временном каталоге — риск потери).

## 5. Порядок работ и оценка

| Шаг | Содержание | Оценка | Окно |
|---|---|---|---|
| 1.0 | Перенос тестовой базы в ~/agent-pipeline/tests/ + green | 1ч | любое |
| 1.1 | Миграция 001 (копия→живая) | 1ч | холодное |
| 1.2 | watchdog.py + юнит + алерт-cron | 4ч | любое (не трогает gateway) |
| 1.3 | Nudge-экономика в gateway + тесты | 3ч | холодное |
| 1.4 | Loop-детектор + тесты | 3ч | холодное |
| 1.5 | Применение Фазы 1 (тумблеры, регресс) | 1ч | холодное |
| 2.1 | required_gates + тесты | 3ч | холодное |
| 2.2 | TraceGuard L1/L2 + тесты | 4ч | холодное |
| 2.3 | Spike allowed_tools → реализация | 4ч | любое/холодное |
| 3.x | H/J/K/L3/I — по элементам | 2-4ч каждый | по готовности |
| 4.x | По железу/доступу | — | — |

Итого до конца Фазы 2: ~24ч работы (3-4 вечера по нашей схеме «вечером докрутили»).

## 6. Риски и меры

| Риск | Мера |
|---|---|
| Положить рабочий конвейер | каждая фаза за ENV-тумблером (по умолч. выкл.), применение в холодное окно, откат = тумблер 0; бэкап gateway.py перед каждым патчем (*.bak-pre<N>) |
| Watchdog ложно срабатывает (cooldown зря) | пороги консервативны (Δ=0 >120с при running>0); PRESSURE-алерты без cooldown; hang_log для аудита; первый месяц — алерты без cooldown (наблюдательный режим), потом включаем |
| Loop-detector убивает легитимный повтор (например, polling-скрипт воркера) | порог 4× подряд одной сигнатуры; whitelist сигнатур в конфиге; сначала observe-режим (лог без kill) |
| Миграция ломает cli/fastlane | все колонки с DEFAULT; прогон на копии + полный регресс до живой |
| Их код-референс тянет на sync-архитектуру | принципиально: переносим концепции в наш async-дизайн, их файлы не применяем |
| SQLite-лок contention (урок дня: fastlane падал) | watchdog — отдельные короткие транзакции; все новые участники пишут с timeout=15 + ретраями (fastlane уже починен) |

## 7. Открытые вопросы к старшему

1. **Алерты watchdog**: Telegram (рекомендую, канал есть) или достаточно файла?
2. **Наблюдательный режим watchdog**: первый месяц только алерты без cooldown
   (рекомендую) или сразу с cooldown?
3. **Фаза 4.1** (агент на GPU-ноде): готов ли ты дать scp/root-доступ на 192.168.31.52
   для установки watchdog-agent? Без него авто-рестарт vLLM невозможен.
4. **Приоритет внутри Фазы 2**: сначала декларативные гейты (гибкость) или
   TraceGuard (качество)? Рекомендую TraceGuard — он защищает от главного риска
   (выдуманные артефакты), а гейты — удобство.

## 8. Что НЕ делаем (осознанно)

- Не переносим их sync-gateway как файл (наш async + Plan B сильнее).
- Не делаем PendingTurn (решает проблему stateless-API tool-call'ов — у нас
  opencode держит сессию, проблема не наша).
- Не делаем пул API-ключей (нет облачных моделей в проде).
- Не делаем img2text до установки VLM.
- Не трогаем fastlane-логику (работает, починен на ретраях).