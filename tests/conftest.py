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
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-32-chars-long-yes-yes")


def pytest_configure(config):
    """Применить миграции один раз перед всеми тестами."""
    from server.db import Base, engine, apply_lightweight_migrations
    from server import models  # noqa — регистрация всех таблиц
    Base.metadata.create_all(bind=engine)
    apply_lightweight_migrations()
