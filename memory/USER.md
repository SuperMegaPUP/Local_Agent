# USER — факты о сетапе
- Проект: Горизонт Событий, /home/g/gorizont-sobytij_test, Next.js 16 + Prisma + Redis + Docker (DEV-контур :3000).
- Инференс: vLLM qwen3.8-27b INT8 на 192.168.31.52:8000 (потолок ~318 tok/s суммарно, prefix-cache 92%).
- Конвейер: ~/agent-pipeline (kanban SQLite + gateway + 9 ролей opencode).
- Лимит параллелизма: PIPELINE_MAX_PARALLEL=4 (решение старшего, 2026-08-25).
- Egress: v2ray VLESS+TLS, SOCKS5 127.0.0.1:1081.
