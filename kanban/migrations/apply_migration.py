#!/usr/bin/env python3
"""apply_migration.py — идемпотентное применение migrations/NNN_*.sql.

SQLite не умеет ADD COLUMN IF NOT EXISTS, поэтому колонки проверяются
перед применением. Комментарии (строковые и хвостовые) удаляются ДО
разбиения на операторы — иначе хвостовой комментарий поглощает
следующую инструкцию.

Использование:
  python3 apply_migration.py [--db PATH] [--dry]
"""
import argparse
import os
import re
import sqlite3

DEFAULT_DB = os.path.expanduser("~/agent-pipeline/kanban/boards/main.sqlite3")
MIGRATIONS_DIR = os.path.dirname(os.path.abspath(__file__))


def existing_columns(con, table):
    return {row[1].lower() for row in con.execute(f"PRAGMA table_info({table})")}


def strip_comments(sql_text):
    """Удаляет -- комментарии (строковые и хвостовые), уважая одинарные кавычки."""
    out = []
    for line in sql_text.splitlines():
        buf = []
        in_q = False
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == "'":
                in_q = not in_q
                buf.append(ch)
            elif ch == "-" and i + 1 < len(line) and line[i + 1] == "-" and not in_q:
                break
            else:
                buf.append(ch)
            i += 1
        out.append("".join(buf))
    return "\n".join(out)


def drop_existing_alters(sql_text, columns):
    """Вырезает ALTER TABLE ... ADD COLUMN для уже существующих колонок."""
    out_lines = []
    pat = re.compile(
        r"(?i)^ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"\[]?(\w+)[`\"\]]?")
    for line in sql_text.splitlines():
        m = pat.match(line.strip())
        if m:
            table, col = m.group(1).lower(), m.group(2).lower()
            if table in columns and col in columns[table]:
                print(f"  skip: {col} уже существует в {table}")
                continue
        out_lines.append(line)
    return "\n".join(out_lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(args.db, timeout=30)
    cols = {t: existing_columns(con, t) for t in ("cards", "cooldowns", "tool_calls",
                                                  "hang_log", "failed_drafts")}
    applied_any = False
    for fname in sorted(os.listdir(MIGRATIONS_DIR)):
        if not fname.endswith(".sql"):
            continue
        sql = open(os.path.join(MIGRATIONS_DIR, fname)).read()
        prepared = drop_existing_alters(strip_comments(sql), cols)
        stmts = [s for s in prepared.split(";") if s.strip()]
        if not stmts:
            print(f"{fname}: нечего применять")
            continue
        print(f"{fname}: применяю {len(stmts)} операторов")
        if args.dry:
            for s in stmts:
                print("  DRY:", s.strip().splitlines()[0][:80])
            continue
        for s in stmts:
            con.execute(s)
        con.commit()
        applied_any = True
        print(f"{fname}: ОК")
    con.close()
    print("готово" if applied_any or args.dry else "ничего не применялось")


if __name__ == "__main__":
    main()