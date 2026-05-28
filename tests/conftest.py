"""Pytest конфиг.

Делает две вещи ДО загрузки server-кода:
1. Включает DEV_MODE — иначе main.py падает с RuntimeError из-за ALLOWED_ORIGINS.
2. Применяет LIGHTWEIGHT_MIGRATIONS — иначе тесты падают с `no such column`
   когда мы добавили новые поля в models.py (create_all не апдейтит существующие).
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Должно быть установлено ДО импорта main.py (CORS-проверка в main.py).
os.environ.setdefault("DEV_MODE", "true")
os.environ.setdefault("APP_ENV", "dev")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-32-chars-long-yes-yes")


def pytest_configure(config):
    """Применить миграции один раз перед всеми тестами.

    Порядок:
      1. Base.metadata.create_all — создаёт отсутствующие таблицы
      2. apply_lightweight_migrations — добавляет колонки/индексы из
         LIGHTWEIGHT_MIGRATIONS/LIGHTWEIGHT_INDEXES в db.py
      3. alembic upgrade head (best-effort) — для сложных миграций
         (DROP COLUMN, data migrations, FK changes); не блокирует
         если alembic.ini отсутствует или БД не SQLite

    Раньше шаг 3 был только в CI (.github/workflows/ci.yml). Из-за
    этого ~159 тестов падали локально с sqlalchemy.exc.OperationalError
    «no such column» — миграция не догнала старую chat.db.
    """
    from server.db import Base, engine, apply_lightweight_migrations
    from server import models  # noqa — регистрация всех таблиц
    Base.metadata.create_all(bind=engine)
    apply_lightweight_migrations()
    # Alembic — best-effort. Если упадёт (нет alembic.ini, нет ревизий,
    # БД не sqlite) — продолжаем без него, lightweight уже сделал базу.
    try:
        import os as _os
        if _os.path.exists("alembic.ini"):
            from alembic.config import Config as _AlcCfg
            from alembic import command as _alc_cmd
            cfg = _AlcCfg("alembic.ini")
            _alc_cmd.upgrade(cfg, "head")
    except Exception as _e:
        import logging as _l
        _l.getLogger(__name__).warning(f"[conftest] alembic upgrade skipped: {_e}")
