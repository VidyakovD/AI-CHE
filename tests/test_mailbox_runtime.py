"""Тесты mailbox runtime для модуля 📧 Почта (Loom).

Покрывают:
  - detect_provider: эвристика по домену email
  - _decode_mime / _extract_body: парсинг MIME-сообщений
  - verify_mailbox_connection: успех + login fail + connect fail
  - fetch_mailbox_recent: возврат свежих писем
  - build_mail_context: формирование текстового блока
  - _build_module_extra_context: подмешивание контекста в invoke_module

IMAP-вызовы заменяются monkeypatch'ем imaplib.IMAP4_SSL.
"""
import asyncio
import json
import time
from email.message import EmailMessage
from unittest.mock import MagicMock

import pytest


# ── detect_provider ─────────────────────────────────────────────────────────


class TestDetectProvider:
    def test_yandex(self):
        from server.mailbox_runtime import detect_provider
        assert detect_provider("me@yandex.ru") == "yandex"
        assert detect_provider("user@ya.ru") == "yandex"
        assert detect_provider("X@YANDEX.com") == "yandex"

    def test_gmail(self):
        from server.mailbox_runtime import detect_provider
        assert detect_provider("foo@gmail.com") == "gmail"
        assert detect_provider("bar@googlemail.com") == "gmail"

    def test_mailru_family(self):
        from server.mailbox_runtime import detect_provider
        for d in ["mail.ru", "inbox.ru", "list.ru", "bk.ru"]:
            assert detect_provider(f"u@{d}") == "mailru"

    def test_other(self):
        from server.mailbox_runtime import detect_provider
        assert detect_provider("ceo@mycompany.com") == "other"
        assert detect_provider("") == "other"


# ── _decode_mime / _extract_body ───────────────────────────────────────────


class TestMimeDecoding:
    def test_decode_plain_ascii(self):
        from server.mailbox_runtime import _decode_mime
        assert _decode_mime("Hello") == "Hello"
        assert _decode_mime("") == ""

    def test_decode_encoded_utf8(self):
        from server.mailbox_runtime import _decode_mime
        # Encoded-word: =?UTF-8?B?...?= с base64 «Привет»
        encoded = "=?UTF-8?B?0J/RgNC40LLQtdGC?="
        assert "Привет" in _decode_mime(encoded)

    def test_extract_body_plain(self):
        from server.mailbox_runtime import _extract_body
        msg = EmailMessage()
        msg.set_content("Тело письма")
        assert "Тело письма" in _extract_body(msg)


# ── verify_mailbox_connection ──────────────────────────────────────────────


def _make_fake_imap(login_ok=True, select_ok=True, msg_count=42):
    """Возвращает MagicMock который ведёт себя как IMAP4_SSL."""
    mock = MagicMock()
    if not login_ok:
        import imaplib
        mock.login.side_effect = imaplib.IMAP4.error("Authentication failed")
    if select_ok:
        mock.select.return_value = ("OK", [str(msg_count).encode()])
    else:
        mock.select.return_value = ("NO", [b"select failed"])
    return mock


class TestVerify:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_success(self, monkeypatch):
        from server import mailbox_runtime as mr
        fake = _make_fake_imap(login_ok=True, select_ok=True, msg_count=10)
        monkeypatch.setattr(mr.imaplib, "IMAP4_SSL", lambda h, p: fake)

        result = self._run(mr.verify_mailbox_connection(
            "imap.yandex.ru", 993, "me@yandex.ru", "appp-asss-word"
        ))
        assert result["ok"] is True
        assert result["messages_total"] == 10

    def test_login_fail_friendly_error(self, monkeypatch):
        from server import mailbox_runtime as mr
        fake = _make_fake_imap(login_ok=False)
        monkeypatch.setattr(mr.imaplib, "IMAP4_SSL", lambda h, p: fake)

        result = self._run(mr.verify_mailbox_connection(
            "imap.yandex.ru", 993, "me@yandex.ru", "wrong"
        ))
        assert result["ok"] is False
        assert "app-password" in result["error"].lower() or "пароль" in result["error"].lower()

    def test_connect_fail(self, monkeypatch):
        from server import mailbox_runtime as mr

        def bad_connect(host, port):
            raise OSError("Network unreachable")
        monkeypatch.setattr(mr.imaplib, "IMAP4_SSL", bad_connect)

        result = self._run(mr.verify_mailbox_connection(
            "imap.bad.host", 993, "u", "p"
        ))
        assert result["ok"] is False
        assert "Network" in result["error"] or "соедин" in result["error"].lower()


# ── build_mail_context ──────────────────────────────────────────────────────


class TestBuildContext:
    def test_empty_inbox(self):
        from server.mailbox_runtime import build_mail_context
        out = build_mail_context([])
        assert "пуст" in out.lower() or "пуст" in out

    def test_with_emails(self):
        from server.mailbox_runtime import build_mail_context
        out = build_mail_context([
            {"uid": 100, "from": "Иван <ivan@x.ru>",
             "subject": "Просьба о КП", "date": "Mon, 19 May",
             "body_preview": "Здравствуйте! Прошу прислать КП..."},
            {"uid": 99, "from": "spam@x.com",
             "subject": "Реклама", "date": "Mon, 19 May",
             "body_preview": "Скидки!"},
        ])
        assert "Письмо 1" in out
        assert "Иван" in out
        assert "Просьба о КП" in out
        assert "UID:   100" in out
        assert "Письмо 2" in out


# ── fetch_mailbox_recent ────────────────────────────────────────────────────


class FakeMailbox:
    """Mock UserMailbox без БД."""
    def __init__(self, **kw):
        self.is_active = kw.get("is_active", True)
        self.host = kw.get("host", "imap.yandex.ru")
        self.port = kw.get("port", 993)
        self.email = kw.get("email", "me@yandex.ru")
        self.password = kw.get("password", "passw")


class TestFetch:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_inactive_returns_empty(self):
        from server.mailbox_runtime import fetch_mailbox_recent
        result = self._run(fetch_mailbox_recent(FakeMailbox(is_active=False)))
        assert result == []

    def test_none_returns_empty(self):
        from server.mailbox_runtime import fetch_mailbox_recent
        result = self._run(fetch_mailbox_recent(None))
        assert result == []


# ── end-to-end: _build_module_extra_context для slug=mail ───────────────────


def _setup_user_with_mailbox(active=True):
    from server.db import db_session
    from server.models import User, UserMailbox
    with db_session() as db:
        u = User(email=f"mb-{time.time_ns()}@x.x",
                 password_hash="h", is_verified=True,
                 agreed_to_terms=True, tokens_balance=0)
        db.add(u); db.commit(); db.refresh(u)
        b = UserMailbox(
            user_id=u.id, provider="yandex",
            email="real@yandex.ru", host="imap.yandex.ru",
            port=993, password="apppass",
            is_active=active,
        )
        db.add(b); db.commit(); db.refresh(b)
        return u.id, b.id


def _cleanup_user_mailbox(uid):
    from server.db import db_session
    from server.models import User, UserMailbox
    with db_session() as db:
        db.query(UserMailbox).filter_by(user_id=uid).delete()
        db.query(User).filter_by(id=uid).delete()
        db.commit()


class TestModuleExtraContext:
    def test_non_mail_slug_returns_empty(self):
        from server.agent_builder import _build_module_extra_context
        assert _build_module_extra_context("copywriter", user_id=1) == ""
        assert _build_module_extra_context("lawyer", user_id=1) == ""

    def test_no_user_id_returns_empty(self):
        from server.agent_builder import _build_module_extra_context
        assert _build_module_extra_context("mail", user_id=None) == ""
        assert _build_module_extra_context("mail", user_id=0) == ""

    def test_mail_no_mailboxes_returns_empty(self):
        from server.agent_builder import _build_module_extra_context
        # Юзер существует но без подключённых ящиков
        from server.db import db_session
        from server.models import User
        with db_session() as db:
            u = User(email=f"nomb-{time.time_ns()}@x.x", password_hash="h",
                    is_verified=True, agreed_to_terms=True, tokens_balance=0)
            db.add(u); db.commit(); db.refresh(u)
            uid = u.id
        try:
            assert _build_module_extra_context("mail", user_id=uid) == ""
        finally:
            _cleanup_user_mailbox(uid)

    def test_mail_with_mailbox_injects_context(self, monkeypatch):
        from server import agent_builder as ab
        from server import mailbox_runtime as mr

        uid, _ = _setup_user_with_mailbox()
        try:
            # Mock IMAP fetch — возвращает 2 письма
            def fake_fetch(host, port, username, password, limit):
                return [
                    {"uid": 100, "from": "Иван <i@x.ru>",
                     "subject": "КП", "date": "Mon",
                     "body_preview": "Здравствуйте, прошу КП"},
                ]
            monkeypatch.setattr(mr, "_fetch_recent_sync", fake_fetch)

            ctx = ab._build_module_extra_context("mail", user_id=uid)
            assert "ПОЧТА" in ctx
            assert "real@yandex.ru" in ctx  # email ящика
            assert "Иван" in ctx
            assert "КП" in ctx
        finally:
            _cleanup_user_mailbox(uid)

    def test_mail_fetch_exception_returns_friendly(self, monkeypatch):
        from server import agent_builder as ab
        from server import mailbox_runtime as mr

        uid, _ = _setup_user_with_mailbox()
        try:
            def fake_fetch(host, port, username, password, limit):
                raise ConnectionError("boom")
            monkeypatch.setattr(mr, "_fetch_recent_sync", fake_fetch)

            ctx = ab._build_module_extra_context("mail", user_id=uid)
            # Mailbox есть, fetch упал → friendly hint
            assert "не удалось" in ctx.lower() or "ошибк" in ctx.lower() or "boom" in ctx
        finally:
            _cleanup_user_mailbox(uid)
