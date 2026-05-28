"""Тесты auto-expire pending actions (cron-job).

Защищает UI от карточек, которые юзер не подтвердил и не отменил —
через 24ч они становятся expired и больше не отображают кнопок.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.db import SessionLocal


_FAKE_BCRYPT = "$2b$12$abcdefghijklmnopqrstuvCxyz0123456789ABCDEFGHIJKLMNOPQRSTU"


def _make_user(db, email: str):
    from server.models import User
    u = db.query(User).filter_by(email=email).first()
    if u:
        return u
    u = User(
        email=email, password_hash=_FAKE_BCRYPT, name=email.split("@")[0],
        tokens_balance=0, is_verified=True, agreed_to_terms=True,
        referral_code=uuid.uuid4().hex[:8].upper(),
    )
    db.add(u); db.commit(); db.refresh(u)
    return u


def _make_pending(db, *, user_id, status="pending", age_hours=0):
    from server.models import PendingAgentAction
    pa = PendingAgentAction(
        user_id=user_id, agent_id=None, module_slug="mail",
        action_type="send_email",
        params_json=json.dumps({"to": "x@y.ru", "subject": "S",
                                  "body": "B", "mailbox_id": 1},
                                 ensure_ascii=False),
        preview_text="preview",
        status=status,
        created_at=datetime.utcnow() - timedelta(hours=age_hours),
    )
    db.add(pa); db.commit(); db.refresh(pa)
    return pa


class TestExpireOldPendingActions:

    def test_old_pending_expired(self):
        from server.cron.agents_modules import expire_old_pending_actions
        from server.models import PendingAgentAction
        db = SessionLocal()
        try:
            u = _make_user(db, "expire-old@test.com")
            pa = _make_pending(db, user_id=u.id, status="pending", age_hours=25)
            old_id = pa.id
        finally:
            db.close()

        n = expire_old_pending_actions(ttl_hours=24)
        assert n >= 1

        db = SessionLocal()
        try:
            row = db.query(PendingAgentAction).get(old_id)
            assert row.status == "expired"
        finally:
            db.close()

    def test_fresh_pending_not_touched(self):
        from server.cron.agents_modules import expire_old_pending_actions
        from server.models import PendingAgentAction
        db = SessionLocal()
        try:
            u = _make_user(db, "expire-fresh@test.com")
            pa = _make_pending(db, user_id=u.id, status="pending", age_hours=1)
            fresh_id = pa.id
        finally:
            db.close()

        expire_old_pending_actions(ttl_hours=24)

        db = SessionLocal()
        try:
            row = db.query(PendingAgentAction).get(fresh_id)
            assert row.status == "pending"
        finally:
            db.close()

    def test_confirmed_not_touched(self):
        """Уже подтверждённые action'ы трогать нельзя — даже если очень старые."""
        from server.cron.agents_modules import expire_old_pending_actions
        from server.models import PendingAgentAction
        db = SessionLocal()
        try:
            u = _make_user(db, "expire-confirmed@test.com")
            pa = _make_pending(db, user_id=u.id, status="confirmed",
                               age_hours=999)
            confirmed_id = pa.id
        finally:
            db.close()

        expire_old_pending_actions(ttl_hours=24)

        db = SessionLocal()
        try:
            row = db.query(PendingAgentAction).get(confirmed_id)
            assert row.status == "confirmed"
        finally:
            db.close()

    def test_cancelled_not_touched(self):
        from server.cron.agents_modules import expire_old_pending_actions
        from server.models import PendingAgentAction
        db = SessionLocal()
        try:
            u = _make_user(db, "expire-cancelled@test.com")
            pa = _make_pending(db, user_id=u.id, status="cancelled",
                               age_hours=999)
            cancelled_id = pa.id
        finally:
            db.close()

        expire_old_pending_actions(ttl_hours=24)

        db = SessionLocal()
        try:
            row = db.query(PendingAgentAction).get(cancelled_id)
            assert row.status == "cancelled"
        finally:
            db.close()

    def test_custom_ttl(self):
        from server.cron.agents_modules import expire_old_pending_actions
        from server.models import PendingAgentAction
        db = SessionLocal()
        try:
            u = _make_user(db, "expire-custom-ttl@test.com")
            # 2 часа старая запись, TTL=1ч → должна expired
            pa = _make_pending(db, user_id=u.id, status="pending", age_hours=2)
            pa_id = pa.id
        finally:
            db.close()

        expire_old_pending_actions(ttl_hours=1)

        db = SessionLocal()
        try:
            row = db.query(PendingAgentAction).get(pa_id)
            assert row.status == "expired"
        finally:
            db.close()
