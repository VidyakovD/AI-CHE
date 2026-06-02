"""Тесты deep-link привязки @aiche_bot ↔ aiche.ru-аккаунта (Вариант 1).

Flow:
  1. Юзер логинится на aiche.ru → POST /user/tg-link/aiche-bot/code
     → получает {code: "AB12CD", deep_link: "t.me/<bot>?start=LINK_AB12CD"}
  2. Юзер открывает deep_link → TG показывает @aiche_bot с pre-filled
     /start LINK_AB12CD
  3. Бот receives /start LINK_AB12CD → server.link_codes.redeem_code →
     User.tg_user_id обновляется
  4. Если у этого tg_user_id уже был auto-created анонимный User —
     баланс переносится на основной (target_user_id) и старый деактивируется

Покрывает:
  - link_codes.issue_code + redeem_code (helpers)
  - merge auto-account с balance transfer
  - bot handle /start LINK_<code> happy path
  - expired code
  - wrong code
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


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _set_bot_env(monkeypatch):
    monkeypatch.setenv("AICHE_TG_BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("AICHE_TG_BOT_WEBHOOK_SECRET", "test-secret-123")
    monkeypatch.setenv("AICHE_TG_BOT_USERNAME", "aiche_bot_test")


@pytest.fixture
def captured_tg_calls(monkeypatch):
    from server import aiche_telegram_bot as bot
    calls: list[tuple[str, dict]] = []

    async def _fake_call(method: str, payload: dict):
        calls.append((method, payload))
        return {"ok": True, "result": {"message_id": 42}}

    monkeypatch.setattr(bot, "_tg_call", _fake_call)
    return calls


@pytest.fixture(autouse=True)
def _reset_link_codes():
    from server.link_codes import _reset_for_tests
    _reset_for_tests()
    yield
    _reset_for_tests()


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


# ── link_codes module ────────────────────────────────────────────────────


class TestLinkCodesModule:
    def test_issue_returns_6_char_code(self):
        from server.link_codes import issue_code
        code = issue_code(42, "tg_user_id")
        assert isinstance(code, str)
        assert len(code) == 6

    def test_issue_redeem_roundtrip(self):
        from server.link_codes import issue_code, redeem_code
        code = issue_code(99, "tg_user_id")
        result = redeem_code(code, "tg-user-123456")
        assert result == (99, "tg_user_id")

    def test_redeem_is_single_use(self):
        from server.link_codes import issue_code, redeem_code
        code = issue_code(99, "tg_user_id")
        first = redeem_code(code, "value")
        second = redeem_code(code, "value")
        assert first is not None
        assert second is None  # код потреблён

    def test_redeem_unknown_code_returns_none(self):
        from server.link_codes import redeem_code
        assert redeem_code("ZZZZZZ", "x") is None

    def test_redeem_expired_code(self, monkeypatch):
        """Истёкший код не должен redeem'иться."""
        import time
        from server.link_codes import issue_code, redeem_code
        code = issue_code(1, "tg_user_id", ttl_sec=1)
        # Сразу проверим что работает
        assert redeem_code(code, "x") is not None
        # Новый код, потом «подкручиваем время»
        code2 = issue_code(2, "tg_user_id", ttl_sec=1)
        monkeypatch.setattr(time, "monotonic",
                             lambda: time.monotonic.__wrapped__() + 1000
                                       if hasattr(time.monotonic, "__wrapped__")
                                       else 10**9)
        assert redeem_code(code2, "x") is None

    def test_invalid_kind_raises(self):
        from server.link_codes import issue_code
        with pytest.raises(ValueError):
            issue_code(1, "fingerprint")


# ── Web endpoint /user/tg-link/aiche-bot/code ────────────────────────────


class TestWebEndpoint:
    def test_no_bot_username_returns_503(self, monkeypatch):
        monkeypatch.delenv("AICHE_TG_BOT_USERNAME", raising=False)
        from fastapi.testclient import TestClient
        from server.auth import create_token
        from main import app
        suffix = uuid.uuid4().hex[:8]
        db = SessionLocal()
        try:
            u = _make_user(db, f"link-noenv-{suffix}@test.com")
            uid = u.id
        finally:
            db.close()
        token = create_token(uid, f"link-noenv-{suffix}@test.com")
        client = TestClient(app)
        r = client.post("/user/tg-link/aiche-bot/code",
                          headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 503

    def test_authed_returns_code_and_deeplink(self, monkeypatch):
        from fastapi.testclient import TestClient
        from server.auth import create_token
        from main import app
        suffix = uuid.uuid4().hex[:8]
        email = f"link-code-{suffix}@test.com"
        db = SessionLocal()
        try:
            u = _make_user(db, email)
            uid = u.id
        finally:
            db.close()
        token = create_token(uid, email)
        client = TestClient(app)
        r = client.post("/user/tg-link/aiche-bot/code",
                          headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert "code" in data and len(data["code"]) == 6
        assert data["bot_username"] == "aiche_bot_test"
        assert f"start=LINK_{data['code']}" in data["deep_link"]


# ── Bot handles /start LINK_<code> ───────────────────────────────────────


class TestBotDeeplink:
    def test_start_link_redeems_and_attaches_tg(self, captured_tg_calls):
        """Юзер залогинен на сайте → получил код → пришёл в бот → tg_user_id
        привязан к существующему User."""
        from server import aiche_telegram_bot as bot
        from server.link_codes import issue_code
        from server.models import User

        # Готовим существующего юзера на сайте (без tg_user_id)
        suffix = uuid.uuid4().hex[:8]
        db = SessionLocal()
        try:
            u = _make_user(db, f"deeplink-{suffix}@test.com", tokens_balance=15000)
            uid = u.id
        finally:
            db.close()

        # Юзер на сайте генерирует код
        code = issue_code(uid, "tg_user_id")

        # Юзер кликает deep_link → TG отправляет /start LINK_<code>
        tg_uid = f"new-tg-{suffix}"
        update = {
            "message": {
                "chat": {"id": 999},
                "text": f"/start LINK_{code}",
                "from": {"id": tg_uid, "username": "alice"},
            }
        }
        _run(bot.handle_update(update))

        # tg_user_id привязан к нашему User
        db = SessionLocal()
        try:
            target = db.query(User).filter_by(id=uid).first()
            assert target.tg_user_id == tg_uid
            assert target.tg_username == "alice"
        finally:
            db.close()

        # Бот ответил с подтверждением и меню
        send_calls = [c for c in captured_tg_calls if c[0] == "sendMessage"]
        assert len(send_calls) == 1
        assert "Привязано" in send_calls[0][1]["text"]
        assert "150.00" in send_calls[0][1]["text"]  # баланс показан

    def test_start_link_merges_auto_account_balance(self, captured_tg_calls):
        """Если у tg_user_id был auto-created анонимный User с балансом —
        этот баланс переносится на основной аккаунт, auto-юзер деактивируется."""
        from server import aiche_telegram_bot as bot
        from server.link_codes import issue_code
        from server.models import User

        suffix = uuid.uuid4().hex[:8]
        tg_uid = f"auto-tg-{suffix}"
        # Шаг 1: создаём auto-account через первый /start (без LINK)
        update1 = {"message": {
            "chat": {"id": 111}, "text": "/start",
            "from": {"id": tg_uid, "username": "bob"},
        }}
        _run(bot.handle_update(update1))
        # Пополняем auto-account
        db = SessionLocal()
        try:
            auto = db.query(User).filter_by(tg_user_id=tg_uid).first()
            assert auto is not None
            auto.tokens_balance = 50000  # 500 ₽
            db.commit()
            auto_id = auto.id
        finally:
            db.close()

        # Шаг 2: тот же человек на сайте логинится отдельным аккаунтом
        db = SessionLocal()
        try:
            site = _make_user(db, f"merge-site-{suffix}@test.com",
                               tokens_balance=10000)
            site_id = site.id
        finally:
            db.close()
        code = issue_code(site_id, "tg_user_id")

        # Шаг 3: юзер в TG жмёт «привязать» → /start LINK_<code>
        captured_tg_calls.clear()
        update2 = {"message": {
            "chat": {"id": 111}, "text": f"/start LINK_{code}",
            "from": {"id": tg_uid, "username": "bob"},
        }}
        _run(bot.handle_update(update2))

        # Site-account теперь имеет tg_user_id и баланс 100 + 500 = 600 ₽
        db = SessionLocal()
        try:
            site_after = db.query(User).filter_by(id=site_id).first()
            assert site_after.tg_user_id == tg_uid
            assert site_after.tokens_balance == 60000

            # Auto-account освобождён (tg_user_id убран, is_active=False)
            auto_after = db.query(User).filter_by(id=auto_id).first()
            assert auto_after.tg_user_id is None
            assert auto_after.is_active is False
        finally:
            db.close()

    def test_start_with_invalid_code(self, captured_tg_calls):
        from server import aiche_telegram_bot as bot
        update = {"message": {
            "chat": {"id": 222},
            "text": "/start LINK_BADBAD",
            "from": {"id": f"bad-{uuid.uuid4().hex[:6]}"},
        }}
        _run(bot.handle_update(update))
        send_calls = [c for c in captured_tg_calls if c[0] == "sendMessage"]
        # Сообщение об устаревшем коде
        assert any("устарел" in c[1].get("text", "").lower()
                   for c in send_calls)
