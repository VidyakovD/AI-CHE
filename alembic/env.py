"""
Alembic env.py для AI Студия Че.

Особенности:
  - URL берётся из DATABASE_URL env (как в server/db.py) — один источник правды.
  - target_metadata = Base.metadata из server.models — autogenerate смотрит на ORM.
  - При старте подключает legacy LIGHTWEIGHT_MIGRATIONS (см. server/db.py)
    для совместимости с уже задеплоенными окружениями. Новые изменения
    схемы — через `alembic revision --autogenerate`.

Команды:
  alembic upgrade head                        # применить все миграции
  alembic revision --autogenerate -m "name"   # сгенерить из diff ORM ↔ БД
  alembic current                              # текущая ревизия
  alembic history                              # все ревизии
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Гарантируем что server/ импортируется
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Alembic Config object — переписываем sqlalchemy.url из env.
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# DATABASE_URL имеет приоритет; fallback — SQLite chat.db в корне проекта.
db_url = os.getenv("DATABASE_URL") or "sqlite:///./chat.db"
config.set_main_option("sqlalchemy.url", db_url)

# ORM metadata для autogenerate
from server import models  # noqa: E402,F401 — регистрирует таблицы
from server.db import Base  # noqa: E402

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Запуск миграций без подключения (генерит SQL для review)."""
    context.configure(
        url=db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Стандартный запуск с подключением."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite не любит ALTER COLUMN — позволяем batch-режим
            render_as_batch=db_url.startswith("sqlite"),
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
