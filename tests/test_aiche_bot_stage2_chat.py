"""Stage 2 тесты: чат с 5 моделями в @aiche_bot.

Flow:
  1. Юзер жмёт «🤖 Чат с AI» → submenu с 5 кнопками
  2. Юзер выбирает модель → state mode=chat, model=X
  3. Юзер пишет text-сообщение → generate_response → списание → ответ

Мокаем generate_response (не зовём реальный LLM), мокаем _tg_call.
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


# ── menu_chat → submenu ──────────────────────────────────────────────────


class TestChatSubmenu:
    def test_menu_chat_shows_5_models(self, captured_tg_calls):
        from server import aiche_telegram_bot as bot
        suffix = uuid.uuid4().hex[:6]
        tg_uid = f"chat-menu-{suffix}"
        db = SessionLocal()
        try:
            _make_user(db, f"chat-menu-{suffix}@test.com", tg_user_id=tg_uid)
        finally:
            db.close()

        update = {"callback_query": {
            "id": "cb-1", "data": "menu_chat",
            "from": {"id": tg_uid},
            "message": {"chat": {"id": 1}, "message_id": 100},
        }}
        _run(bot.handle_update(update))

        edit = [c for c in captured_tg_calls if c[0] == "editMessageText"]
        assert len(edit) == 1
        kb = edit[0][1]["reply_markup"]["inline_keyboard"]
        # 5 моделей + кнопка Назад
        assert len(kb) == 6
        callbacks = [row[0]["callback_data"] for row in kb]
        assert "chat:claude" in callbacks
        assert "chat:claude-sonnet" in callbacks
        assert "chat:openai" in callbacks
        assert "chat:grok" in callbacks
        assert "chat:perplexity" in callbacks


# ── chat:<model> → set state ─────────────────────────────────────────────


class TestModelSelection:
    def test_selecting_model_sets_state(self, captured_tg_calls):
        from server import aiche_telegram_bot as bot
        suffix = uuid.uuid4().hex[:6]
        tg_uid = f"chat-sel-{suffix}"
        db = SessionLocal()
        try:
            _make_user(db, f"chat-sel-{suffix}@test.com", tg_user_id=tg_uid)
        finally:
            db.close()

        update = {"callback_query": {
            "id": "cb-2", "data": "chat:claude-sonnet",
            "from": {"id": tg_uid},
            "message": {"chat": {"id": 1}, "message_id": 200},
        }}
        _run(bot.handle_update(update))

        # State установлен
        state = bot._get_state(tg_uid)
        assert state is not None
        assert state["mode"] == "chat"
        assert state["model"] == "claude-sonnet"

        # Юзеру показано приглашение
        edit = [c for c in captured_tg_calls if c[0] == "editMessageText"]
        assert any("Sonnet" in c[1]["text"] for c in edit)

    def test_unknown_model_returns_to_submenu(self, captured_tg_calls):
        from server import aiche_telegram_bot as bot
        tg_uid = f"chat-bad-{uuid.uuid4().hex[:6]}"
        db = SessionLocal()
        try:
            _make_user(db, f"chat-bad-{tg_uid}@test.com", tg_user_id=tg_uid)
        finally:
            db.close()

        update = {"callback_query": {
            "id": "cb-3", "data": "chat:fake-model",
            "from": {"id": tg_uid},
            "message": {"chat": {"id": 1}, "message_id": 300},
        }}
        _run(bot.handle_update(update))
        edit = [c for c in captured_tg_calls if c[0] == "editMessageText"]
        assert any("Неизвестная" in c[1]["text"] for c in edit)


# ── Сообщение в режиме chat → LLM → списание ─────────────────────────────


class TestChatMessageFlow:
    def test_chat_message_calls_llm_and_debits(self, captured_tg_calls,
                                                  monkeypatch):
        from server import aiche_telegram_bot as bot
        from server import ai as ai_module
        from server.models import User, Transaction

        suffix = uuid.uuid4().hex[:6]
        tg_uid = f"chat-msg-{suffix}"
        db = SessionLocal()
        try:
            u = _make_user(db, f"chat-msg-{suffix}@test.com",
                            tg_user_id=tg_uid, tokens_balance=100000)
            uid = u.id
        finally:
            db.close()

        # Мокаем generate_response
        def _fake_gen(model, messages, extra=None, **kw):
            return {
                "type": "text",
                "content": "Привет, я отвечаю!",
                "usage": {"input_tokens": 10, "output_tokens": 30,
                          "actual_cost_kop": 5},
            }
        monkeypatch.setattr(ai_module, "generate_response", _fake_gen)

        # Set state
        bot._set_state(tg_uid, mode="chat", model="claude-sonnet")

        # Юзер пишет сообщение
        update = {"message": {
            "chat": {"id": 1},
            "text": "Привет, расскажи о Python",
            "from": {"id": tg_uid, "username": "u"},
        }}
        _run(bot.handle_update(update))

        # Был sendChatAction(typing)
        actions = [c for c in captured_tg_calls if c[0] == "sendChatAction"]
        assert len(actions) >= 1
        assert actions[0][1]["action"] == "typing"

        # И sendMessage с ответом + footer (списание + баланс)
        sends = [c for c in captured_tg_calls if c[0] == "sendMessage"]
        assert len(sends) == 1
        text = sends[0][1]["text"]
        assert "Привет, я отвечаю!" in text
        assert "₽" in text  # footer с балансом

        # Списание произошло
        db = SessionLocal()
        try:
            cur = db.query(User).filter_by(id=uid).first()
            assert cur.tokens_balance < 100000, "Баланс должен снизиться"
            # Транзакция записана
            tx = (db.query(Transaction)
                    .filter_by(user_id=uid, type="usage")
                    .order_by(Transaction.id.desc()).first())
            assert tx is not None
            assert tx.tokens_delta < 0
        finally:
            db.close()

    def test_chat_message_low_balance_rejects(self, captured_tg_calls,
                                                 monkeypatch):
        from server import aiche_telegram_bot as bot
        suffix = uuid.uuid4().hex[:6]
        tg_uid = f"chat-poor-{suffix}"
        db = SessionLocal()
        try:
            _make_user(db, f"chat-poor-{suffix}@test.com",
                       tg_user_id=tg_uid, tokens_balance=50)  # < 100 коп
        finally:
            db.close()
        bot._set_state(tg_uid, mode="chat", model="claude")

        update = {"message": {
            "chat": {"id": 1}, "text": "тест",
            "from": {"id": tg_uid},
        }}
        _run(bot.handle_update(update))
        sends = [c for c in captured_tg_calls if c[0] == "sendMessage"]
        assert any("Недостаточно" in c[1]["text"] for c in sends)

    def test_chat_message_llm_error_no_charge(self, captured_tg_calls,
                                                 monkeypatch):
        """LLM упал → не списываем (юзер ничего не получил)."""
        from server import aiche_telegram_bot as bot
        from server import ai as ai_module
        from server.models import User

        suffix = uuid.uuid4().hex[:6]
        tg_uid = f"chat-err-{suffix}"
        db = SessionLocal()
        try:
            u = _make_user(db, f"chat-err-{suffix}@test.com",
                            tg_user_id=tg_uid, tokens_balance=50000)
            uid = u.id
            initial = u.tokens_balance
        finally:
            db.close()

        def _fake_gen_err(model, messages, extra=None, **kw):
            raise RuntimeError("anthropic 503")
        monkeypatch.setattr(ai_module, "generate_response", _fake_gen_err)

        bot._set_state(tg_uid, mode="chat", model="claude")
        update = {"message": {
            "chat": {"id": 1}, "text": "test",
            "from": {"id": tg_uid},
        }}
        _run(bot.handle_update(update))

        sends = [c for c in captured_tg_calls if c[0] == "sendMessage"]
        assert any("временно недоступен" in c[1]["text"].lower() for c in sends)

        # Баланс не изменился
        db = SessionLocal()
        try:
            cur = db.query(User).filter_by(id=uid).first()
            assert cur.tokens_balance == initial
        finally:
            db.close()

    def test_message_without_state_shows_menu(self, captured_tg_calls):
        """Если юзер пишет text без активного режима — показываем меню."""
        from server import aiche_telegram_bot as bot
        suffix = uuid.uuid4().hex[:6]
        tg_uid = f"chat-nostate-{suffix}"
        db = SessionLocal()
        try:
            _make_user(db, f"chat-nostate-{suffix}@test.com", tg_user_id=tg_uid)
        finally:
            db.close()
        # state НЕ установлен
        update = {"message": {
            "chat": {"id": 1}, "text": "просто текст",
            "from": {"id": tg_uid},
        }}
        _run(bot.handle_update(update))
        sends = [c for c in captured_tg_calls if c[0] == "sendMessage"]
        assert any("/start" in c[1]["text"] for c in sends)


# ── State management ────────────────────────────────────────────────────


class TestState:
    def test_state_set_and_get(self):
        from server.aiche_telegram_bot import _set_state, _get_state, _clear_state
        tg_uid = f"state-{uuid.uuid4().hex[:6]}"
        _set_state(tg_uid, mode="chat", model="claude")
        s = _get_state(tg_uid)
        assert s is not None
        assert s["mode"] == "chat"
        assert s["model"] == "claude"
        _clear_state(tg_uid)
        assert _get_state(tg_uid) is None

    def test_menu_chat_clears_old_state(self, captured_tg_calls):
        """Возврат в submenu чата сбрасывает state — чтобы /menu из чата не
        ловил следующий текст как chat-prompt."""
        from server import aiche_telegram_bot as bot
        tg_uid = f"state-clear-{uuid.uuid4().hex[:6]}"
        db = SessionLocal()
        try:
            _make_user(db, f"state-clear-{tg_uid}@test.com", tg_user_id=tg_uid)
        finally:
            db.close()
        bot._set_state(tg_uid, mode="chat", model="claude")
        update = {"callback_query": {
            "id": "cb-clear", "data": "menu_chat",
            "from": {"id": tg_uid},
            "message": {"chat": {"id": 1}, "message_id": 555},
        }}
        _run(bot.handle_update(update))
        assert bot._get_state(tg_uid) is None
