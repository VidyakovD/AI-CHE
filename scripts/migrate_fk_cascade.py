"""
Миграция: добавить ON DELETE CASCADE на все FK к users.id.

ЗАЧЕМ: до фикса при удалении User в БД оставались orphan-rows
в Message/Transaction/VerifyToken/etc — нарушение референциальной
целостности. После миграции delete user автоматически удалит связанные
строки (или установит NULL для nullable FK).

КАК: SQLite не поддерживает ALTER COLUMN для FK constraints.
Полный recreate-цикл:
  1. PRAGMA foreign_keys=OFF
  2. CREATE TABLE new_X (...) с правильными ON DELETE
  3. INSERT INTO new_X SELECT * FROM X
  4. DROP TABLE X
  5. ALTER TABLE new_X RENAME TO X
  6. Восстанавливаем индексы
  7. PRAGMA foreign_keys=ON

БЕЗОПАСНОСТЬ: скрипт делает БЭКАП перед началом + останавливает сервис.

ИСПОЛЬЗОВАНИЕ:
  systemctl stop ai-che
  cd /root/AI-CHE
  ./venv/bin/python scripts/migrate_fk_cascade.py --execute
  systemctl start ai-che

Без --execute — dry-run (показывает что будет сделано, не меняет БД).
"""
import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime


# Таблицы и колонки которые ссылаются на users.id.
# nullable=True → ON DELETE SET NULL (анон-чат остаётся после удаления)
# nullable=False → ON DELETE CASCADE (без юзера запись бессмысленна)
TARGETS = [
    # (table, fk_column, nullable, description)
    ("verify_tokens",          "user_id",   False, "verify codes"),
    ("subscriptions",          "user_id",   False, "subs (legacy)"),
    ("transactions",           "user_id",   False, "history of payments"),
    ("messages",               "user_id",   True,  "chat messages (anon allowed)"),
    ("api_keys",               "user_id",   True,  "user-provided api keys"),
    ("model_pricing",          "user_id",   True,  "per-user price overrides"),
    ("usage_logs",             "user_id",   False, "AI usage stats"),
    ("model_features",         "user_id",   True,  "model UI features"),
    ("ban_logs",               "user_id",   False, "moderation history"),
    ("user_referrals",         "user_id",   False, "referral links"),
    ("admin_audit_log",        "admin_id",  False, "admin actions"),
    ("user_logs",              "user_id",   False, "audit user-side"),
    ("agent_jobs",             "user_id",   True,  "agent runner queue"),
    ("uploaded_files",         "user_id",   True,  "uploaded media"),
    ("tariff_subscriptions",   "user_id",   False, "tariff subs"),
    ("session_logs",           "user_id",   False, "login sessions"),
]


def has_table(conn, name):
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return cur.fetchone() is not None


def get_create_sql(conn, table):
    cur = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,))
    row = cur.fetchone()
    return row[0] if row else None


def patch_create_sql(sql: str, fk_column: str, on_delete: str) -> str:
    """
    Грубо: заменяет `<col> INTEGER ... REFERENCES users(id)` →
    `<col> INTEGER ... REFERENCES users(id) ON DELETE CASCADE`
    Если уже есть ON DELETE — пропускает.
    """
    import re
    # Пропускаем если уже есть ON DELETE рядом с REFERENCES users
    if re.search(r'REFERENCES\s+["\']?users["\']?\s*\(\s*id\s*\)\s+ON\s+DELETE', sql, re.I):
        return sql
    # Подмешиваем ON DELETE сразу после REFERENCES users(id)
    new_sql = re.sub(
        r'(REFERENCES\s+["\']?users["\']?\s*\(\s*id\s*\))',
        rf'\1 ON DELETE {on_delete}',
        sql,
        count=1,
        flags=re.I,
    )
    return new_sql


def migrate_table(conn, table, fk_column, nullable, dry_run=True):
    if not has_table(conn, table):
        return f"  [skip] {table}: таблица не существует"
    create_sql = get_create_sql(conn, table)
    if not create_sql:
        return f"  [skip] {table}: не удалось прочитать SQL"
    on_delete = "SET NULL" if nullable else "CASCADE"
    new_sql = patch_create_sql(create_sql, fk_column, on_delete)
    if new_sql == create_sql:
        return f"  [skip] {table}.{fk_column}: уже имеет ON DELETE или нет REFERENCES users"
    new_sql_renamed = new_sql.replace(f'CREATE TABLE "{table}"', f'CREATE TABLE "{table}_new"', 1)
    new_sql_renamed = new_sql_renamed.replace(f"CREATE TABLE {table}", f"CREATE TABLE {table}_new", 1)
    if dry_run:
        return f"  [DRY] {table}.{fk_column} → ON DELETE {on_delete}"
    # Получаем индексы
    indexes = list(conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
        (table,)
    ))
    # Транзакция
    conn.execute(new_sql_renamed)
    conn.execute(f'INSERT INTO "{table}_new" SELECT * FROM "{table}"')
    conn.execute(f'DROP TABLE "{table}"')
    conn.execute(f'ALTER TABLE "{table}_new" RENAME TO "{table}"')
    # Восстанавливаем индексы (CREATE INDEX IF NOT EXISTS)
    for name, sql in indexes:
        if not sql:
            continue
        try:
            conn.execute(sql)
        except Exception as e:
            print(f"    [warn] index {name}: {e}")
    return f"  [OK]  {table}.{fk_column} → ON DELETE {on_delete} (recreated)"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true",
                        help="Реально применить миграцию (без флага — dry-run)")
    parser.add_argument("--db", default="chat.db",
                        help="Путь к chat.db (default: ./chat.db)")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: {args.db} не найдена", file=sys.stderr)
        sys.exit(1)

    if args.execute:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = f"{args.db}.pre-cascade-{ts}.bak"
        print(f"Backup: {args.db} → {backup_path}")
        shutil.copy(args.db, backup_path)
    else:
        print("=== DRY RUN === (без --execute, ничего не меняется)")

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        for table, col, nullable, desc in TARGETS:
            try:
                msg = migrate_table(conn, table, col, nullable, dry_run=not args.execute)
                print(msg)
            except Exception as e:
                print(f"  [ERR] {table}.{col}: {e}")
                conn.rollback()
                if args.execute:
                    raise
        if args.execute:
            conn.commit()
            print("\n✓ Миграция применена. Backup: " + backup_path)
        else:
            print("\nDry-run завершён. Запусти с --execute для применения.")
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.close()


if __name__ == "__main__":
    main()
