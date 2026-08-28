-- 001_symbiosis.sql — расширения схемы kanban для симбиоза (Wave 1-2)
-- Идемпотентность: колонки добавляются только если отсутствуют (SQLite не поддерживает IF NOT EXISTS для ADD COLUMN).
-- Применяется скриптом apply_migration.py, который сам проверяет существование колонок.

-- Слой A: декларативные гейты + ограничения + мягкие пинок-счётчики
ALTER TABLE cards ADD COLUMN required_gates TEXT DEFAULT '';   -- CSV тегов: "reviewer,qa,ui"
ALTER TABLE cards ADD COLUMN allowed_tools  TEXT DEFAULT '';   -- CSV: "bash,write,read"
ALTER TABLE cards ADD COLUMN security_gate  INTEGER DEFAULT 0; -- 1 = red-team перед deploy
ALTER TABLE cards ADD COLUMN nudged         INTEGER DEFAULT 0; -- счётчик мягких пинков

-- Таблица cooldown'ов (движок/инструменты/воркеры)
CREATE TABLE IF NOT EXISTS cooldowns (
  key    TEXT PRIMARY KEY,   -- engine | tool:<name> | worker:<card_id>
  reason TEXT,
  until  TEXT
);

-- Журнал вызовов инструментов (для loop-детектора)
CREATE TABLE IF NOT EXISTS tool_calls (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id       TEXT,
  tool          TEXT,
  args_hash     TEXT,
  result_status TEXT,
  ts            TEXT
);

-- Журнал hang'ов движка
CREATE TABLE IF NOT EXISTS hang_log (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  ts             TEXT,
  action         TEXT,
  gpu_util       INTEGER,
  kv_cache_pct   REAL,
  gen_tokens_60s REAL
);

-- Хранилище неудачных черновиков (не засоряют комментарии)
CREATE TABLE IF NOT EXISTS failed_drafts (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id TEXT,
  body    TEXT,
  reason  TEXT,
  ts      TEXT
);