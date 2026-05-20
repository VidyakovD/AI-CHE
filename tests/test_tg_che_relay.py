"""Тесты TG-relay: входящие сообщения от TG-юзера → процесс через те же
helpers что HTTP endpoint /api/agents/me/messages, ответ обратно в TG.

Покрывают:
  - process_message: создаёт Agent если нет, сохраняет user+assistant сообщения
  - process_message: вызывает build_reply_personal с правильными параметрами
  - process_message: при invoke_request → создаёт tool-сообщение
  - process_message: insufficient_funds возвращает понятный reply
  - format_for_tg: 1 сообщение когда нет module
  - format_for_tg: 2 сообщения когда module ответил
  - format_for_tg: HTML-escape тегов в reply
"""
import json
import time
from unittest.mock import patch

import pytest


# ── format_for_tg ────────────────────────────────────────────────────────────


class TestFormatForTg:

    def test_reply_only(self):
        from server.tg_che_relay import format_for_tg
        parts = format_for_tg({"reply": "Привет!"})
        assert parts == ["Привет!"]

    def test_with_module(self):
        from server.tg_che_relay import format_for_tg
        parts = format_for_tg({
            "reply": "Поручаю Копирайтеру",
            "module_reply": "Вот текст поста",
            "module_slug": "copywriter",
            "new_level": 1,
        })
        assert len(parts) == 2
        assert "copywriter" in parts[1]
        assert "Вот текст поста" in parts[1]
        assert "L1" in parts[1]

    def test_level_up_badge(self):
        from server.tg_che_relay import format_for_tg
        parts = format_for_tg({
            "reply": "Окей",
            "module_reply": "результат",
            "module_slug": "smm",
            "new_level": 2,
            "level_up": True,
        })
        assert "⬆" in parts[1]
        assert "прокачался" in parts[1]

    def test_html_escape(self):
        """Reply содержит <script> — не должен попасть как HTML тег в TG."""
        from server.tg_che_relay import format_for_tg
        parts = format_for_tg({
            "reply": "<script>alert(1)</script> & <b>hi</b>",
        })
        assert "<script>" not in parts[0]
        assert "&lt;script&gt;" in parts[0]
        assert "&amp;" in parts[0]

    def test_empty_returns_placeholder(self):
        from server.tg_che_relay import format_for_tg
        parts = format_for_tg({"reply": ""})
        assert len(parts) == 1
        assert "пустой" in parts[0].lower()


# ── process_message (integration) ────────────────────────────────────────────


def _make_user(balance_kop: int = 100_000) -> int:
    from server.db import db_session
    from server.models import User
    with db_session() as db:
        u = User(
            email=f"tg-relay-{time.time_ns()}@x.x",
            password_hash="h", is_verified=True,
            agreed_to_terms=True, tokens_balance=balance_kop,
        )
        db.add(u); db.commit(); db.refresh(u)
        return u.id


def _cleanup_user(user_id: int):
    from server.db import db_session
    from server.models import User, Agent, AgentModule, AgentMessage, Transaction
    with db_session() as db:
        a = db.query(Agent).filter_by(user_id=user_id).first()
        if a:
            db.query(AgentMessage).filter_by(agent_id=a.id).delete()
            db.query(AgentModule).filter_by(agent_id=a.id).delete()
            db.delete(a)
        db.query(Transaction).filter_by(user_id=user_id).delete()
        db.query(User).filter_by(id=user_id).delete()
        db.commit()


class TestProcessMessage:

    def test_creates_agent_if_not_exists(self, monkeypatch):
        """Юзер первый раз пишет в TG — создаём ему агента в онбординге."""
        from server.db import db_session
        from server.models import User, Agent
        from server.tg_che_relay import process_message

        # Mock LLM ответ
        def fake_build(*, agent_name, mode, profile, personality, modules,
                       history, user_input, user_id):
            return {"reply": f"Привет, я {agent_name}!", "applied": [],
                    "profile_changed": False}
        monkeypatch.setattr("server.agent_builder.build_reply_personal", fake_build)

        uid = _make_user()
        try:
            with db_session() as db:
                u = db.query(User).filter_by(id=uid).first()
                # Агента ещё нет
                assert db.query(Agent).filter_by(user_id=uid).first() is None

                result = process_message(db, u, "привет")

            assert result["reply"].startswith("Привет")
            assert result["error"] is None

            # Агент создан в БД
            with db_session() as db:
                a = db.query(Agent).filter_by(user_id=uid).first()
                assert a is not None
                assert a.status == "onboarding"
        finally:
            _cleanup_user(uid)

    def test_empty_message_returns_error(self):
        from server.db import db_session
        from server.models import User
        from server.tg_che_relay import process_message

        uid = _make_user()
        try:
            with db_session() as db:
                u = db.query(User).filter_by(id=uid).first()
                result = process_message(db, u, "")
            assert result["error"] == "Пустое сообщение"
        finally:
            _cleanup_user(uid)

    def test_insufficient_funds(self, monkeypatch):
        """Юзер с нулевым балансом и не в онбординге → ошибка."""
        from server.db import db_session
        from server.models import User, Agent, AgentMessage
        from server.tg_che_relay import process_message

        uid = _make_user(balance_kop=10)  # 0.10 ₽ — недостаточно на сообщение
        try:
            # Сделаем юзера в active-режиме (иначе первое сообщение бесплатное)
            with db_session() as db:
                a = Agent(user_id=uid, name="Че", status="active",
                          profile_json="{}", personality_json="{}")
                db.add(a); db.commit()
                # Заполним 5+ сообщений чтобы он не попал в free_onboarding
                for i in range(6):
                    db.add(AgentMessage(agent_id=a.id, role="user",
                                         content=f"msg{i}"))
                db.commit()

            with db_session() as db:
                u = db.query(User).filter_by(id=uid).first()
                result = process_message(db, u, "ещё сообщение")

            assert result["error"] == "insufficient_funds"
            assert "Недостаточно" in result["reply"]
        finally:
            _cleanup_user(uid)

    def test_saves_user_and_assistant_messages(self, monkeypatch):
        """После успешного process_message в БД два сообщения: user+assistant."""
        from server.db import db_session
        from server.models import User, Agent, AgentMessage
        from server.tg_che_relay import process_message

        def fake_build(**kwargs):
            return {"reply": "ответ Че", "applied": [], "profile_changed": False}
        monkeypatch.setattr("server.agent_builder.build_reply_personal", fake_build)

        uid = _make_user()
        try:
            with db_session() as db:
                u = db.query(User).filter_by(id=uid).first()
                process_message(db, u, "тестовое сообщение")

            with db_session() as db:
                a = db.query(Agent).filter_by(user_id=uid).first()
                msgs = (db.query(AgentMessage)
                          .filter_by(agent_id=a.id)
                          .order_by(AgentMessage.id.asc()).all())
                assert len(msgs) >= 2
                assert msgs[0].role == "user"
                assert "тестовое" in msgs[0].content
                assert msgs[1].role == "assistant"
                assert msgs[1].content == "ответ Че"
                # source=tg в meta
                meta = json.loads(msgs[0].meta_json or "{}")
                assert meta.get("source") == "tg"
        finally:
            _cleanup_user(uid)

    def test_invokes_module_when_requested(self, monkeypatch):
        """build_reply вернул invoke_request → process_message вызывает invoke_module."""
        from server.db import db_session
        from server.models import User, Agent, AgentModule, AgentMessage
        from server.tg_che_relay import process_message

        # Builder говорит «делегирую copywriter»
        def fake_build(**kwargs):
            return {
                "reply": "Поручаю Копирайтеру",
                "applied": [],
                "profile_changed": False,
                "invoke_request": {"slug": "copywriter",
                                   "task": "напиши пост про стройку"},
            }
        monkeypatch.setattr("server.agent_builder.build_reply_personal", fake_build)

        # invoke_module возвращает результат
        def fake_invoke(**kwargs):
            return {"ok": True, "output": "Пост про стройку готов: ...",
                    "model_used": "claude-haiku", "memory_updates": {}}
        monkeypatch.setattr("server.agent_builder.invoke_module", fake_invoke)

        uid = _make_user()
        try:
            # Подключим copywriter
            with db_session() as db:
                a = Agent(user_id=uid, name="Че", status="active",
                          profile_json="{}", personality_json="{}")
                db.add(a); db.commit()
                m = AgentModule(agent_id=a.id, slug="copywriter", level=0,
                                is_enabled=True, interaction_count=0,
                                module_memory_json="{}")
                db.add(m); db.commit()

            with db_session() as db:
                u = db.query(User).filter_by(id=uid).first()
                result = process_message(db, u, "напиши пост")

            assert result["reply"] == "Поручаю Копирайтеру"
            assert result["module_slug"] == "copywriter"
            assert "Пост про стройку" in result["module_reply"]

            # В БД три сообщения: user + assistant + tool
            with db_session() as db:
                a = db.query(Agent).filter_by(user_id=uid).first()
                msgs = (db.query(AgentMessage)
                          .filter_by(agent_id=a.id)
                          .order_by(AgentMessage.id.asc()).all())
                roles = [m.role for m in msgs]
                assert "user" in roles
                assert "assistant" in roles
                assert "tool" in roles
        finally:
            _cleanup_user(uid)
