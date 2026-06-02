"""Тесты для @aiche_bot — общий TG-бот платформы (Stage 1).

Покрывают:
  - Webhook secret check (404 на неправильный secret, 503 если не настроен)
  - /start auto-create User по tg_user_id
  - /start существующий юзер — реюз
  - callback "balance" / "topup" — корректные ответы
  - callback "menu_main" — возврат в главное меню
  - "В разработке" заглушка на menu_chat/image/video/settings
  - is_configured() — guard

Все TG API-вызовы мокаются через monkeypatch на _tg_call.
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


@pytest.fixture
def captured_tg_calls(monkeypatch):
    """Мокаем _tg_call — возвращает список (method, payload) для проверок."""
    from server import aiche_telegram_bot as bot
    calls: list[tuple[str, dict]] = []

    async def _fake_call(method: str, payload: dict):
        calls.append((method, payload))
        return {"ok": True, "result": {"message_id": 42}}

    monkeypatch.setattr(bot, "_tg_call", _fake_call)
    return calls


# ── is_configured ────────────────────────────────────────────────────────


class TestConfig:
    def test_configured_with_env(self):
        from server.aiche_telegram_bot import is_configured
        assert is_configured() is True

    def test_not_configured_without_token(self, monkeypatch):
        monkeypatch.delenv("AICHE_TG_BOT_TOKEN", raising=False)
        from server.aiche_telegram_bot import is_configured
        assert is_configured() is False

    def test_not_configured_without_secret(self, monkeypatch):
        monkeypatch.delenv("AICHE_TG_BOT_WEBHOOK_SECRET", raising=False)
        from server.aiche_telegram_bot import is_configured
        assert is_configured() is False


# ── /start auto-create ──────────────────────────────────────────────────


class TestStartAutoCreate:
    def test_start_creates_new_user(self, captured_tg_calls, monkeypatch):
        """Новый юзер через /start auto-created с trial-балансом."""
        from server import aiche_telegram_bot as bot
        from server import pricing as _pricing
        from server.models import User
        # Зануляем trial — get_price импортируется внутри функции, патчим в pricing.
        monkeypatch.setattr(_pricing, "get_price",
                            lambda k, default=0: 0 if "trial" in k else (default or 0))
        tg_uid = f"new-{uuid.uuid4().hex[:8]}"
        update = {
            "message": {
                "chat": {"id": 123},
                "text": "/start",
                "from": {"id": int(tg_uid.replace("new-", "")[:6], 16),
                          "username": "testuser",
                          "first_name": "Test", "last_name": "User"},
            }
        }
        tg_uid_num = str(update["message"]["from"]["id"])
        update["message"]["from"]["id"] = int(tg_uid_num)

        _run(bot.handle_update(update))

        # Юзер должен появиться в БД
        db = SessionLocal()
        try:
            u = db.query(User).filter_by(tg_user_id=tg_uid_num).first()
            assert u is not None, "Юзер должен быть auto-created"
            assert u.tg_username == "testuser"
            # Баланс 0 потому что мы зануливли trial
            assert u.tokens_balance == 0
        finally:
            db.close()

        # Отправилось sendMessage с inline-меню
        send_calls = [c for c in captured_tg_calls if c[0] == "sendMessage"]
        assert len(send_calls) >= 1
        payload = send_calls[0][1]
        assert payload["chat_id"] == "123"
        assert "0.00" in payload["text"]
        assert "inline_keyboard" in payload["reply_markup"]
        buttons_text = [b[0]["text"] for b in payload["reply_markup"]["inline_keyboard"]]
        assert "💰 Баланс" in buttons_text
        assert "💳 Пополнить" in buttons_text

    def test_start_grants_trial_by_default(self, captured_tg_calls):
        """Без monkeypatch'инга trial — новый юзер получает default 500 ₽."""
        from server import aiche_telegram_bot as bot
        from server.models import User, Transaction
        # Уникальный numeric tg_user_id из uuid hex
        tg_uid_num = int(uuid.uuid4().hex[:8], 16) % (10**9)
        update = {"message": {
            "chat": {"id": 555}, "text": "/start",
            "from": {"id": tg_uid_num, "username": "trialer"},
        }}
        _run(bot.handle_update(update))
        db = SessionLocal()
        try:
            u = db.query(User).filter_by(tg_user_id=str(tg_uid_num)).first()
            assert u is not None
            assert u.tokens_balance == 50_000  # default 500 ₽
            assert u.trial_ends_at is not None
            tx = (db.query(Transaction).filter_by(user_id=u.id, type="bonus")
                    .first())
            assert tx is not None and tx.tokens_delta == 50_000
        finally:
            db.close()

    def test_start_existing_user_reuses(self, captured_tg_calls):
        from server import aiche_telegram_bot as bot
        from server.models import User
        # Готовим существующего юзера
        suffix = uuid.uuid4().hex[:8]
        tg_uid = f"existing-{suffix}"
        db = SessionLocal()
        try:
            u = User(
                email=f"existing-{suffix}@test.com",
                password_hash=_FAKE_BCRYPT, name="Existing",
                tg_user_id=tg_uid, tokens_balance=99999,
                is_verified=True, agreed_to_terms=True,
                referral_code=uuid.uuid4().hex[:8].upper(),
            )
            db.add(u)
            db.commit()
            uid = u.id
        finally:
            db.close()

        update = {
            "message": {
                "chat": {"id": 456},
                "text": "/start",
                "from": {"id": tg_uid, "username": "exist"},
            }
        }
        _run(bot.handle_update(update))

        # Не должно быть второго юзера
        db = SessionLocal()
        try:
            count = db.query(User).filter_by(tg_user_id=tg_uid).count()
            assert count == 1
            # Баланс в сообщении должен отражать существующий
            send_calls = [c for c in captured_tg_calls if c[0] == "sendMessage"]
            assert any("999.99" in c[1].get("text", "") for c in send_calls)
        finally:
            db.close()


# ── Callbacks ────────────────────────────────────────────────────────────


def _setup_user_for_callback(tg_uid: str, balance_kop: int = 0) -> int:
    from server.models import User
    db = SessionLocal()
    try:
        u = User(
            email=f"cb-{tg_uid}@test.com",
            password_hash=_FAKE_BCRYPT, name="CB User",
            tg_user_id=tg_uid, tokens_balance=balance_kop,
            is_verified=True, agreed_to_terms=True,
            referral_code=uuid.uuid4().hex[:8].upper(),
        )
        db.add(u); db.commit(); db.refresh(u)
        return u.id
    finally:
        db.close()


class TestCallbacks:
    def test_balance_callback_shows_amount(self, captured_tg_calls):
        from server import aiche_telegram_bot as bot
        tg_uid = f"cb-bal-{uuid.uuid4().hex[:6]}"
        _setup_user_for_callback(tg_uid, balance_kop=54321)
        update = {
            "callback_query": {
                "id": "cb-1",
                "data": "balance",
                "from": {"id": tg_uid},
                "message": {"chat": {"id": 1}, "message_id": 100},
            }
        }
        _run(bot.handle_update(update))

        edit_calls = [c for c in captured_tg_calls if c[0] == "editMessageText"]
        assert len(edit_calls) == 1
        assert "543.21" in edit_calls[0][1]["text"]

        # И answerCallbackQuery был вызван (убрать «часики»)
        ack_calls = [c for c in captured_tg_calls if c[0] == "answerCallbackQuery"]
        assert len(ack_calls) == 1

    def test_topup_callback_returns_link(self, captured_tg_calls, monkeypatch):
        from server import aiche_telegram_bot as bot
        monkeypatch.setenv("APP_URL", "https://aiche.ru")
        tg_uid = f"cb-top-{uuid.uuid4().hex[:6]}"
        _setup_user_for_callback(tg_uid)
        update = {
            "callback_query": {
                "id": "cb-2", "data": "topup",
                "from": {"id": tg_uid},
                "message": {"chat": {"id": 1}, "message_id": 200},
            }
        }
        _run(bot.handle_update(update))

        edit_calls = [c for c in captured_tg_calls if c[0] == "editMessageText"]
        assert len(edit_calls) == 1
        assert "aiche.ru" in edit_calls[0][1]["text"]
        assert "topup" in edit_calls[0][1]["text"].lower()

    def test_menu_main_returns_to_root(self, captured_tg_calls):
        from server import aiche_telegram_bot as bot
        tg_uid = f"cb-main-{uuid.uuid4().hex[:6]}"
        _setup_user_for_callback(tg_uid, balance_kop=10000)
        update = {
            "callback_query": {
                "id": "cb-3", "data": "menu_main",
                "from": {"id": tg_uid},
                "message": {"chat": {"id": 1}, "message_id": 300},
            }
        }
        _run(bot.handle_update(update))

        edit_calls = [c for c in captured_tg_calls if c[0] == "editMessageText"]
        assert len(edit_calls) == 1
        kb = edit_calls[0][1]["reply_markup"]["inline_keyboard"]
        # 6 строк меню
        assert len(kb) == 6

    def test_unknown_callback_returns_to_menu(self, captured_tg_calls):
        """Неизвестный callback_data → fallback в главное меню.
        Stage 1-4 все callbacks теперь живые, заглушек не осталось."""
        from server import aiche_telegram_bot as bot
        tg_uid = f"cb-unk-{uuid.uuid4().hex[:6]}"
        _setup_user_for_callback(tg_uid)
        update = {"callback_query": {
            "id": "cb-unknown", "data": "totally_unknown_action_xyz",
            "from": {"id": tg_uid},
            "message": {"chat": {"id": 1}, "message_id": 400},
        }}
        _run(bot.handle_update(update))
        edit_calls = [c for c in captured_tg_calls if c[0] == "editMessageText"]
        assert len(edit_calls) == 1
        assert "Неизвестная" in edit_calls[0][1]["text"]


# ── Webhook secret check ─────────────────────────────────────────────────


class TestWebhookSecret:
    def test_correct_secret_processed(self, captured_tg_calls):
        from fastapi.testclient import TestClient
        from main import app
        client = TestClient(app)
        r = client.post("/webhook/aiche-tg/test-secret-123", json={})
        assert r.status_code == 200

    def test_wrong_secret_404(self):
        from fastapi.testclient import TestClient
        from main import app
        client = TestClient(app)
        r = client.post("/webhook/aiche-tg/wrong", json={})
        assert r.status_code == 404

    def test_no_secret_env_503(self, monkeypatch):
        monkeypatch.delenv("AICHE_TG_BOT_WEBHOOK_SECRET", raising=False)
        from fastapi.testclient import TestClient
        from main import app
        client = TestClient(app)
        r = client.post("/webhook/aiche-tg/anything", json={})
        assert r.status_code == 503
