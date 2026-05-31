"""Тесты на миграцию notify_user — приоритет personal_tg_bot_token над legacy
tg_user_id (2026-05 архитектурный переход «общий бот» → «свой бот»).

Не делаем реальных HTTP-вызовов — мокаем send_personal_tg_sync /
send_message_sync через monkeypatch.
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.db import SessionLocal


_FAKE_BCRYPT = "$2b$12$abcdefghijklmnopqrstuvCxyz0123456789ABCDEFGHIJKLMNOPQRSTU"


def _make_user(db, email: str, **extra):
    from server.models import User
    u = db.query(User).filter_by(email=email).first()
    if not u:
        u = User(
            email=email, password_hash=_FAKE_BCRYPT, name=email.split("@")[0],
            tokens_balance=0, is_verified=True, agreed_to_terms=True,
            referral_code=uuid.uuid4().hex[:8].upper(),
        )
        db.add(u)
    for k, v in extra.items():
        setattr(u, k, v)
    db.commit(); db.refresh(u)
    return u


class TestNotifyUserMigration:
    """Современная архитектура: personal bot приоритетнее legacy."""

    def test_personal_used_when_available(self, monkeypatch):
        """Если у юзера задан personal_tg_bot_token + personal_tg_chat_id —
        notify_user шлёт через них, НЕ через legacy."""
        from server import tg_management
        suffix = uuid.uuid4().hex[:8]
        db = SessionLocal()
        try:
            u = _make_user(
                db, f"notify-personal-{suffix}@test.com",
                personal_tg_bot_token="bot-token-abc",
                personal_tg_chat_id=f"chat-{suffix}",
                tg_user_id=f"legacy-{suffix}",  # legacy тоже есть, но не должен использоваться
            )
            uid = u.id
            expected_chat = u.personal_tg_chat_id
            expected_token = u.personal_tg_bot_token
        finally:
            db.close()

        calls = {"personal": [], "legacy": []}

        def _fake_personal(token, chat_id, text, reply_markup=None,
                           parse_mode="HTML"):
            calls["personal"].append({"token": token, "chat_id": chat_id,
                                       "text": text})
            return True

        def _fake_legacy(tg_user_id, text, reply_markup=None, parse_mode="HTML"):
            calls["legacy"].append({"tg_user_id": tg_user_id, "text": text})
            return True

        monkeypatch.setattr(tg_management, "send_personal_tg_sync", _fake_personal)
        monkeypatch.setattr(tg_management, "send_message_sync", _fake_legacy)

        ok = tg_management.notify_user(uid, "Test message", kind="info")
        assert ok is True
        assert len(calls["personal"]) == 1
        assert calls["personal"][0]["chat_id"] == expected_chat
        assert calls["personal"][0]["token"] == expected_token
        assert len(calls["legacy"]) == 0, "Legacy не должен использоваться когда есть personal"

    def test_falls_back_to_legacy_when_no_personal(self, monkeypatch):
        """Юзер без personal-bot, только с legacy tg_user_id —
        notify_user идёт через legacy."""
        from server import tg_management
        suffix = uuid.uuid4().hex[:8]
        db = SessionLocal()
        try:
            u = _make_user(
                db, f"notify-legacy-{suffix}@test.com",
                personal_tg_bot_token=None,
                personal_tg_chat_id=None,
                tg_user_id=f"legacy-only-{suffix}",
            )
            uid = u.id
            expected_legacy = u.tg_user_id
        finally:
            db.close()

        calls = {"personal": 0, "legacy": []}

        def _fake_personal(*a, **kw):
            calls["personal"] += 1
            return True

        def _fake_legacy(tg_user_id, text, reply_markup=None, parse_mode="HTML"):
            calls["legacy"].append({"tg_user_id": tg_user_id, "text": text})
            return True

        monkeypatch.setattr(tg_management, "send_personal_tg_sync", _fake_personal)
        monkeypatch.setattr(tg_management, "send_message_sync", _fake_legacy)

        ok = tg_management.notify_user(uid, "Legacy hello", kind="info")
        assert ok is True
        assert calls["personal"] == 0
        assert len(calls["legacy"]) == 1
        assert calls["legacy"][0]["tg_user_id"] == expected_legacy

    def test_personal_failure_falls_back_to_legacy(self, monkeypatch):
        """Personal-bot есть, но `sendMessage` упал (например юзер заблокировал
        свой бот) → пробуем legacy. Без потери уведомления."""
        from server import tg_management
        suffix = uuid.uuid4().hex[:8]
        db = SessionLocal()
        try:
            u = _make_user(
                db, f"notify-fallback-{suffix}@test.com",
                personal_tg_bot_token="bot-token",
                personal_tg_chat_id=f"chat-fb-{suffix}",
                tg_user_id=f"legacy-fb-{suffix}",
            )
            uid = u.id
            expected_legacy = u.tg_user_id
        finally:
            db.close()

        legacy_sent = []

        def _personal_fail(*a, **kw):
            return False  # bot заблокирован

        def _legacy_ok(tg_user_id, text, reply_markup=None, parse_mode="HTML"):
            legacy_sent.append(tg_user_id)
            return True

        monkeypatch.setattr(tg_management, "send_personal_tg_sync", _personal_fail)
        monkeypatch.setattr(tg_management, "send_message_sync", _legacy_ok)

        ok = tg_management.notify_user(uid, "Try fallback", kind="info")
        assert ok is True
        assert legacy_sent == [expected_legacy]

    def test_no_target_returns_false(self, monkeypatch):
        """Юзер вообще без TG-привязки — возвращаем False, ничего не шлём."""
        from server import tg_management
        db = SessionLocal()
        try:
            u = _make_user(
                db, f"notify-none-{uuid.uuid4().hex[:6]}@test.com",
                personal_tg_bot_token=None,
                personal_tg_chat_id=None,
                tg_user_id=None,
            )
            uid = u.id
        finally:
            db.close()

        called = {"personal": 0, "legacy": 0}
        monkeypatch.setattr(tg_management, "send_personal_tg_sync",
                            lambda *a, **kw: (called.update(personal=called["personal"]+1) or True))
        monkeypatch.setattr(tg_management, "send_message_sync",
                            lambda *a, **kw: (called.update(legacy=called["legacy"]+1) or True))

        ok = tg_management.notify_user(uid, "No target", kind="info")
        assert ok is False
        assert called["personal"] == 0
        assert called["legacy"] == 0

    def test_kind_toggle_blocks_proposals_when_disabled(self, monkeypatch):
        """Юзер выключил tg_notify_proposals → push типа 'proposals' не уходит,
        даже если есть и personal, и legacy каналы."""
        from server import tg_management
        suffix = uuid.uuid4().hex[:8]
        db = SessionLocal()
        try:
            u = _make_user(
                db, f"notify-toggle-{suffix}@test.com",
                personal_tg_bot_token="t",
                personal_tg_chat_id=f"c-{suffix}",
                tg_user_id=f"legacy-tog-{suffix}",
                tg_notify_proposals=False,
            )
            uid = u.id
        finally:
            db.close()

        sent = []
        monkeypatch.setattr(tg_management, "send_personal_tg_sync",
                            lambda *a, **kw: sent.append("p") or True)
        monkeypatch.setattr(tg_management, "send_message_sync",
                            lambda *a, **kw: sent.append("l") or True)

        ok = tg_management.notify_user(uid, "КП открыт", kind="proposals")
        assert ok is False
        assert sent == []

    def test_kind_info_ignores_toggle(self, monkeypatch):
        """Системные уведомления (kind='info') шлются всегда, даже если все
        toggle-флаги выключены — это критичные алерты."""
        from server import tg_management
        db = SessionLocal()
        try:
            u = _make_user(
                db, f"notify-info-{uuid.uuid4().hex[:6]}@test.com",
                personal_tg_bot_token="t",
                personal_tg_chat_id="c",
                tg_notify_proposals=False,
                tg_notify_records=False,
                tg_notify_errors=False,
            )
            uid = u.id
        finally:
            db.close()

        called = []
        monkeypatch.setattr(tg_management, "send_personal_tg_sync",
                            lambda *a, **kw: called.append(1) or True)
        monkeypatch.setattr(tg_management, "send_message_sync",
                            lambda *a, **kw: called.append(2) or True)

        ok = tg_management.notify_user(uid, "Critical", kind="info")
        assert ok is True
        assert called == [1]  # personal путь, без toggle-блока

    def test_nonexistent_user_returns_false(self, monkeypatch):
        """Несуществующий user_id — return False, не падать."""
        from server import tg_management
        called = []
        monkeypatch.setattr(tg_management, "send_personal_tg_sync",
                            lambda *a, **kw: called.append(1) or True)
        monkeypatch.setattr(tg_management, "send_message_sync",
                            lambda *a, **kw: called.append(2) or True)
        ok = tg_management.notify_user(999999999, "ghost", kind="info")
        assert ok is False
        assert called == []


# ── send_personal_tg_sync helper unit-test ──────────────────────────────────


class TestSendPersonalTgSync:
    def test_returns_false_on_empty_token(self):
        from server.tg_management import send_personal_tg_sync
        assert send_personal_tg_sync("", "123", "text") is False

    def test_returns_false_on_empty_chat_id(self):
        from server.tg_management import send_personal_tg_sync
        assert send_personal_tg_sync("token", "", "text") is False

    def test_calls_httpx_with_correct_url(self, monkeypatch):
        from server import tg_management
        captured = {}

        class _FakeResp:
            status_code = 200
            text = ""

        class _FakeClient:
            def __init__(self, **kw): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def post(self, url, json=None):
                captured["url"] = url
                captured["json"] = json
                return _FakeResp()

        import httpx
        monkeypatch.setattr(httpx, "Client", _FakeClient)

        ok = tg_management.send_personal_tg_sync(
            "BOT-T0KEN", "98765", "Привет!", parse_mode="HTML",
        )
        assert ok is True
        assert "BOT-T0KEN/sendMessage" in captured["url"]
        assert captured["json"]["chat_id"] == "98765"
        assert captured["json"]["text"] == "Привет!"
