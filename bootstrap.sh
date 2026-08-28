#!/usr/bin/env bash
# bootstrap.sh — развёртывание конвейера на новой машине.
#
# Использование:
#   ./bootstrap.sh <PIPELINE_HOME> <PROJECT_DIR> <MODEL_NAME>
# Пример:
#   ./bootstrap.sh ~/agent-pipeline ~/my-project vllm/qwen3.8-27b
#
# Шаги: раскладка файлов, init БД, миграции, генерация ролей,
#       служебные каталоги, напоминание о baseline и юнитах.
# НЕ устанавливает systemd-юниты (нужны sudo + ваши пути) —
# показывает готовые к установке файлы.
set -euo pipefail

PH="${1:?PIPELINE_HOME, напр. ~/agent-pipeline}"
PD="${2:?PROJECT_DIR, напр. ~/my-project}"
MODEL="${3:?MODEL, напр. vllm/qwen3.8-27b}"
HERE="$(cd "$(dirname "$0")" && pwd)"

expand() { eval echo "$1"; }
PH="$(expand "$PH")"
PD="$(expand "$PD")"

echo "==> PIPELINE_HOME=$PH"
echo "==> PROJECT_DIR=$PD"
echo "==> MODEL=$MODEL"

# 0. проверки
command -v python3 >/dev/null || { echo "НЕТ python3"; exit 1; }
[ -d "$PD/.git" ] || { echo "PROJECT_DIR не git-репо: $PD"; exit 1; }
OPENCODE="$HOME/.opencode/bin/opencode"
[ -x "$OPENCODE" ] || echo "!! opencode не найден в $OPENCODE (нужен для запуска воркеров)"

# 1. раскладка
echo "==> раскладываю файлы..."
mkdir -p "$PH"/{kanban/boards,plans,runs,gates,backups,reports}
cp -r "$HERE"/. "$PH"/
rm -f "$PH/bootstrap.sh"

# 2. база данных
echo "==> init БД..."
python3 "$PH/kanban/cli.py" init
echo "==> миграции..."
python3 "$PH/kanban/migrations/apply_migration.py" --db "$PH/kanban/boards/main.sqlite3"

# 3. роли
echo "==> генерация ролей (SOUL.md + opencode-агенты)..."
python3 "$PH/gen_roles.py"

# 4. служебные файлы
touch "$PH/PLAN_B"   # worktree-ферма включена по умолчанию
echo "==> PLAN_B включён (worktree-ферма)"

# 5. baseline гейтов
echo "==> baseline механических гейтов (tsc/lint/jest)..."
(cd "$PD" && PIPELINE_HOME="$PH" PIPELINE_PROJECT_DIR="$PD" \
  python3 -c "
import sys; sys.path.insert(0, '$PH/gateway')
import gateway
gateway.refresh_gate_baseline()
") || echo "!! baseline не получился — проверьте, что в $PD есть npx tsc / npm run lint / npm run test:ci"

if [ -x "$PH/tools/gitleaks" ]; then
  echo "==> baseline security (gitleaks + npm audit)..."
  (cd "$PD" && PIPELINE_HOME="$PH" PIPELINE_PROJECT_DIR="$PD" \
    python3 "$PH/gateway/gateway.py" --refresh-sec-baseline)
else
  echo "!! gitleaks не установлен — security-гейт (PIPELINE_SECURITY_SCAN) будет fail-closed."
  echo "   Установка: скачать бинарник в $PH/tools/gitleaks (см. docs/CONVEYOR-METHODOLOGY.md, слой K)"
fi

# 6. итог
cat <<EOF

================ ГОТОВО ================
Конвейер развёрнут в $PH

Осталось вручную (нужны sudo / ваши решения):
  1. systemd-юниты:
       sudo cp $PH/gateway/agent-gateway.service /etc/systemd/system/
       sudo cp $PH/control-node/agent-watchdog.service /etc/systemd/system/
     отредактируйте: User, WorkingDirectory, PATH,
     PIPELINE_PROJECT_DIR=$PD, PIPELINE_MODEL=$MODEL,
     WATCHDOG_AGENT_URL/WATCHDOG_TOKEN (если vLLM на другой машине)
     sudo systemctl daemon-reload && sudo systemctl enable --now agent-gateway agent-watchdog
  2. fastlane cron:
     (crontab -l 2>/dev/null; echo "*/5 * * * * python3 $PH/fastlane.py >> $PH/fastlane.cron.log 2>&1") | crontab -
  3. GPU-нода (если vLLM внешний):
     scp $PH/gpu-node/* gpu-node@HOST: && ssh gpu-node@HOST 'bash install-gpu-node.sh'
  4. Первая карточка:
     python3 $PH/kanban/cli.py create --kind task --assignee architect \\
       --title "Диагностика: <проблема>" --description "<ТЗ>"

Проверка:
  PIPELINE_DRY_RUN=1 PIPELINE_HOME=$PH PIPELINE_PROJECT_DIR=$PD \\
    python3 $PH/gateway/gateway.py --once
========================================
EOF