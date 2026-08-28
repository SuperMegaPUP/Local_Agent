#!/usr/bin/env bash
# install-gpu-node.sh — установка watchdog-agent на GPU-ноде. (v2, исправлены баги v1)
#
# ВАЖНО: запускать ОБЫЧНЫМ пользователем, НЕ через sudo:
#   cd ~/gpu-node && bash install-gpu-node.sh
# Привилегированные шаги (tee юнита, daemon-reload, ufw) делают sudo ВНУТРИ скрипта.
#
# Безопасность: скрипт НИКОГДА не выполняет команд изменения состояния vLLM.
# Определение способа рестарта — только чтение (list-unit-files / docker ps).
set -euo pipefail

APPDIR="${HOME}/watchdog-agent"
SERVICE_USER="$(whoami)"
CONTROL_IP="${CONTROL_IP:-192.168.122.3}"   # IP control-node (нашей виртуалки)
TOKEN_FILE=".watchdog_token"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "== watchdog-agent: установка =="

# 1. каталог и скрипт
mkdir -p "$APPDIR"
cp -f "$SCRIPT_DIR/watchdog-agent.py" "$APPDIR/"
chmod 755 "$APPDIR/watchdog-agent.py"

# 2. токен
if [[ ! -f "$SCRIPT_DIR/$TOKEN_FILE" ]]; then
  echo "генерирую новый токен..."
  openssl rand -hex 24 > "$SCRIPT_DIR/$TOKEN_FILE"
fi
chmod 600 "$SCRIPT_DIR/$TOKEN_FILE"
TOKEN="$(cat "$SCRIPT_DIR/$TOKEN_FILE")"
echo "токен: ${TOKEN:0:8}... (полный в $TOKEN_FILE)"

# 3. определяем способ рестарта vLLM — ТОЛЬКО ОПРОС, без команд изменения состояния.
#    Поддержка принудительного задания: METHOD=systemctl UNIT=vllm bash install-gpu-node.sh
METHOD="${METHOD:-}"
UNIT="${UNIT:-}"
CONTAINER="${CONTAINER:-}"
if [[ -z "$METHOD" ]]; then
  CAND_SYS="$(systemctl list-unit-files 2>/dev/null | grep -i vllm | awk '{print $1}' || true)"
  CAND_RT="$(systemctl list-units --state=running 2>/dev/null | grep -i vllm | awk '{print $1}' || true)"
  echo "кандидаты (unit-файлы): ${CAND_SYS:-—}"
  echo "кандидаты (запущены):   ${CAND_RT:-—}"
  if [[ -n "$CAND_SYS" ]]; then
    METHOD="systemctl"
    UNIT="$(printf '%s\n' "$CAND_SYS" | head -1 | sed 's/\.service$//')"
  elif [[ -n "$CAND_RT" ]]; then
    METHOD="systemctl"
    UNIT="$(printf '%s\n' "$CAND_RT" | head -1 | sed 's/\.service$//')"
  elif docker ps --format '{{.Names}}' 2>/dev/null | grep -qi vllm; then
    METHOD="docker"
    CONTAINER="$(docker ps --format '{{.Names}}' | grep -i vllm | head -1)"
  else
    echo "ОШИБКА: vllm не найден среди systemd-юнитов и docker-контейнеров." >&2
    echo "Укажите вручную, например:" >&2
    echo "  METHOD=systemctl UNIT=<имя-юнита> bash install-gpu-node.sh" >&2
    echo "  METHOD=docker CONTAINER=<имя> bash install-gpu-node.sh" >&2
    echo "Имена юнитов посмотреть: systemctl list-unit-files | grep -i vllm" >&2
    exit 1
  fi
fi
if [[ "$METHOD" == "systemctl" ]]; then
  echo "метод рестарта: systemctl ($UNIT)"
else
  echo "метод рестарта: docker ($CONTAINER)"
fi

# 4. проверка права на рестарт — только зонд прав, НИКАКИХ команд над vllm
if [[ "$METHOD" == "systemctl" ]]; then
  if sudo -n true >/dev/null 2>&1; then
    echo "sudo: доступен"
  else
    echo "ВНИМАНИЕ: passwordless sudo недоступен. Добавьте в /etc/sudoers.d/vllm-restart:"
    echo "  $SERVICE_USER ALL=(root) NOPASSWD: /bin/systemctl restart $UNIT"
    read -rp "Продолжить установку? [y/N] " ans; [[ "$ans" =~ ^[Yy]$ ]] || exit 1
  fi
fi

# 5. systemd-юнит: генерация в переменной, установка через sudo tee (без /tmp)
#    Логи — в journald (совместимо со всеми версиями systemd).
PYBIN="$(command -v python3)"
UNIT_CONTENT="[Unit]
Description=vLLM watchdog agent (sensors + restart actuator)
After=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Environment=WATCHDOG_AGENT_PORT=9100
Environment=WATCHDOG_TOKEN=$TOKEN
Environment=PIPELINE_ENGINE_URL=http://127.0.0.1:8000
Environment=PIPELINE_ENGINE_RESTART=$METHOD
Environment=PIPELINE_ENGINE_UNIT=$UNIT
Environment=PIPELINE_ENGINE_CONTAINER=$CONTAINER
ExecStart=$PYBIN $APPDIR/watchdog-agent.py
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target"

echo "$UNIT_CONTENT" | sudo tee /etc/systemd/system/watchdog-agent.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now watchdog-agent || {
  echo "ОШИБКА: юнит не запустился. Диагностика:"
  echo "  systemctl status watchdog-agent.service --no-pager | head -20"
  echo "  journalctl -u watchdog-agent -n 20 --no-pager"
  echo "  systemctl --version | head -1"
  exit 1
}
sleep 2

# 6. firewall: открыть 9100 ТОЛЬКО для control-node
if command -v ufw >/dev/null 2>&1 && sudo ufw status 2>/dev/null | grep -q "Status: active"; then
  sudo ufw allow from "$CONTROL_IP" to any port 9100 proto tcp
  echo "ufw: открыт 9100 только для $CONTROL_IP"
else
  echo "ufw не активен — убедитесь, что порт 9100 закрыт от посторонних!"
fi

# 7. смоук-тест (логи агента теперь в journald)
echo "== проверка =="
if curl -sf http://127.0.0.1:9100/health; then
  echo
  curl -sf http://127.0.0.1:9100/metrics | python3 -m json.tool | head -25
  echo
  echo "ГОТОВО. Сохраните $TOKEN_FILE — он нужен на control-node (watchdog.py)."
  echo "Логи агента: journalctl -u watchdog-agent -f"
  echo "С control-node проверить: curl http://<gpu-ip>:9100/metrics"
else
  echo "ОШИБКА: агент не ответил на :9100. Диагностика:"
  echo "  systemctl status watchdog-agent.service --no-pager | head -20"
  echo "  journalctl -u watchdog-agent -n 30 --no-pager"
  exit 1
fi