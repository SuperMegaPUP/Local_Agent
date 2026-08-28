#!/bin/bash
# Аккуратный останов конвейера перед выключением/перезагрузкой сервера.
# Порядок: стоп-кран -> убить группы воркеров -> остановить шлюз.
# После включения сервера: rm ~/agent-pipeline/PAUSE (шлюз стартует сам).
set -uo pipefail
BASE="$HOME/agent-pipeline"
WS="$BASE/runs/workers.json"

echo "[1/4] Стоп-кран: шлюз не запустит новых воркеров"
touch "$BASE/PAUSE"

pids=""
if [ -f "$WS" ]; then
  pids=$(python3 -c "import json;d=json.load(open('$WS'));print(' '.join(str(v) for v in d.values() if v!='dry'))" 2>/dev/null || true)
fi

if [ -n "${pids// /}" ]; then
  echo "[2/4] Мягкий сигнал группам воркеров: $pids"
  for pid in $pids; do kill -TERM -- "-$pid" 2>/dev/null || true; done
  sleep 6
  echo "[3/4] Принудительно убираю выживших"
  for pid in $pids; do kill -KILL -- "-$pid" 2>/dev/null || true; done
else
  echo "[2/4] Активных воркеров нет"
  echo "[3/4] —"
fi
rm -f "$WS"

echo "[4/4] Останавливаю шлюз"
systemctl stop agent-gateway.service 2>/dev/null || sudo systemctl stop agent-gateway.service 2>/dev/null
sleep 2
if systemctl is-active agent-gateway.service 2>/dev/null | grep -q active; then
  echo "ВНИМАНИЕ: systemctl потребовал пароль — шлюз жив, НО стоп-кран PAUSE его парализует:"
  echo "он крутит холостой цикл и НЕ запускает воркеров. Для сервера это безопасно."
fi
left=$(pgrep -f 'opencode run --agent' | tr '\n' ' ')
if [ -n "$left" ]; then
  echo "ВНИМАНИЕ: остались процессы opencode: $left — убить вручную?"
else
  echo "OK: воркеров нет, шлюз остановлен. Можно выключать сервер."
fi
echo "После включения: rm ~/agent-pipeline/PAUSE  (шлюз стартует сам и продолжит с той же точки)"