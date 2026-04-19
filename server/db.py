import contextlib
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = "sqlite:///./chat.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)

Base = declarative_base()


@contextlib.contextmanager
def db_session():
    """
    Контекст-менеджер для разовых сессий вне FastAPI Depends.
    Гарантирует rollback при исключении и закрытие в любом случае.
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ── Lightweight schema migrations (без Alembic) ─────────────────────────────
# SQLAlchemy create_all() умеет создавать новые таблицы, но НЕ умеет добавлять
# колонки к существующим. Для прода используем идемпотентный ALTER TABLE.
#
# Как добавить миграцию: append (table, column, sql_type) к LIGHTWEIGHT_MIGRATIONS.
# Запуск произойдёт автоматически при startup.

LIGHTWEIGHT_MIGRATIONS: list[tuple[str, str, str]] = [
    # пример: ("chatbots", "tg_webhook_secret", "VARCHAR"),
]


def apply_lightweight_migrations():
    """Идемпотентно добавляет недостающие колонки в существующие таблицы (SQLite)."""
    from sqlalchemy import text
    import logging
    log = logging.getLogger(__name__)
    with engine.connect() as conn:
        for table, col, sql_type in LIGHTWEIGHT_MIGRATIONS:
            try:
                rows = list(conn.execute(text(f"PRAGMA table_info({table})")))
            except Exception as e:
                log.warning(f"migration: cannot read {table}: {e}")
                continue
            existing = {row[1] for row in rows}
            if col in existing:
                continue
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {sql_type}"))
                conn.commit()
                log.info(f"migration: added {table}.{col} {sql_type}")
            except Exception as e:
                log.error(f"migration: failed to add {table}.{col}: {e}")