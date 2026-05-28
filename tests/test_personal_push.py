"""Тесты для push_to_user — autoresponder cron-режим.

Эмулируем юзера с подключённым TG/MAX/VK ботом и проверяем что push идёт
в нужные каналы. Реальных API-вызовов не делаем — мокаем sender'ы.
"""
from __future__ import annotations

import asyncio
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


def _run(coro):
    return asyncio.run(coro)


class TestPushToUser:

    def test_no_bots_no_delivery(self):
        from server.personal_bot_relay import push_to_user
        db = SessionLocal()
        try:
            u = _make_user(db, "push-no-bots@test.com",
                           personal_tg_bot_token=None,
                           personal_tg_chat_id=None)
            user_obj = u
            db.expunge(user_obj)
        finally:
            db.close()

        r = _run(push_to_user(user_obj, "тест"))
        assert r["delivered"] == 0
        assert r["channels"] == []

    def test_tg_delivery_success(self, monkeypatch):
        from server import personal_bot_relay as pbr
        called = {}

        async def _fake_tg_send(token, chat_id, text, parse_mode="HTML"):
            called["tg"] = (token, chat_id, text, parse_mode)
            return True

        monkeypatch.setattr(pbr, "tg_send_message", _fake_tg_send)

        db = SessionLocal()
        try:
            u = _make_user(db, "push-tg-ok@test.com",
                           personal_tg_bot_token="123:abc",
                           personal_tg_chat_id="987654321")
            user_obj = u
            db.expunge(user_obj)
        finally:
            db.close()

        r = _run(pbr.push_to_user(user_obj, "🧩 coach: что сегодня?"))
        assert r["delivered"] == 1
        assert r["channels"] == ["tg"]
        assert called["tg"][1] == "987654321"
        assert "coach" in called["tg"][2]

    def test_tg_failure_logged_not_raised(self, monkeypatch):
        from server import personal_bot_relay as pbr

        async def _bad_tg(token, chat_id, text, parse_mode="HTML"):
            return False  # сетевая ошибка

        monkeypatch.setattr(pbr, "tg_send_message", _bad_tg)

        db = SessionLocal()
        try:
            u = _make_user(db, "push-tg-fail@test.com",
                           personal_tg_bot_token="123:abc",
                           personal_tg_chat_id="111")
            user_obj = u
            db.expunge(user_obj)
        finally:
            db.close()

        r = _run(pbr.push_to_user(user_obj, "test"))
        assert r["delivered"] == 0

    def test_tg_and_max_both_delivered(self, monkeypatch):
        from server import personal_bot_relay as pbr
        calls = []

        async def _tg(token, chat_id, text, parse_mode="HTML"):
            calls.append("tg")
            return True

        async def _max(token, user_id, text):
            calls.append("max")
            return True

        monkeypatch.setattr(pbr, "tg_send_message", _tg)
        monkeypatch.setattr(pbr, "max_send_message", _max)

        db = SessionLocal()
        try:
            u = _make_user(db, "push-tg-max@test.com",
                           personal_tg_bot_token="t",
                           personal_tg_chat_id="111",
                           personal_max_bot_token="mt",
                           personal_max_user_id="222")
            user_obj = u
            db.expunge(user_obj)
        finally:
            db.close()

        r = _run(pbr.push_to_user(user_obj, "test"))
        assert r["delivered"] == 2
        assert "tg" in r["channels"] and "max" in r["channels"]
        assert calls == ["tg", "max"]

    def test_only_max_when_tg_missing(self, monkeypatch):
        from server import personal_bot_relay as pbr
        calls = []

        async def _max(token, user_id, text):
            calls.append("max")
            return True

        monkeypatch.setattr(pbr, "max_send_message", _max)

        db = SessionLocal()
        try:
            u = _make_user(db, "push-only-max@test.com",
                           personal_tg_bot_token=None,
                           personal_tg_chat_id=None,
                           personal_max_bot_token="mt",
                           personal_max_user_id="888")
            user_obj = u
            db.expunge(user_obj)
        finally:
            db.close()

        r = _run(pbr.push_to_user(user_obj, "test"))
        assert r["delivered"] == 1
        assert r["channels"] == ["max"]
