# Conveyor Core — мультиагентный конвейер «Симбиоз»

Готовое ядро автономного конвейера разработки: несколько LLM-агентов
(opencode) ведут проект по kanban-доске, а детерминированный код
(гейты, TraceGuard, watchdog, fastlane) судит их работу.

**Полное описание подхода** — [`docs/CONVEYOR-METHODOLOGY.md`](docs/CONVEYOR-METHODOLOGY.md)
(концепция, архитектура, реализация, боевая статистика).

Этот репо — «как поднять»: код + роли + миграции + тесты + юниты +
bootstrap. Собран из боевого конвейера (приватный проект «Горизонт
событий», 7/7 эпиков, 61 карточка, 3 дня автономной работы).

---

## Состав

```
gateway/gateway.py            диспетчер: цикл 90с, promote/запуск/гейты/мержи,
                              nudge-экономика, loop-детектор, worktree-ферма,
                              security-гейты, декларативные гейты
gateway/watchdog.py           watchdog LLM-движка: hang/stuck/KV-трэш,
                              cooldown в БД, авто-рестарт через агента
gateway/agent-gateway.service systemd-юнит шлюза (ШАБЛОН — заполнить env)
fastlane.py                   автоприёмка review: свежие гейты, TraceGuard L1/L2,
                              чёрный список, лимит 3/прогон
kanban/cli.py                 CLI + SCHEMA (владелец структуры БД):
                              create/list/show/move/comment/mem/recall/ask
kanban/migrations/            001_symbiosis (колонки+таблицы), 002_consult_kind
roles/<role>/SOUL.md          9 профилей ролей (architect, coder, qa,
                              code-reviewer, ui-reviewer, e2e, docs,
                              controller, orchestrator)
skills/                       шаблоны: task-brief, architect-plan, gates,
                              final-report, deploy
gen_roles.py                  генератор ролей: SOUL.md + agent-файлы opencode
tests/test_wave3.py           27 изолированных тестов (рецепт изоляции внутри)
gpu-node/watchdog-agent.py    агент на GPU-ноде (:9100): /metrics + /restart
gpu-node/install-gpu-node.sh  установка агента (NOPASSWD visudo, ufw по IP)
control-node/agent-watchdog.service  юнит watchdog (ШАБЛОН — заполнить)
pulse.py                      пульс-монитор (cron */10)
shutdown-safe.sh              аккуратное остановка конвейера
PLAN-SYMBIOSIS.md             мастер-план (источник номеров слоёв B–K)
docs/CONVEYOR-METHODOLOGY.md  МЕТОДОЛОГИЯ (читать первой)
```

## Требования

| Компонент | Что нужно |
|---|---|
| ОС | Linux (systemd) |
| Python | 3.10+, только stdlib (sqlite3, fcntl, subprocess) |
| LLM-движок | vLLM с OpenAI-совместимым API (`/v1/chat/completions`, `/v1/models`) |
| Клиент агентов | [opencode](https://opencode.ai) (`~/.opencode/bin/opencode`), профили ролей |
| Проект | git-репо с `npx tsc --noEmit`, `npm run lint`, `npm run test:ci` (гейты) |
| Опционально | gitleaks (security-гейт), npm (npm audit) |

## Быстрый старт (30 минут)

### 1. Раскладка

```bash
export PIPELINE_HOME=~/agent-pipeline
export PROJECT_DIR=~/my-project          # ваш git-репо
mkdir -p $PIPELINE_HOME
cp -r conveyor-core/. $PIPELINE_HOME/
```

### 2. База данных

```bash
python3 $PIPELINE_HOME/kanban/cli.py init
python3 $PIPELINE_HOME/kanban/migrations/apply_migration.py \
  --db $PIPELINE_HOME/kanban/boards/main.sqlite3
# идемпотентно: применяет 001 (колонки+таблицы) и 002 (consult-kind)
```

### 3. Роли

```bash
python3 $PIPELINE_HOME/gen_roles.py
# создаёт roles/*/SOUL.md (уже в репо) + ~/.config/opencode/agent/*.md
```

### 4. Конфигурация (env)

Минимальный набор для шлюза:

```
PIPELINE_HOME=$PIPELINE_HOME
PIPELINE_PROJECT_DIR=$PROJECT_DIR
PIPELINE_MODEL=vllm/<ваша-модель>        # имя модели в vLLM
PIPELINE_MAX_PARALLEL=3                   # начните с 3 (KV-давление!)
```

Полный список параметров — в `docs/CONVEYOR-METHODOLOGY.md`, приложение B.

### 5. Baseline гейтов (ОБЯЗАТЕЛЬНО до первого запуска)

```bash
cd $PROJECT_DIR
# зафиксируйте текущее состояние как норму:
python3 $PIPELINE_HOME/gateway/gateway.py --refresh-sec-baseline   # если есть gitleaks
# механические baseline'ы:
#   tsc/lint/jest — см. refresh_gate_baseline() в gateway.py
#   (вызывается явно; без baseline гейты работают в строгом режиме)
```

**Без baseline** security-гейт и механические гейты блокируют всё
(fail-closed). Это осознанное решение — см. методологию, принцип P4.

### 6. GPU-нода (если vLLM на отдельной машине)

```bash
scp gpu-node/watchdog-agent.py gpu-node/install-gpu-node.sh gpu-node@host:
ssh gpu-node@host 'bash install-gpu-node.sh'   # создаёт :9100, visudo, ufw
```

Заполните `control-node/agent-watchdog.service`:
- `WATCHDOG_AGENT_URL=http://<gpu-ip>:9100`
- `WATCHDOG_TOKEN=<сгенерированный токен>`

### 7. Системные службы

```bash
sudo cp gateway/agent-gateway.service /etc/systemd/system/
sudo cp control-node/agent-watchdog.service /etc/systemd/system/
# отредактируйте env в обоих (User, WorkingDirectory, PATH, токен)
sudo systemctl daemon-reload
sudo systemctl enable --now agent-gateway agent-watchdog
```

### 8. Fastlane (автоприёмка)

```bash
(crontab -l 2>/dev/null; echo "*/5 * * * * python3 $PIPELINE_HOME/fastlane.py >> $PIPELINE_HOME/fastlane.cron.log 2>&1") | crontab -
```

### 9. Проверка

```bash
# сухой прогон (ничего не запускает):
PIPELINE_DRY_RUN=1 python3 $PIPELINE_HOME/gateway/gateway.py --once

# тесты (изолированные, не трогают боевую БД):
python3 $PIPELINE_HOME/tests/test_wave3.py

# первая карточка:
python3 $PIPELINE_HOME/kanban/cli.py create --kind task --assignee architect \
  --title "Диагностика: <проблема>" --description "<ТЗ по skills/task-brief.md>"
```

## Эксплуатация

| Операция | Команда |
|---|---|
| Пауза | `touch $PIPELINE_HOME/PAUSE` (снять: `rm`) |
| Сменить параллелизм | env в юните + `systemctl restart agent-gateway` |
| Worktree-ферма вкл/выкл | `touch/rm $PIPELINE_HOME/PLAN_B` (hot-switch) |
| Обновить security-baseline | `python3 gateway/gateway.py --refresh-sec-baseline` |
| Спросить у роли | `python3 kanban/cli.py ask <role> "<вопрос>"` |
| Безопасная остановка | `./shutdown-safe.sh` |
| Логи | `gateway.log`, `watchdog.log`, `fastlane.log`, `runs/*.log` |

## Правила, которые нельзя нарушать

1. **Холодное окно** — миграции/рестарты только при running=0, review=0.
2. **Бэкап БД** перед любым изменением схемы.
3. **Baseline обязателен** для накопительных сканеров (gitleaks/npm).
4. **MAX_PARALLEL=3** на одной карте — выше будет KV-thrash.
5. **Тесты изолированы** — см. рецепт в `tests/test_wave3.py`
   (env до импорта, ConnProxy, assert на temp-БД, stub'ы).
6. **Секреты** — только в env, никогда в коммитах. Security-гейт
   (gitleaks) ловит нарушения автоматически.

## Лицензия

Приватный проект. Распространение — по решению владельца.