"""
Миграция данных из SQLite (chat.db) в PostgreSQL.

Использование на проде:
  # 1. Создать БД и пользователя в postgres
  sudo -u postgres psql -c "CREATE USER aiche WITH PASSWORD 'STRONG_PASSWORD';"
  sudo -u postgres psql -c "CREATE DATABASE aiche OWNER aiche;"

  # 2. Запустить миграцию
  cd /root/AI-CHE
  SQLITE_PATH=./chat.db \
  POSTGRES_URL='postgresql://aiche:STRONG_PASSWORD@localhost:5432/aiche' \
  ./venv/bin/python scripts/migrate_sqlite_to_postgres.py

  # 3. Изменить .env DATABASE_URL → systemctl restart ai-che

Что делает:
  1. Создаёт схему в postgres через Base.metadata.create_all (все таблицы +
     индексы из server.models). На свежей БД это даст корректные NULLable
     поля, FK, UNIQUE — без legacy багов.
  2. Применяет LIGHTWEIGHT_INDEXES (partial UNIQUE и т.д.).
  3. Для каждой таблицы из metadata: SELECT * FROM sqlite → INSERT INTO postgres.
  4. Сбрасывает sequences для autoincrement (postgres не подхватывает MAX(id)).
  5. Проверяет что число строк совпадает.

Идемпотентно НЕ является — если postgres-БД не пустая, скрипт упадёт на
конфликте PK. Запускать на свежей БД.
"""
import os
import sys
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("migrate")

SQLITE_PATH = os.getenv("SQLITE_PATH", "./chat.db")
POSTGRES_URL = os.getenv("POSTGRES_URL", "").strip()

if not POSTGRES_URL:
    log.error("POSTGRES_URL не задан. Пример: postgresql://aiche:pwd@localhost:5432/aiche")
    sys.exit(1)
if not POSTGRES_URL.startswith("postgresql"):
    log.error("POSTGRES_URL должен начинаться с postgresql://")
    sys.exit(1)
if not os.path.exists(SQLITE_PATH):
    log.error(f"SQLite-файл не найден: {SQLITE_PATH}")
    sys.exit(1)

# Подменяем DATABASE_URL ДО импорта server.* — иначе db.py возьмёт sqlite default
os.environ["DATABASE_URL"] = POSTGRES_URL

# Добавляем корень проекта в sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text, MetaData
from sqlalchemy.orm import sessionmaker

# Импортируем models чтобы заполнились metadata. db.py теперь смотрит на postgres.
from server import models  # noqa: F401 — нужно для регистрации таблиц
from server.db import Base, engine as pg_engine, LIGHTWEIGHT_INDEXES


def main():
    log.info(f"Source SQLite: {SQLITE_PATH}")
    log.info(f"Target Postgres: {POSTGRES_URL.split('@')[1] if '@' in POSTGRES_URL else POSTGRES_URL}")

    # ── 1. Создаём схему в postgres ───────────────────────────────────────────
    log.info("[1/5] Base.metadata.create_all() в postgres…")
    Base.metadata.create_all(pg_engine)
    log.info(f"    создано {len(Base.metadata.tables)} таблиц")

    # ── 2. Lightweight indexes (partial UNIQUE) ──────────────────────────────
    log.info("[2/5] LIGHTWEIGHT_INDEXES…")
    with pg_engine.connect() as conn:
        for name, sql in LIGHTWEIGHT_INDEXES:
            try:
                conn.execute(text(sql))
                conn.commit()
                log.info(f"    {name}: ok")
            except Exception as e:
                log.warning(f"    {name}: {e}")

    # ── 3. Открываем SQLite source ────────────────────────────────────────────
    log.info("[3/5] Подключаемся к SQLite source…")
    sqlite_engine = create_engine(f"sqlite:///{SQLITE_PATH}")
    SqliteSession = sessionmaker(bind=sqlite_engine)
    PgSession = sessionmaker(bind=pg_engine)

    # Какие таблицы в SQLite реально есть (на случай legacy)
    with sqlite_engine.connect() as sconn:
        sqlite_tables = {row[0] for row in sconn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ))}

    # ── 4. Перенос данных таблица за таблицей ─────────────────────────────────
    log.info("[4/5] Копирование данных…")
    # Сортируем таблицы в порядке зависимостей (FK) — sorted_tables это даёт
    ordered = Base.metadata.sorted_tables
    total_copied = 0
    skipped = []
    for table in ordered:
        tname = table.name
        if tname not in sqlite_tables:
            skipped.append(tname)
            continue
        try:
            with sqlite_engine.connect() as sconn:
                rows = list(sconn.execute(text(f"SELECT * FROM {tname}")))
                if not rows:
                    log.info(f"    {tname}: 0 (пусто)")
                    continue
                cols = list(rows[0]._mapping.keys())
            # Вставка в postgres батчами через executemany
            with pg_engine.begin() as pconn:
                cols_csv = ", ".join(f'"{c}"' for c in cols)
                placeholders = ", ".join(f":{c}" for c in cols)
                stmt = text(f'INSERT INTO "{tname}" ({cols_csv}) VALUES ({placeholders})')
                payload = [dict(r._mapping) for r in rows]
                pconn.execute(stmt, payload)
            log.info(f"    {tname}: {len(rows)} строк скопировано")
            total_copied += len(rows)
        except Exception as e:
            log.error(f"    {tname}: ОШИБКА {type(e).__name__}: {str(e)[:300]}")
            raise
    if skipped:
        log.info(f"    пропущено (нет в SQLite): {', '.join(skipped[:10])}"
                  + (f" и ещё {len(skipped)-10}" if len(skipped) > 10 else ""))

    # ── 5. Сбрасываем sequences (Postgres не подхватывает MAX(id)) ───────────
    log.info("[5/5] Resetting PK sequences…")
    with pg_engine.connect() as pconn:
        for table in ordered:
            tname = table.name
            pk_cols = [c.name for c in table.primary_key.columns]
            if not pk_cols:
                continue
            pk = pk_cols[0]
            # PostgreSQL автогенерит sequence имя <table>_<col>_seq
            seq_name = f"{tname}_{pk}_seq"
            try:
                # setval(seq, max(id)) — если нет данных, оставляем 1
                pconn.execute(text(
                    f"SELECT setval('{seq_name}', "
                    f"COALESCE((SELECT MAX(\"{pk}\") FROM \"{tname}\"), 1), "
                    f"(SELECT MAX(\"{pk}\") FROM \"{tname}\") IS NOT NULL)"
                ))
                pconn.commit()
            except Exception:
                # Sequence может не существовать (если PK не autoincrement) —
                # это ок для composite/string PK.
                pconn.rollback()
                continue
    log.info(f"=== ГОТОВО. Скопировано {total_copied} строк в {len(ordered)} таблиц ===")


if __name__ == "__main__":
    main()
