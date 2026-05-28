"""Тесты для agent_actions — парсер блоков, регистрация executor, prompt-helper.

Действия исполняются ТОЛЬКО после подтверждения юзером — это безопасный flow
для опасных операций (отправка email, создание встречи, пауза кампании).
"""
from __future__ import annotations

import json
import os
import sys
import uuid

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


# ── Парсер action-блоков ─────────────────────────────────────────────────────


class TestParseActionBlocks:

    def test_single_block(self):
        from server.agent_actions import parse_action_blocks
        text = """Готовлю ответ.

[ACTION:send_email]
mailbox_id: 5
to: ivan@example.com
subject: Re: договор
body:
Здравствуйте, Иван!

Готов выслать.
[/ACTION]
"""
        blocks = parse_action_blocks(text)
        assert len(blocks) == 1
        assert blocks[0]["action_type"] == "send_email"
        params = blocks[0]["params"]
        assert params["mailbox_id"] == 5
        assert isinstance(params["mailbox_id"], int)
        assert params["to"] == "ivan@example.com"
        assert params["subject"] == "Re: договор"
        # Body должен сохранить переносы
        assert "Здравствуйте, Иван!" in params["body"]
        assert "Готов выслать." in params["body"]
        assert "\n" in params["body"]

    def test_no_blocks_returns_empty(self):
        from server.agent_actions import parse_action_blocks
        assert parse_action_blocks("") == []
        assert parse_action_blocks("обычный текст без блоков") == []

    def test_multiple_blocks(self):
        from server.agent_actions import parse_action_blocks
        text = """
[ACTION:send_email]
mailbox_id: 1
to: a@x.ru
subject: A
body:
тело А
[/ACTION]

И ещё одно:

[ACTION:create_google_event]
calendar_connection_id: 2
title: Встреча
start: 2026-06-05T14:00:00+03:00
[/ACTION]
"""
        blocks = parse_action_blocks(text)
        assert len(blocks) == 2
        assert blocks[0]["action_type"] == "send_email"
        assert blocks[1]["action_type"] == "create_google_event"
        assert blocks[1]["params"]["calendar_connection_id"] == 2

    def test_type_coercion_id_fields(self):
        from server.agent_actions import parse_action_blocks
        text = """[ACTION:send_email]
mailbox_id: 42
to: x@y.com
subject: X
body:
Z
[/ACTION]"""
        b = parse_action_blocks(text)[0]
        assert b["params"]["mailbox_id"] == 42
        assert isinstance(b["params"]["mailbox_id"], int)

    def test_type_coercion_float_budget(self):
        from server.agent_actions import parse_action_blocks
        text = """[ACTION:yandex_direct_set_daily_budget]
campaign_id: 12345
new_daily_budget_rub: 2500.50
[/ACTION]"""
        b = parse_action_blocks(text)[0]
        assert b["params"]["new_daily_budget_rub"] == 2500.50
        assert b["params"]["campaign_id"] == 12345

    def test_strip_blocks(self):
        from server.agent_actions import strip_action_blocks
        text = """Перед.

[ACTION:send_email]
mailbox_id: 1
to: x@y.com
subject: X
body:
тело
[/ACTION]

После."""
        clean = strip_action_blocks(text)
        assert "[ACTION:" not in clean
        assert "Перед." in clean
        assert "После." in clean
        assert "send_email" in clean  # маркер остался

    def test_malformed_block_skipped(self):
        """Незакрытый [ACTION:...] не должен ломать парсинг."""
        from server.agent_actions import parse_action_blocks
        text = "[ACTION:send_email]\nто что попало без закрывающего"
        assert parse_action_blocks(text) == []


# ── Регистрация и вызов executor'а ───────────────────────────────────────────


class TestExecutor:

    def test_unknown_action_returns_error(self):
        from server.agent_actions import execute_action
        r = execute_action("nonexistent_xxxx", {}, user_id=999)
        assert r["ok"] is False
        assert "nonexistent_xxxx" in (r.get("error") or "")

    def test_register_and_execute(self):
        from server.agent_actions import register_executor, execute_action
        called = {}

        @register_executor("test_action_demo")
        def _h(params, uid):
            called["params"] = params
            called["uid"] = uid
            return {"ok": True, "result": {"x": params.get("y", 0) * 2}, "error": None}

        r = execute_action("test_action_demo", {"y": 5}, user_id=42)
        assert r["ok"] is True
        assert r["result"] == {"x": 10}
        assert called["uid"] == 42

    def test_executor_exception_caught(self):
        from server.agent_actions import register_executor, execute_action

        @register_executor("test_action_throws")
        def _h(params, uid):
            raise RuntimeError("boom")

        r = execute_action("test_action_throws", {}, user_id=1)
        assert r["ok"] is False
        assert "boom" in (r.get("error") or "")


# ── Создание PendingAgentAction в БД ────────────────────────────────────────


class TestCreatePendingActions:

    def test_creates_row_in_db(self):
        from server.agent_actions import create_pending_actions
        from server.models import PendingAgentAction

        db = SessionLocal()
        try:
            u = _make_user(db, "pending-action-create@test.com")
            uid = u.id
        finally:
            db.close()

        output = """[ACTION:send_email]
mailbox_id: 7
to: test@example.com
subject: Hi
body:
Тестовое тело
[/ACTION]"""

        clean, pending = create_pending_actions(
            user_id=uid, agent_id=None, module_slug="mail", output=output,
        )
        assert len(pending) == 1
        assert pending[0]["action_type"] == "send_email"
        assert pending[0]["id"] > 0

        db = SessionLocal()
        try:
            row = db.query(PendingAgentAction).get(pending[0]["id"])
            assert row is not None
            assert row.user_id == uid
            assert row.status == "pending"
            assert row.module_slug == "mail"
            params = json.loads(row.params_json)
            assert params["to"] == "test@example.com"
            assert "Тестовое тело" in params["body"]
            # preview должен содержать что-то осмысленное
            assert "test@example.com" in (row.preview_text or "")
        finally:
            db.close()

    def test_no_blocks_no_rows(self):
        from server.agent_actions import create_pending_actions
        clean, pending = create_pending_actions(
            user_id=1, agent_id=None, module_slug="mail",
            output="обычный ответ без действий",
        )
        assert pending == []
        assert clean == "обычный ответ без действий"


# ── Action protocol prompt ───────────────────────────────────────────────────


class TestActionProtocolPrompt:

    def test_empty_for_no_actions(self):
        from server.agent_actions import get_action_protocol_prompt
        assert get_action_protocol_prompt([]) == ""

    def test_includes_send_email_example(self):
        from server.agent_actions import get_action_protocol_prompt
        p = get_action_protocol_prompt(["send_email"])
        assert "[ACTION:send_email]" in p
        assert "mailbox_id" in p
        assert "body:" in p
        assert "подтвердит" in p.lower() or "подтверждения" in p.lower()

    def test_unknown_action_falls_back_to_bullet(self):
        from server.agent_actions import get_action_protocol_prompt
        p = get_action_protocol_prompt(["custom_xxxxx_unknown"])
        assert "custom_xxxxx_unknown" in p
