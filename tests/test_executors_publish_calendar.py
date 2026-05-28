"""Тесты для оставшихся 2 executor'ов (полные unit-тесты):

  - publish_to_creators: создание ContentItem + (опц.) auto-publish через
    creators_publisher (мокаем).
  - create_google_event: refresh_token flow + httpx mock + сохранение
    UserCalendarConnection.

Цель — покрыть полный путь executor'ов, не только нижние функции.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock

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


# ── publish_to_creators ──────────────────────────────────────────────────────


class TestPublishToCreators:

    def _make_brand(self, db, user_id, name="Test Brand"):
        from server.models import CreatorBrand
        b = (db.query(CreatorBrand)
               .filter_by(user_id=user_id, name=name).first())
        if not b:
            b = CreatorBrand(user_id=user_id, name=name, niche="it")
            db.add(b); db.commit(); db.refresh(b)
        return b

    def test_missing_brand_id_rejected(self):
        from server.agent_actions import execute_action
        r = execute_action("publish_to_creators", {
            "platform": "tg", "body": "тест",
        }, user_id=1)
        assert r["ok"] is False
        assert "brand_id" in (r.get("error") or "")

    def test_unsupported_platform_rejected(self):
        from server.agent_actions import execute_action
        r = execute_action("publish_to_creators", {
            "brand_id": 1, "platform": "snapchat", "body": "тест",
        }, user_id=1)
        assert r["ok"] is False
        assert "платформа" in (r.get("error") or "").lower()

    def test_empty_body_rejected(self):
        from server.agent_actions import execute_action
        r = execute_action("publish_to_creators", {
            "brand_id": 1, "platform": "tg", "body": "   ",
        }, user_id=1)
        assert r["ok"] is False
        assert "body" in (r.get("error") or "").lower()

    def test_brand_not_owned_rejected(self):
        from server.agent_actions import execute_action
        # brand_id принадлежит другому юзеру → не находим
        db = SessionLocal()
        try:
            owner = _make_user(db, "publ-owner@test.com")
            other = _make_user(db, "publ-other@test.com")
            from server.models import CreatorBrand
            b = CreatorBrand(user_id=owner.id, name="Owned",
                              niche="it")
            db.add(b); db.commit(); db.refresh(b)
            brand_id = b.id
            attacker_id = other.id
        finally:
            db.close()

        r = execute_action("publish_to_creators", {
            "brand_id": brand_id, "platform": "tg", "body": "test",
        }, user_id=attacker_id)
        assert r["ok"] is False
        assert "не найден" in (r.get("error") or "")

    def test_future_schedule_creates_item_but_not_published(self):
        from server.agent_actions import execute_action
        from server.models import ContentItem

        db = SessionLocal()
        try:
            u = _make_user(db, "publ-future@test.com")
            b = self._make_brand(db, u.id, "Future Brand")
            uid, bid = u.id, b.id
        finally:
            db.close()

        r = execute_action("publish_to_creators", {
            "brand_id": bid, "platform": "tg", "type": "text",
            "schedule_at": "2099-12-31T10:00:00Z",
            "body": "Будущий пост.",
        }, uid)
        assert r["ok"] is True
        assert r["result"]["published"] is False

        db = SessionLocal()
        try:
            item = db.query(ContentItem).get(r["result"]["item_id"])
            assert item is not None
            assert item.platform == "tg"
            assert item.status == "ready"
            assert "Будущий пост" in (item.prepared_content_md or "")
        finally:
            db.close()

    def test_immediate_publish_calls_publisher(self, monkeypatch):
        """schedule_at = в прошлом + tg/vk → должен дёрнуть creators_publisher."""
        from server.agent_actions import execute_action
        from server.models import ContentItem
        from server import creators_publisher as cp

        db = SessionLocal()
        try:
            u = _make_user(db, "publ-immediate@test.com")
            b = self._make_brand(db, u.id, "Immediate Brand")
            uid, bid = u.id, b.id
        finally:
            db.close()

        called = {}

        async def _fake_publish(db, item):
            called["item_id"] = item.id
            called["brand_id"] = item.calendar_id  # для проверки что объект пришёл
            return {"ok": True, "external_post_id": "tg:42"}

        monkeypatch.setattr(cp, "publish_item", _fake_publish)

        r = execute_action("publish_to_creators", {
            "brand_id": bid, "platform": "tg", "type": "text",
            # без schedule_at → now → immediate
            "body": "Срочный пост.",
        }, uid)
        assert r["ok"] is True, r
        assert r["result"]["published"] is True
        assert r["result"]["external_post_id"] == "tg:42"
        assert called["item_id"] == r["result"]["item_id"]

    def test_publish_failure_keeps_item_in_calendar(self, monkeypatch):
        """Если publisher упал — item всё равно остаётся в календаре."""
        from server.agent_actions import execute_action
        from server.models import ContentItem
        from server import creators_publisher as cp

        db = SessionLocal()
        try:
            u = _make_user(db, "publ-fail@test.com")
            b = self._make_brand(db, u.id, "Fail Brand")
            uid, bid = u.id, b.id
        finally:
            db.close()

        async def _bad_publish(db, item):
            return {"ok": False, "description": "Telegram отверг сообщение"}

        monkeypatch.setattr(cp, "publish_item", _bad_publish)

        r = execute_action("publish_to_creators", {
            "brand_id": bid, "platform": "tg", "type": "text",
            "body": "Уйдёт в календарь.",
        }, uid)
        # ContentItem создан → executor говорит ok=True, но published=False
        assert r["ok"] is True
        assert r["result"]["published"] is False
        assert "publish_error" in r["result"]


# ── create_google_event ──────────────────────────────────────────────────────


def _mock_httpx_async_client(responses):
    """Helper: возвращает MagicMock который при `async with httpx.AsyncClient()`
    выдаёт mock-объект с указанным набором responses.
    `responses` = dict {'post': mock, 'get': mock}.
    """
    mock_client = MagicMock()
    if "post" in responses:
        mock_client.post = AsyncMock(return_value=responses["post"])
    if "get" in responses:
        mock_client.get = AsyncMock(return_value=responses["get"])
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    return mock_client


class TestCreateGoogleEventExecutor:

    def _mk_conn(self, db, user_id, refresh="rt-123", access="at-old"):
        from server.models import UserCalendarConnection
        # Удаляем старые
        db.query(UserCalendarConnection).filter_by(
            user_id=user_id, provider="google",
        ).delete()
        db.commit()
        conn = UserCalendarConnection(
            user_id=user_id, provider="google",
            account_email="me@gmail.com", calendar_id="primary",
            access_token=access, refresh_token=refresh,
            is_active=True,
        )
        db.add(conn); db.commit(); db.refresh(conn)
        return conn

    def test_no_connection_rejected(self):
        from server.agent_actions import execute_action
        db = SessionLocal()
        try:
            u = _make_user(db, "cal-no-conn@test.com")
            from server.models import UserCalendarConnection
            db.query(UserCalendarConnection).filter_by(
                user_id=u.id, provider="google",
            ).delete()
            db.commit()
            uid = u.id
        finally:
            db.close()

        r = execute_action("create_google_event", {
            "title": "Test", "start": "2026-06-05T14:00:00+03:00",
        }, uid)
        assert r["ok"] is False
        assert "подключение" in (r.get("error") or "").lower() or \
            "Google" in (r.get("error") or "")

    def test_missing_title_rejected(self):
        from server.agent_actions import execute_action
        r = execute_action("create_google_event", {
            "start": "2026-06-05T14:00:00+03:00",
        }, user_id=1)
        assert r["ok"] is False
        assert "title" in (r.get("error") or "").lower() or \
            "название" in (r.get("error") or "").lower()

    def test_missing_start_rejected(self):
        from server.agent_actions import execute_action
        r = execute_action("create_google_event", {
            "title": "Test",
        }, user_id=1)
        assert r["ok"] is False
        assert "start" in (r.get("error") or "").lower() or \
            "начала" in (r.get("error") or "").lower()

    def test_successful_create_with_refresh(self, monkeypatch):
        """Полный flow: refresh_token → access_token → create event."""
        from server.agent_actions import execute_action

        monkeypatch.setenv("GOOGLE_CLIENT_ID", "tid")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "tsecret")

        db = SessionLocal()
        try:
            u = _make_user(db, "cal-create-ok@test.com")
            self._mk_conn(db, u.id)
            uid = u.id
        finally:
            db.close()

        # Mock httpx — два разных вызова: refresh POST + create POST
        refresh_resp = MagicMock()
        refresh_resp.status_code = 200
        refresh_resp.json = MagicMock(return_value={
            "access_token": "at-new-fresh", "expires_in": 3600,
        })
        create_resp = MagicMock()
        create_resp.status_code = 200
        create_resp.json = MagicMock(return_value={
            "id": "abc123", "htmlLink": "https://calendar.google.com/event?eid=zz",
        })

        # httpx.AsyncClient вызывается дважды — для refresh и для create.
        # Возвращаем клиенты последовательно.
        clients = iter([
            _mock_httpx_async_client({"post": refresh_resp}),
            _mock_httpx_async_client({"post": create_resp}),
        ])

        import httpx as _httpx
        monkeypatch.setattr(_httpx, "AsyncClient",
                            lambda **kw: next(clients))

        r = execute_action("create_google_event", {
            "title": "Встреча",
            "start": "2026-06-05T14:00:00+03:00",
            "end": "2026-06-05T15:00:00+03:00",
            "location": "Zoom",
        }, uid)
        assert r["ok"] is True, r
        assert r["result"]["event_id"] == "abc123"
        assert r["result"]["title"] == "Встреча"
        assert "calendar.google.com" in r["result"]["html_link"]

    def test_403_readonly_friendly_error(self, monkeypatch):
        """Старый connection со scope=readonly → 403 → friendly hint."""
        from server.agent_actions import execute_action

        monkeypatch.setenv("GOOGLE_CLIENT_ID", "tid")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "tsecret")

        db = SessionLocal()
        try:
            u = _make_user(db, "cal-readonly-403@test.com")
            self._mk_conn(db, u.id)
            uid = u.id
        finally:
            db.close()

        refresh_resp = MagicMock()
        refresh_resp.status_code = 200
        refresh_resp.json = MagicMock(return_value={
            "access_token": "at-readonly", "expires_in": 3600,
        })
        create_resp = MagicMock()
        create_resp.status_code = 403
        create_resp.text = "Insufficient permissions"

        clients = iter([
            _mock_httpx_async_client({"post": refresh_resp}),
            _mock_httpx_async_client({"post": create_resp}),
        ])
        import httpx as _httpx
        monkeypatch.setattr(_httpx, "AsyncClient", lambda **kw: next(clients))

        r = execute_action("create_google_event", {
            "title": "X", "start": "2026-06-05T14:00:00+03:00",
        }, uid)
        assert r["ok"] is False
        assert "переподключи" in (r.get("error") or "").lower()

    def test_refresh_failure_friendly_error(self, monkeypatch):
        """Если refresh не вернул access_token — friendly error."""
        from server.agent_actions import execute_action

        db = SessionLocal()
        try:
            u = _make_user(db, "cal-refresh-fail@test.com")
            self._mk_conn(db, u.id, refresh="bad-rt")
            uid = u.id
        finally:
            db.close()

        # google_refresh_access_token при 400 вернёт None — мокнем
        from server import calendar_sync as cs
        async def _bad_refresh(rt):
            return None
        monkeypatch.setattr(cs, "google_refresh_access_token", _bad_refresh)

        r = execute_action("create_google_event", {
            "title": "X", "start": "2026-06-05T14:00:00+03:00",
        }, uid)
        assert r["ok"] is False
        assert "переподключ" in (r.get("error") or "").lower() or \
            "обновить" in (r.get("error") or "").lower()
