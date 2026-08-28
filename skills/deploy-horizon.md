# Deploy Горизонт Событий (LOCALE, НЕ Vercel)
1. cd /home/g/gorizont-sobytij_test
2. docker compose up -d --build dev
3. sleep 10; curl -sf http://localhost:3000/api/health | jq .status == "ok"
4. curl -sf http://localhost:3000/ | grep -q 'lang="ru"'
5. ARTIFACTS: вывод health + commit HEAD
Внимание: /api/instruments и /api/tinvest зависят от внешних сервисов
(MOEX APIM/JWT, T-Invest) — их FAIL не блокирует деплой (см. CONTEXT/KNOWN-ISSUES.md).
