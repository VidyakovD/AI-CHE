"""Тесты MAX-management (симметрично TG).

Покрывают:
  - _format_for_max: markdown-формат (не HTML) для MAX
  - generate_link_code / consume_link_code: lifecycle привязки
  - is_configured: чтение env
"""
import os
import time
import pytest


class TestFormatForMax:

    def test_reply_only(self):
        from server.max_management import _format_for_max
        parts = _format_for_max({"reply": "Привет!"})
        assert parts == ["Привет!"]

    def test_with_module(self):
        from server.max_management import _format_for_max
        parts = _format_for_max({
            "reply": "Поручаю",
            "module_reply": "результат",
            "module_slug": "copywriter",
            "new_level": 1,
        })
        assert len(parts) == 2
        # MAX использует markdown (**bold**), а не HTML (<b>bold</b>)
        assert "**copywriter**" in parts[1]
        assert "<b>" not in parts[1]

    def test_level_up_badge(self):
        from server.max_management import _format_for_max
        parts = _format_for_max({
            "reply": "Окей",
            "module_reply": "результат",
            "module_slug": "smm",
            "new_level": 2,
            "level_up": True,
        })
        assert "⬆" in parts[1]

    def test_empty(self):
        from server.max_management import _format_for_max
        parts = _format_for_max({"reply": ""})
        assert len(parts) == 1
        assert "пустой" in parts[0].lower()

    def test_html_not_escaped(self):
        """Для MAX format=markdown — мы НЕ должны экранировать <> (как для TG).
        Если юзер пишет код через <code> — пусть приходит как есть."""
        from server.max_management import _format_for_max
        parts = _format_for_max({"reply": "Используй <div> в коде"})
        # Не превращаем < > в &lt; &gt;
        assert "<div>" in parts[0]
        assert "&lt;" not in parts[0]


class TestIsConfigured:

    def test_no_token_returns_false(self, monkeypatch):
        monkeypatch.delenv("MAX_MGMT_BOT_TOKEN", raising=False)
        from server.max_management import is_configured
        assert is_configured() is False

    def test_empty_token_returns_false(self, monkeypatch):
        monkeypatch.setenv("MAX_MGMT_BOT_TOKEN", "")
        from server.max_management import is_configured
        assert is_configured() is False

    def test_with_token_returns_true(self, monkeypatch):
        monkeypatch.setenv("MAX_MGMT_BOT_TOKEN", "abc123")
        from server.max_management import is_configured
        assert is_configured() is True


class TestLinkCodeLifecycle:

    def _make_user(self) -> int:
        from server.db import db_session
        from server.models import User
        with db_session() as db:
            u = User(
                email=f"max-link-{time.time_ns()}@x.x",
                password_hash="h", is_verified=True,
                agreed_to_terms=True, tokens_balance=0,
            )
            db.add(u); db.commit(); db.refresh(u)
            return u.id

    def _cleanup(self, user_id: int):
        from server.db import db_session
        from server.models import User
        with db_session() as db:
            db.query(User).filter_by(id=user_id).delete()
            db.commit()

    def test_generate_creates_code(self):
        from server.db import db_session
        from server.models import User
        from server.max_management import generate_link_code

        uid = self._make_user()
        try:
            with db_session() as db:
                code = generate_link_code(db, uid)
                assert len(code) == 6
                assert code.isupper()
                u = db.query(User).filter_by(id=uid).first()
                assert u.max_link_code == code
                assert u.max_link_expires is not None
        finally:
            self._cleanup(uid)

    def test_consume_links_account(self):
        """consume_link_code привязывает max_user_id к юзеру."""
        from server.db import db_session
        from server.models import User
        from server.max_management import generate_link_code, consume_link_code

        uid = self._make_user()
        try:
            with db_session() as db:
                code = generate_link_code(db, uid)

            with db_session() as db:
                result_uid = consume_link_code(db, code,
                                                max_user_id="111222",
                                                max_username="denis")
            assert result_uid == uid

            with db_session() as db:
                u = db.query(User).filter_by(id=uid).first()
                assert u.max_user_id == "111222"
                assert u.max_username == "denis"
                assert u.max_link_code is None  # сброшен
        finally:
            self._cleanup(uid)

    def test_consume_wrong_code_returns_none(self):
        from server.db import db_session
        from server.max_management import generate_link_code, consume_link_code

        uid = self._make_user()
        try:
            with db_session() as db:
                generate_link_code(db, uid)

            with db_session() as db:
                result = consume_link_code(db, "WRONG1",
                                            max_user_id="111222",
                                            max_username="x")
            assert result is None
        finally:
            self._cleanup(uid)

    def test_unlink_clears_fields(self):
        from server.db import db_session
        from server.models import User
        from server.max_management import generate_link_code, consume_link_code, unlink

        uid = self._make_user()
        try:
            with db_session() as db:
                code = generate_link_code(db, uid)
            with db_session() as db:
                consume_link_code(db, code, max_user_id="111", max_username="d")

            with db_session() as db:
                ok = unlink(db, uid)
                assert ok is True
                u = db.query(User).filter_by(id=uid).first()
                assert u.max_user_id is None
                assert u.max_username is None
        finally:
            self._cleanup(uid)
