"""Stage 4 тесты: settings submenu в @aiche_bot.

Покрывает:
  - menu_settings → 3 кнопки
  - settings_profile → readonly info
  - settings_default_model → submenu с ⭐ на текущей
  - settings_set_model:<id> → персист в User.tg_default_chat_model
  - settings_unlink → confirm → settings_unlink_confirm → tg_user_id=None
  - menu_chat подсветка ⭐ на дефолтной
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
    from server import aiche_telegram_bot as bot
    calls: list[tuple[str, dict]] = []

    async def _fake_call(method: str, payload: dict):
        calls.append((method, payload))
        return {"ok": True, "result": {"message_id": 42}}

    monkeypatch.setattr(bot, "_tg_call", _fake_call)
    return calls


def _make_user(tg_uid: str, **extra) -> int:
    from server.models import User
    db = SessionLocal()
    try:
        defaults = {
            "email": f"set-{tg_uid}-{uuid.uuid4().hex[:6]}@test.com",
            "password_hash": _FAKE_BCRYPT,
            "name": "Settings User",
            "tg_user_id": tg_uid,
            "tokens_balance": 10000,
            "is_verified": True,
            "agreed_to_terms": True,
            "referral_code": uuid.uuid4().hex[:8].upper(),
        }
        defaults.update(extra)
        u = User(**defaults)
        db.add(u); db.commit(); db.refresh(u)
        return u.id
    finally:
        db.close()


# ── Главное Settings submenu ────────────────────────────────────────────


class TestSettingsMenu:
    def test_menu_settings_shows_3_buttons(self, captured_tg_calls):
        from server import aiche_telegram_bot as bot
        tg_uid = f"set-menu-{uuid.uuid4().hex[:6]}"
        _make_user(tg_uid)
        update = {"callback_query": {
            "id": "cb-s1", "data": "menu_settings",
            "from": {"id": tg_uid},
            "message": {"chat": {"id": 1}, "message_id": 100},
        }}
        _run(bot.handle_update(update))
        edit = [c for c in captured_tg_calls if c[0] == "editMessageText"]
        assert len(edit) == 1
        kb = edit[0][1]["reply_markup"]["inline_keyboard"]
        callbacks = [row[0]["callback_data"] for row in kb]
        assert "settings_profile" in callbacks
        assert "settings_default_model" in callbacks
        assert "settings_unlink" in callbacks


# ── Profile ──────────────────────────────────────────────────────────────


class TestProfile:
    def test_profile_shows_user_info(self, captured_tg_calls):
        from server import aiche_telegram_bot as bot
        tg_uid = f"prof-{uuid.uuid4().hex[:6]}"
        _make_user(tg_uid, tg_username="ivan", tokens_balance=54321)
        update = {"callback_query": {
            "id": "cb-p1", "data": "settings_profile",
            "from": {"id": tg_uid, "username": "ivan"},
            "message": {"chat": {"id": 1}, "message_id": 200},
        }}
        _run(bot.handle_update(update))
        edit = [c for c in captured_tg_calls if c[0] == "editMessageText"]
        assert len(edit) == 1
        text = edit[0][1]["text"]
        assert "Профиль" in text
        assert "543.21" in text  # баланс
        assert "@ivan" in text


# ── Default model ───────────────────────────────────────────────────────


class TestDefaultModel:
    def test_submenu_shows_5_models_with_default_marked(self, captured_tg_calls):
        from server import aiche_telegram_bot as bot
        from server.models import User
        tg_uid = f"defm-{uuid.uuid4().hex[:6]}"
        uid = _make_user(tg_uid)
        # Ставим default = grok
        db = SessionLocal()
        try:
            db.query(User).filter_by(id=uid).update(
                {"tg_default_chat_model": "grok"})
            db.commit()
        finally:
            db.close()

        update = {"callback_query": {
            "id": "cb-d1", "data": "settings_default_model",
            "from": {"id": tg_uid},
            "message": {"chat": {"id": 1}, "message_id": 300},
        }}
        _run(bot.handle_update(update))
        edit = [c for c in captured_tg_calls if c[0] == "editMessageText"]
        assert len(edit) == 1
        kb = edit[0][1]["reply_markup"]["inline_keyboard"]
        # 5 моделей + back
        assert len(kb) == 6
        # У grok должна быть звезда
        for row in kb:
            btn = row[0]
            if "Grok" in btn["text"]:
                assert btn["text"].startswith("⭐"), \
                    f"Default не помечен: {btn['text']}"

    def test_set_model_persists(self, captured_tg_calls):
        from server import aiche_telegram_bot as bot
        from server.models import User
        tg_uid = f"setm-{uuid.uuid4().hex[:6]}"
        uid = _make_user(tg_uid)
        update = {"callback_query": {
            "id": "cb-s1", "data": "settings_set_model:claude-sonnet",
            "from": {"id": tg_uid},
            "message": {"chat": {"id": 1}, "message_id": 400},
        }}
        _run(bot.handle_update(update))

        db = SessionLocal()
        try:
            u = db.query(User).filter_by(id=uid).first()
            assert u.tg_default_chat_model == "claude-sonnet"
        finally:
            db.close()

    def test_set_unknown_model_rejected(self, captured_tg_calls):
        from server import aiche_telegram_bot as bot
        tg_uid = f"setm-bad-{uuid.uuid4().hex[:6]}"
        _make_user(tg_uid)
        update = {"callback_query": {
            "id": "cb-sb", "data": "settings_set_model:unknown-x",
            "from": {"id": tg_uid},
            "message": {"chat": {"id": 1}, "message_id": 500},
        }}
        _run(bot.handle_update(update))
        # answerCallbackQuery с alert
        cbs = [c for c in captured_tg_calls if c[0] == "answerCallbackQuery"]
        assert any("Неизвестная" in c[1]["text"] for c in cbs)

    def test_chat_menu_marks_default_with_star(self, captured_tg_calls):
        from server import aiche_telegram_bot as bot
        from server.models import User
        tg_uid = f"chat-def-{uuid.uuid4().hex[:6]}"
        uid = _make_user(tg_uid)
        # Default = perplexity
        db = SessionLocal()
        try:
            db.query(User).filter_by(id=uid).update(
                {"tg_default_chat_model": "perplexity"})
            db.commit()
        finally:
            db.close()

        update = {"callback_query": {
            "id": "cb-cd", "data": "menu_chat",
            "from": {"id": tg_uid},
            "message": {"chat": {"id": 1}, "message_id": 600},
        }}
        _run(bot.handle_update(update))
        edit = [c for c in captured_tg_calls if c[0] == "editMessageText"]
        kb = edit[0][1]["reply_markup"]["inline_keyboard"]
        for row in kb:
            btn = row[0]
            if "Perplexity" in btn["text"]:
                assert btn["text"].startswith("⭐"), \
                    f"Default не подсвечен в chat submenu: {btn['text']}"


# ── Unlink ──────────────────────────────────────────────────────────────


class TestUnlink:
    def test_unlink_shows_confirmation(self, captured_tg_calls):
        from server import aiche_telegram_bot as bot
        tg_uid = f"unl-{uuid.uuid4().hex[:6]}"
        _make_user(tg_uid)
        update = {"callback_query": {
            "id": "cb-u1", "data": "settings_unlink",
            "from": {"id": tg_uid},
            "message": {"chat": {"id": 1}, "message_id": 700},
        }}
        _run(bot.handle_update(update))
        edit = [c for c in captured_tg_calls if c[0] == "editMessageText"]
        assert len(edit) == 1
        kb = edit[0][1]["reply_markup"]["inline_keyboard"]
        callbacks = [row[0]["callback_data"] for row in kb]
        assert "settings_unlink_confirm" in callbacks
        assert "menu_settings" in callbacks  # отмена

    def test_unlink_confirm_clears_tg_user_id(self, captured_tg_calls):
        from server import aiche_telegram_bot as bot
        from server.models import User
        tg_uid = f"unl-go-{uuid.uuid4().hex[:6]}"
        uid = _make_user(tg_uid)
        # И state, чтобы проверить что он тоже сбросится
        bot._set_state(tg_uid, mode="chat", model="claude")

        update = {"callback_query": {
            "id": "cb-u2", "data": "settings_unlink_confirm",
            "from": {"id": tg_uid},
            "message": {"chat": {"id": 1}, "message_id": 800},
        }}
        _run(bot.handle_update(update))

        db = SessionLocal()
        try:
            u = db.query(User).filter_by(id=uid).first()
            assert u.tg_user_id is None
            assert u.tg_username is None
            # email/balance не трогаем
            assert u.tokens_balance == 10000
        finally:
            db.close()

        # State юзера тоже очищен
        assert bot._get_state(tg_uid) is None
