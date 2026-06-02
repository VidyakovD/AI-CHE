"""Stage 3 тесты для @aiche_bot: GPT-image + Kling video.

- Image: callback menu_image → state mode=image; text-prompt → sendPhoto +
  списание 60 ₽.
- Video: callback menu_video → submenu; video:kling → state mode=video;
  text-prompt → submit Kling → asyncio.create_task для poller'а.
  Сам poller (poll_kling_task) тестируется отдельно через прямой вызов.

Мокаются: generate_response (LLM/Image/Video), _tg_call (TG API).
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
    monkeypatch.setenv("APP_URL", "https://aiche.ru")


@pytest.fixture
def captured_tg_calls(monkeypatch):
    from server import aiche_telegram_bot as bot
    calls: list[tuple[str, dict]] = []

    async def _fake_call(method: str, payload: dict):
        calls.append((method, payload))
        return {"ok": True, "result": {"message_id": 42}}

    monkeypatch.setattr(bot, "_tg_call", _fake_call)
    return calls


def _make_user_with_tg(tg_uid: str, balance_kop: int = 0) -> int:
    from server.models import User
    db = SessionLocal()
    try:
        u = User(
            email=f"media-{tg_uid}-{uuid.uuid4().hex[:6]}@test.com",
            password_hash=_FAKE_BCRYPT, name="Media User",
            tg_user_id=tg_uid, tokens_balance=balance_kop,
            is_verified=True, agreed_to_terms=True,
            referral_code=uuid.uuid4().hex[:8].upper(),
        )
        db.add(u); db.commit(); db.refresh(u)
        return u.id
    finally:
        db.close()


# ── Image ─────────────────────────────────────────────────────────────────


class TestImageMenu:
    def test_menu_image_sets_state(self, captured_tg_calls):
        from server import aiche_telegram_bot as bot
        tg_uid = f"img-menu-{uuid.uuid4().hex[:6]}"
        _make_user_with_tg(tg_uid)
        update = {"callback_query": {
            "id": "cb-im1", "data": "menu_image",
            "from": {"id": tg_uid},
            "message": {"chat": {"id": 1}, "message_id": 100},
        }}
        _run(bot.handle_update(update))
        state = bot._get_state(tg_uid)
        assert state and state["mode"] == "image"
        assert state["model"] == "gpt-image"


class TestImageGeneration:
    def test_image_happy_path(self, captured_tg_calls, monkeypatch):
        from server import aiche_telegram_bot as bot
        from server import ai as ai_module
        from server.models import User, Transaction

        tg_uid = f"img-ok-{uuid.uuid4().hex[:6]}"
        uid = _make_user_with_tg(tg_uid, balance_kop=20000)  # 200 ₽

        def _fake_img(model, messages, extra=None, **kw):
            return {"type": "image", "url": "/uploads/test.png",
                    "content": "/uploads/test.png",
                    "usage": {"input_tokens": 0, "output_tokens": 0,
                              "actual_cost_kop": 1700}}
        monkeypatch.setattr(ai_module, "generate_response", _fake_img)

        bot._set_state(tg_uid, mode="image", model="gpt-image")
        update = {"message": {
            "chat": {"id": 5}, "text": "лиса на закате",
            "from": {"id": tg_uid},
        }}
        _run(bot.handle_update(update))

        # sendPhoto был вызван с публичным URL
        photo_calls = [c for c in captured_tg_calls if c[0] == "sendPhoto"]
        assert len(photo_calls) == 1
        assert photo_calls[0][1]["photo"] == "https://aiche.ru/uploads/test.png"
        assert "− 60.00 ₽" in photo_calls[0][1]["caption"]

        # Списание 60 ₽
        db = SessionLocal()
        try:
            u = db.query(User).filter_by(id=uid).first()
            assert u.tokens_balance == 14000  # 200 − 60 ₽
            tx = (db.query(Transaction).filter_by(user_id=uid, type="usage")
                    .order_by(Transaction.id.desc()).first())
            assert tx is not None
            assert tx.tokens_delta == -6000
            assert "image:gpt-image" in tx.description
        finally:
            db.close()

    def test_image_low_balance_rejects(self, captured_tg_calls, monkeypatch):
        from server import aiche_telegram_bot as bot
        from server import ai as ai_module
        tg_uid = f"img-poor-{uuid.uuid4().hex[:6]}"
        uid = _make_user_with_tg(tg_uid, balance_kop=3000)  # 30 ₽ < 60
        calls = []
        monkeypatch.setattr(ai_module, "generate_response",
                            lambda *a, **kw: calls.append(1) or {})

        bot._set_state(tg_uid, mode="image", model="gpt-image")
        update = {"message": {
            "chat": {"id": 5}, "text": "тест", "from": {"id": tg_uid},
        }}
        _run(bot.handle_update(update))

        # LLM НЕ вызывался
        assert calls == []
        sends = [c for c in captured_tg_calls if c[0] == "sendMessage"]
        assert any("Недостаточно" in c[1]["text"] for c in sends)

    def test_image_error_no_charge(self, captured_tg_calls, monkeypatch):
        from server import aiche_telegram_bot as bot
        from server import ai as ai_module
        from server.models import User

        tg_uid = f"img-err-{uuid.uuid4().hex[:6]}"
        uid = _make_user_with_tg(tg_uid, balance_kop=20000)
        initial = 20000

        def _fake_err(*a, **kw):
            raise RuntimeError("OpenAI 500")
        monkeypatch.setattr(ai_module, "generate_response", _fake_err)

        bot._set_state(tg_uid, mode="image", model="gpt-image")
        update = {"message": {
            "chat": {"id": 5}, "text": "test", "from": {"id": tg_uid},
        }}
        _run(bot.handle_update(update))

        # Не списали
        db = SessionLocal()
        try:
            u = db.query(User).filter_by(id=uid).first()
            assert u.tokens_balance == initial
        finally:
            db.close()


# ── Video ─────────────────────────────────────────────────────────────────


class TestVideoMenu:
    def test_menu_video_shows_submenu(self, captured_tg_calls):
        from server import aiche_telegram_bot as bot
        tg_uid = f"vid-menu-{uuid.uuid4().hex[:6]}"
        _make_user_with_tg(tg_uid)
        update = {"callback_query": {
            "id": "cb-v1", "data": "menu_video",
            "from": {"id": tg_uid},
            "message": {"chat": {"id": 1}, "message_id": 200},
        }}
        _run(bot.handle_update(update))
        edit = [c for c in captured_tg_calls if c[0] == "editMessageText"]
        assert len(edit) == 1
        kb = edit[0][1]["reply_markup"]["inline_keyboard"]
        callbacks = [row[0]["callback_data"] for row in kb]
        assert "video:kling" in callbacks
        # Kling-A: добавлены v1.6/v2/v2.1/v3
        assert "video:kling-1-6" in callbacks
        assert "video:kling-2" in callbacks
        assert "video:kling-3" in callbacks

    def test_video_selection_sets_state(self, captured_tg_calls):
        from server import aiche_telegram_bot as bot
        tg_uid = f"vid-sel-{uuid.uuid4().hex[:6]}"
        _make_user_with_tg(tg_uid)
        update = {"callback_query": {
            "id": "cb-v2", "data": "video:kling-2",
            "from": {"id": tg_uid},
            "message": {"chat": {"id": 1}, "message_id": 300},
        }}
        _run(bot.handle_update(update))
        st = bot._get_state(tg_uid)
        assert st and st["mode"] == "video"
        assert st["model"] == "kling-2"


class TestVideoSubmit:
    def test_video_submit_registers_task(self, captured_tg_calls, monkeypatch):
        from server import aiche_telegram_bot as bot
        from server import ai as ai_module

        tg_uid = f"vid-sub-{uuid.uuid4().hex[:6]}"
        uid = _make_user_with_tg(tg_uid, balance_kop=10000)

        def _fake_kling_submit(model, messages, extra=None, **kw):
            return {"type": "video_task", "task_id": "kling-task-abc123"}
        monkeypatch.setattr(ai_module, "generate_response", _fake_kling_submit)

        # Перехватываем asyncio.create_task — не хотим реально запускать poller
        captured_tasks = []
        original_create = asyncio.create_task

        def _fake_create(coro):
            captured_tasks.append(coro)
            coro.close()  # не запускать
            class _FakeT:
                def cancel(self): pass
            return _FakeT()
        monkeypatch.setattr(asyncio, "create_task", _fake_create)

        bot._set_state(tg_uid, mode="video", model="kling")
        update = {"message": {
            "chat": {"id": 7}, "text": "кот гуляет под луной",
            "from": {"id": tg_uid},
        }}
        _run(bot.handle_update(update))

        # «Принял, генерирую» отправлено
        sends = [c for c in captured_tg_calls if c[0] == "sendMessage"]
        assert any("генерируется" in c[1]["text"].lower() for c in sends)

        # Task зарегистрирован
        assert "kling-task-abc123" in bot._KLING_TASKS
        info = bot._KLING_TASKS["kling-task-abc123"]
        assert info["user_id"] == uid
        assert info["cost_kop"] == 6000  # kling v1 = 60 ₽ после recalc

        # poll-coroutine был создан
        assert len(captured_tasks) == 1

        # Очищаем глобальный стейт
        bot._KLING_TASKS.pop("kling-task-abc123", None)

    def test_video_low_balance_rejects(self, captured_tg_calls, monkeypatch):
        from server import aiche_telegram_bot as bot
        from server import ai as ai_module
        tg_uid = f"vid-poor-{uuid.uuid4().hex[:6]}"
        _make_user_with_tg(tg_uid, balance_kop=3000)  # 30 ₽ < 50
        calls = []
        monkeypatch.setattr(ai_module, "generate_response",
                            lambda *a, **kw: calls.append(1) or {})
        bot._set_state(tg_uid, mode="video", model="kling")
        update = {"message": {
            "chat": {"id": 1}, "text": "test", "from": {"id": tg_uid},
        }}
        _run(bot.handle_update(update))
        assert calls == []
        sends = [c for c in captured_tg_calls if c[0] == "sendMessage"]
        assert any("Недостаточно" in c[1]["text"] for c in sends)

    def test_video_kling_error_no_charge(self, captured_tg_calls, monkeypatch):
        """Kling вернул не-video_task type → ошибка, без списания."""
        from server import aiche_telegram_bot as bot
        from server import ai as ai_module
        from server.models import User
        tg_uid = f"vid-err-{uuid.uuid4().hex[:6]}"
        uid = _make_user_with_tg(tg_uid, balance_kop=10000)

        def _fake_kling_err(model, messages, extra=None, **kw):
            return {"type": "text", "content": "Сервис временно недоступен..."}
        monkeypatch.setattr(ai_module, "generate_response", _fake_kling_err)

        bot._set_state(tg_uid, mode="video", model="kling")
        update = {"message": {
            "chat": {"id": 1}, "text": "test", "from": {"id": tg_uid},
        }}
        _run(bot.handle_update(update))

        db = SessionLocal()
        try:
            u = db.query(User).filter_by(id=uid).first()
            assert u.tokens_balance == 10000  # без списания
        finally:
            db.close()
