-- SIMBIOSIS Wave 3 (слой J): kind='consult' — вопрос к роли, ответ = ARTIFACTS-комментарий.
-- CHECK на kind — column-level (без имени), DROP CONSTRAINT его не снимет.
-- Единственный путь — реконструкция таблицы (стандартный SQLite-рецепт).
-- Применение: холодное окно (running=0), бэкап БД заранее.
-- Откат: восстановить бэкап (rebuild обратима данными — данные не меняются, меняется только DDL).

PRAGMA foreign_keys=OFF;
BEGIN;

CREATE TABLE cards_new (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT DEFAULT '',
  kind TEXT DEFAULT 'task' CHECK(kind IN ('task','epic','escalation','intervention','report','consult')),
  assignee TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'todo' CHECK(status IN ('todo','ready','running','review','done','blocked')),
  parent_id TEXT REFERENCES cards(id), depends_on TEXT DEFAULT '',
  timeout_minutes INTEGER DEFAULT 90, retry_count INTEGER DEFAULT 0, max_retries INTEGER DEFAULT 3,
  created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, heartbeat TEXT,
  required_gates TEXT DEFAULT '', allowed_tools TEXT DEFAULT '',
  security_gate INTEGER DEFAULT 0, nudged INTEGER DEFAULT 0);

INSERT INTO cards_new (id,title,description,kind,assignee,status,parent_id,depends_on,
  timeout_minutes,retry_count,max_retries,created_at,started_at,finished_at,heartbeat,
  required_gates,allowed_tools,security_gate,nudged)
SELECT id,title,description,kind,assignee,status,parent_id,depends_on,
  timeout_minutes,retry_count,max_retries,created_at,started_at,finished_at,heartbeat,
  required_gates,allowed_tools,security_gate,nudged FROM cards;

DROP TABLE cards;
ALTER TABLE cards_new RENAME TO cards;
CREATE INDEX IF NOT EXISTS idx_cards_status ON cards(status);

COMMIT;
PRAGMA foreign_keys=ON;