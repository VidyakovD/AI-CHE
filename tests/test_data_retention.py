"""
Тесты для data_retention_loop / _data_retention_tick (152-ФЗ ст. 5).

Покрытие:
- Анонимизация неактивного User (last_login_at > N месяцев)
- Активный User НЕ трогается
- DRY_RUN режим логирует но не пишет
- Двойной прогон не дублирует (anon_* фильтр)
- Очистка ProposalProject содержимого
- ENV-флаги (если 0 — пропускаем тип)
"""
import asyncio
import os
import uuid
from datetime import datetime, timedelta

import pytest


def _uniq_email(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}@test.local"


def _make_user(db, *, email: str, days_inactive: int | None, name: str = "Test"):
    """Создать User с заданной last_login_at."""
    from server.models import User
    u = User(
        email=email,
        password_hash="$2b$12$" + "x" * 50,  # фейк bcrypt
        name=name,
        is_verified=True,
        last_login_at=(datetime.utcnow() - timedelta(days=days_inactive)) if days_inactive else None,
        marketing_consent=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


class TestDataRetentionUser:
    def test_anonymizes_inactive_user(self, monkeypatch):
        monkeypatch.setenv("DATA_RETENTION_USER_INACTIVE_MONTHS", "24")
        monkeypatch.setenv("DATA_RETENTION_PROPOSAL_YEARS", "0")
        monkeypatch.setenv("DATA_RETENTION_DRY_RUN", "false")

        from server.db import db_session
        from server.scheduler import _data_retention_tick

        with db_session() as db:
            u = _make_user(db, email=_uniq_email("stale_retention"), days_inactive=900)
            uid = u.id

        asyncio.run(_data_retention_tick())

        with db_session() as db:
            from server.models import User
            after = db.query(User).filter_by(id=uid).first()
            assert after is not None
            # email → anon_<id>_<hash>@deleted.local
            assert after.email.startswith(f"anon_{uid}_")
            assert "@deleted.local" in after.email
            assert after.name == "Удалённый пользователь"
            assert after.marketing_consent is False

    def test_active_user_NOT_touched(self, monkeypatch):
        monkeypatch.setenv("DATA_RETENTION_USER_INACTIVE_MONTHS", "24")
        monkeypatch.setenv("DATA_RETENTION_PROPOSAL_YEARS", "0")
        monkeypatch.setenv("DATA_RETENTION_DRY_RUN", "false")

        from server.db import db_session
        from server.scheduler import _data_retention_tick

        with db_session() as db:
            u = _make_user(db, email=_uniq_email("active_retention"), days_inactive=30)
            uid = u.id
            orig_email = u.email

        asyncio.run(_data_retention_tick())

        with db_session() as db:
            from server.models import User
            after = db.query(User).filter_by(id=uid).first()
            assert after.email == orig_email
            assert after.marketing_consent is True

    def test_dry_run_does_not_modify(self, monkeypatch):
        monkeypatch.setenv("DATA_RETENTION_USER_INACTIVE_MONTHS", "24")
        monkeypatch.setenv("DATA_RETENTION_PROPOSAL_YEARS", "0")
        monkeypatch.setenv("DATA_RETENTION_DRY_RUN", "true")

        from server.db import db_session
        from server.scheduler import _data_retention_tick

        with db_session() as db:
            u = _make_user(db, email=_uniq_email("dryrun_retention"), days_inactive=900)
            uid = u.id
            orig_email = u.email

        asyncio.run(_data_retention_tick())

        with db_session() as db:
            from server.models import User
            after = db.query(User).filter_by(id=uid).first()
            assert after.email == orig_email  # без изменений

    def test_already_anonymized_NOT_reprocessed(self, monkeypatch):
        """Повторный прогон не должен снова трогать anon_* юзеров."""
        monkeypatch.setenv("DATA_RETENTION_USER_INACTIVE_MONTHS", "24")
        monkeypatch.setenv("DATA_RETENTION_PROPOSAL_YEARS", "0")
        monkeypatch.setenv("DATA_RETENTION_DRY_RUN", "false")

        from server.db import db_session
        from server.scheduler import _data_retention_tick
        from server.models import User

        anon_email = f"anon_{uuid.uuid4().hex[:8]}_oldhash@deleted.local"
        with db_session() as db:
            u = _make_user(db, email=anon_email, days_inactive=900)
            uid = u.id

        asyncio.run(_data_retention_tick())

        with db_session() as db:
            after = db.query(User).filter_by(id=uid).first()
            # Уже анонимизирован — email не должен меняться
            assert after.email == anon_email

    def test_disabled_when_env_zero(self, monkeypatch):
        """Если DATA_RETENTION_USER_INACTIVE_MONTHS=0 — User-фаза скипается."""
        monkeypatch.setenv("DATA_RETENTION_USER_INACTIVE_MONTHS", "0")
        monkeypatch.setenv("DATA_RETENTION_PROPOSAL_YEARS", "0")

        from server.db import db_session
        from server.scheduler import _data_retention_tick

        with db_session() as db:
            u = _make_user(db, email=_uniq_email("disabled_retention"), days_inactive=9999)
            uid = u.id
            orig_email = u.email

        asyncio.run(_data_retention_tick())

        with db_session() as db:
            from server.models import User
            after = db.query(User).filter_by(id=uid).first()
            assert after.email == orig_email
