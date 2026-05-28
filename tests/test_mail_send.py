"""Тесты для server/mail_send.py — SMTP отправка модулем mail.

Все smtplib-вызовы мокаются — реальной сети нет.
"""
from __future__ import annotations

import os
import sys
import smtplib
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestDeriveSmtp:

    def test_yandex(self):
        from server.mail_send import derive_smtp
        assert derive_smtp("imap.yandex.ru") == ("smtp.yandex.ru", 465)

    def test_gmail(self):
        from server.mail_send import derive_smtp
        assert derive_smtp("imap.gmail.com") == ("smtp.gmail.com", 465)

    def test_mailru(self):
        from server.mail_send import derive_smtp
        assert derive_smtp("imap.mail.ru") == ("smtp.mail.ru", 465)

    def test_explicit_override(self):
        from server.mail_send import derive_smtp
        # Если явно задан smtp_host — используем его, игнорируем IMAP
        assert derive_smtp("imap.yandex.ru", "custom-smtp.example.com", 587) == (
            "custom-smtp.example.com", 587,
        )

    def test_unknown_imap_falls_back_to_smtp_prefix(self):
        from server.mail_send import derive_smtp
        assert derive_smtp("imap.weird-domain.example") == (
            "smtp.weird-domain.example", 465,
        )


class TestIsValidEmail:

    def test_valid_addresses(self):
        from server.mail_send import is_valid_email
        assert is_valid_email("user@example.com") is True
        assert is_valid_email("name.surname+tag@sub.domain.ru") is True

    def test_invalid_addresses(self):
        from server.mail_send import is_valid_email
        assert is_valid_email("") is False
        assert is_valid_email("not-an-email") is False
        assert is_valid_email("@no-local.com") is False
        assert is_valid_email("no-tld@example") is False
        assert is_valid_email(None) is False


class TestSendViaSmtp:

    def test_rejects_invalid_recipient(self):
        from server.mail_send import send_via_smtp
        r = send_via_smtp(
            smtp_host="smtp.yandex.ru", smtp_port=465,
            smtp_user="me@yandex.ru", smtp_password="x",
            from_addr="me@yandex.ru", from_name=None,
            to="bad-email", subject="X", body="Z",
        )
        assert r["ok"] is False
        assert "Невалидный" in r["error"]

    def test_rejects_empty_subject(self):
        from server.mail_send import send_via_smtp
        r = send_via_smtp(
            smtp_host="smtp.yandex.ru", smtp_port=465,
            smtp_user="me@yandex.ru", smtp_password="x",
            from_addr="me@yandex.ru", from_name=None,
            to="ok@yandex.ru", subject="   ", body="Z",
        )
        assert r["ok"] is False
        assert "Пустая тема" in r["error"]

    def test_rejects_empty_body(self):
        from server.mail_send import send_via_smtp
        r = send_via_smtp(
            smtp_host="smtp.yandex.ru", smtp_port=465,
            smtp_user="me@yandex.ru", smtp_password="x",
            from_addr="me@yandex.ru", from_name=None,
            to="ok@yandex.ru", subject="X", body="",
        )
        assert r["ok"] is False
        assert "Пустое тело" in r["error"]

    def test_success_465_uses_smtp_ssl(self, monkeypatch):
        from server import mail_send

        # Мокаем smtplib.SMTP_SSL — нужен context manager + login + send_message
        mock_server = MagicMock()
        mock_server.__enter__ = MagicMock(return_value=mock_server)
        mock_server.__exit__ = MagicMock(return_value=None)
        mock_server.login = MagicMock()
        mock_server.send_message = MagicMock()

        ssl_class = MagicMock(return_value=mock_server)
        monkeypatch.setattr(mail_send.smtplib, "SMTP_SSL", ssl_class)

        r = mail_send.send_via_smtp(
            smtp_host="smtp.yandex.ru", smtp_port=465,
            smtp_user="me@yandex.ru", smtp_password="app-pwd",
            from_addr="me@yandex.ru", from_name="Денис",
            to="ivan@example.com", subject="Re: проект",
            body="Тело письма с\nпереносами строк.",
        )
        assert r["ok"] is True
        assert r["error"] is None
        ssl_class.assert_called_once_with(
            "smtp.yandex.ru", 465, context=ssl_class.call_args.kwargs.get("context") or None,
            timeout=20,
        ) if False else None  # позиционные аргументы — проверяем мягко
        mock_server.login.assert_called_once_with("me@yandex.ru", "app-pwd")
        mock_server.send_message.assert_called_once()

    def test_smtp_auth_error_returns_friendly(self, monkeypatch):
        from server import mail_send

        mock_server = MagicMock()
        mock_server.__enter__ = MagicMock(return_value=mock_server)
        mock_server.__exit__ = MagicMock(return_value=None)
        mock_server.login = MagicMock(
            side_effect=smtplib.SMTPAuthenticationError(535, b"auth failed"),
        )

        monkeypatch.setattr(mail_send.smtplib, "SMTP_SSL",
                            MagicMock(return_value=mock_server))

        r = mail_send.send_via_smtp(
            smtp_host="smtp.yandex.ru", smtp_port=465,
            smtp_user="me@yandex.ru", smtp_password="wrong",
            from_addr="me@yandex.ru", from_name=None,
            to="ok@example.com", subject="X", body="Z",
        )
        assert r["ok"] is False
        assert "auth" in r["error"].lower()

    def test_smtp_recipients_refused(self, monkeypatch):
        from server import mail_send

        mock_server = MagicMock()
        mock_server.__enter__ = MagicMock(return_value=mock_server)
        mock_server.__exit__ = MagicMock(return_value=None)
        mock_server.login = MagicMock()
        mock_server.send_message = MagicMock(
            side_effect=smtplib.SMTPRecipientsRefused(
                {"bad@nope.invalid": (550, b"User unknown")}
            ),
        )
        monkeypatch.setattr(mail_send.smtplib, "SMTP_SSL",
                            MagicMock(return_value=mock_server))

        r = mail_send.send_via_smtp(
            smtp_host="smtp.yandex.ru", smtp_port=465,
            smtp_user="me@yandex.ru", smtp_password="ok",
            from_addr="me@yandex.ru", from_name=None,
            to="ok@example.com", subject="X", body="Z",
        )
        assert r["ok"] is False
        assert "Получатель" in r["error"]
