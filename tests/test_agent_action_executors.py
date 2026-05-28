"""Тесты исполнителей agent_actions.execute_action: finance / coach / nutrition.

Mail send и Google Calendar create — отдельно (нужны mocks для SMTP/HTTP).
Тут только те, что пишут в локальную БД.
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


# ── add_finance_transaction ─────────────────────────────────────────────────


class TestAddFinanceTransaction:

    def test_creates_expense(self):
        from server.agent_actions import execute_action
        from server.models import FinanceTransaction

        db = SessionLocal()
        try:
            u = _make_user(db, "fin-tx-expense@test.com")
            uid = u.id
        finally:
            db.close()

        r = execute_action("add_finance_transaction", {
            "amount_kop": -15000, "category": "transport",
            "description": "такси домой",
        }, uid)
        assert r["ok"] is True
        tx_id = r["result"]["transaction_id"]

        db = SessionLocal()
        try:
            tx = db.query(FinanceTransaction).get(tx_id)
            assert tx is not None
            assert tx.user_id == uid
            assert tx.amount_kop == -15000
            assert tx.category == "transport"
            assert tx.description == "такси домой"
            assert tx.source == "manual"
        finally:
            db.close()

    def test_creates_income(self):
        from server.agent_actions import execute_action
        db = SessionLocal()
        try:
            u = _make_user(db, "fin-tx-income@test.com")
            uid = u.id
        finally:
            db.close()

        r = execute_action("add_finance_transaction", {
            "amount_kop": 5000000, "category": "income",
            "description": "оплата от клиента",
        }, uid)
        assert r["ok"] is True
        assert r["result"]["amount_rub"] == 50000.0

    def test_zero_amount_rejected(self):
        from server.agent_actions import execute_action
        r = execute_action("add_finance_transaction", {
            "amount_kop": 0, "category": "other",
        }, user_id=1)
        assert r["ok"] is False
        assert "0" in (r.get("error") or "")

    def test_amount_over_cap_rejected(self):
        from server.agent_actions import execute_action
        # 30 млн рублей = 3 млрд копеек — превышает 2 млрд лимит
        r = execute_action("add_finance_transaction", {
            "amount_kop": -3_000_000_000, "category": "other",
        }, user_id=1)
        assert r["ok"] is False
        assert "лимит" in (r.get("error") or "").lower()

    def test_unknown_category_falls_back_to_other(self):
        from server.agent_actions import execute_action
        from server.models import FinanceTransaction

        db = SessionLocal()
        try:
            u = _make_user(db, "fin-tx-unknown-cat@test.com")
            uid = u.id
        finally:
            db.close()

        r = execute_action("add_finance_transaction", {
            "amount_kop": -1000, "category": "xxxxxx_nonexistent",
        }, uid)
        assert r["ok"] is True
        db = SessionLocal()
        try:
            tx = db.query(FinanceTransaction).get(r["result"]["transaction_id"])
            assert tx.category == "other"
        finally:
            db.close()


# ── log_workout ─────────────────────────────────────────────────────────────


class TestLogWorkout:

    def test_simple_sets(self):
        from server.agent_actions import execute_action
        from server.models import WorkoutLog

        db = SessionLocal()
        try:
            u = _make_user(db, "workout-simple@test.com")
            uid = u.id
        finally:
            db.close()

        r = execute_action("log_workout", {
            "exercise": "присед", "sets": "100×8, 100×8, 105×6",
        }, uid)
        assert r["ok"] is True
        assert r["result"]["sets_count"] == 3
        assert r["result"]["total_volume_kg"] == pytest.approx(2230.0)

        db = SessionLocal()
        try:
            row = db.query(WorkoutLog).get(r["result"]["id"])
            assert row.exercise == "присед"
            sets = json.loads(row.sets_json)
            assert len(sets) == 3
            assert sets[0] == {"weight": 100.0, "reps": 8}
            assert sets[2] == {"weight": 105.0, "reps": 6}
        finally:
            db.close()

    def test_expansion_via_third_factor(self):
        """80×8×3 → 3 одинаковых подхода."""
        from server.agent_actions import execute_action
        db = SessionLocal()
        try:
            u = _make_user(db, "workout-expand@test.com")
            uid = u.id
        finally:
            db.close()

        r = execute_action("log_workout", {
            "exercise": "жим лёжа", "sets": "80×8×4",
        }, uid)
        assert r["ok"] is True
        assert r["result"]["sets_count"] == 4
        # 80*8*4 = 2560
        assert r["result"]["total_volume_kg"] == pytest.approx(2560.0)

    def test_missing_exercise_rejected(self):
        from server.agent_actions import execute_action
        r = execute_action("log_workout", {"sets": "100×8"}, user_id=1)
        assert r["ok"] is False
        assert "exercise" in (r.get("error") or "").lower()

    def test_unparseable_sets_rejected(self):
        from server.agent_actions import execute_action
        r = execute_action("log_workout", {
            "exercise": "x", "sets": "хрень какая-то"
        }, user_id=1)
        assert r["ok"] is False

    def test_x_separator_works_too(self):
        """Латинский x вместо × тоже работает (LLM может выдать любой)."""
        from server.agent_actions import execute_action
        db = SessionLocal()
        try:
            u = _make_user(db, "workout-x-sep@test.com")
            uid = u.id
        finally:
            db.close()
        r = execute_action("log_workout", {
            "exercise": "подтягивания", "sets": "0x12, 0x10, 0x8",
        }, uid)
        assert r["ok"] is True
        assert r["result"]["sets_count"] == 3


# ── log_meal ────────────────────────────────────────────────────────────────


class TestLogMeal:

    def test_creates_meal_record(self):
        from server.agent_actions import execute_action
        from server.models import MealLog

        db = SessionLocal()
        try:
            u = _make_user(db, "meal-create@test.com")
            uid = u.id
        finally:
            db.close()

        r = execute_action("log_meal", {
            "meal_type": "lunch",
            "description": "курица гриль + рис + салат",
            "calories": 550, "protein_g": 45, "fat_g": 15, "carbs_g": 50,
        }, uid)
        assert r["ok"] is True

        db = SessionLocal()
        try:
            row = db.query(MealLog).get(r["result"]["id"])
            assert row.meal_type == "lunch"
            assert row.calories == 550
            assert row.protein_g == 45
            assert "курица" in row.description
        finally:
            db.close()

    def test_invalid_meal_type_rejected(self):
        from server.agent_actions import execute_action
        r = execute_action("log_meal", {
            "meal_type": "ужинчик", "description": "что-то",
        }, user_id=1)
        assert r["ok"] is False
        assert "meal_type" in (r.get("error") or "").lower()

    def test_calories_can_be_omitted(self):
        from server.agent_actions import execute_action
        db = SessionLocal()
        try:
            u = _make_user(db, "meal-no-cal@test.com")
            uid = u.id
        finally:
            db.close()
        r = execute_action("log_meal", {
            "meal_type": "snack", "description": "яблоко",
        }, uid)
        assert r["ok"] is True
        assert r["result"]["calories"] is None


# ── send_email (без реального SMTP) ─────────────────────────────────────────


class TestExecuteSendEmail:

    def test_missing_mailbox_id(self):
        from server.agent_actions import execute_action
        r = execute_action("send_email", {
            "to": "x@y.com", "subject": "X", "body": "Z",
        }, user_id=1)
        assert r["ok"] is False
        assert "mailbox" in (r.get("error") or "").lower()

    def test_nonexistent_mailbox(self):
        from server.agent_actions import execute_action
        db = SessionLocal()
        try:
            u = _make_user(db, "send-email-nomailbox@test.com")
            uid = u.id
        finally:
            db.close()
        r = execute_action("send_email", {
            "mailbox_id": 999999, "to": "x@y.com",
            "subject": "X", "body": "Z",
        }, uid)
        assert r["ok"] is False
        assert "не найден" in (r.get("error") or "").lower()

    def test_invalid_recipient(self):
        from server.agent_actions import execute_action
        r = execute_action("send_email", {
            "mailbox_id": 1, "to": "not-an-email",
            "subject": "X", "body": "Z",
        }, user_id=1)
        assert r["ok"] is False
        assert "Невалидный" in (r.get("error") or "")

    def test_send_via_mocked_smtp(self, monkeypatch):
        """Создаём mailbox и эмулируем успешный SMTP."""
        from server.agent_actions import execute_action
        from server.models import UserMailbox

        db = SessionLocal()
        try:
            u = _make_user(db, "send-email-mock@test.com")
            uid = u.id
            # Удалим старый ящик если есть
            db.query(UserMailbox).filter_by(user_id=uid,
                                            email="me@yandex.ru").delete()
            mb = UserMailbox(
                user_id=uid, provider="yandex",
                email="me@yandex.ru", host="imap.yandex.ru", port=993,
                password="app-pwd-encrypted-via-EncryptedString",
                is_active=True,
            )
            db.add(mb); db.commit(); db.refresh(mb)
            mailbox_id = mb.id
        finally:
            db.close()

        # Мокаем send_via_smtp
        from server import mail_send as ms
        called = {}

        def _fake_send(**kw):
            called.update(kw)
            return {"ok": True, "message_id": "<abc@yandex.ru>", "error": None}

        monkeypatch.setattr(ms, "send_via_smtp", _fake_send)
        # Так как _execute_send_email делает локальный import, патчим в agent_actions namespace
        from server import agent_actions as aa
        # Просто пушим _fake_send в server.mail_send namespace — глобально подменено выше

        r = execute_action("send_email", {
            "mailbox_id": mailbox_id, "to": "ivan@example.com",
            "subject": "Re: тест", "body": "Здравствуйте, Иван!",
        }, uid)
        assert r["ok"] is True, r
        assert called["smtp_host"] == "smtp.yandex.ru"   # auto-derive
        assert called["smtp_port"] == 465
        assert called["to"] == "ivan@example.com"
        assert called["subject"] == "Re: тест"
        assert called["smtp_user"] == "me@yandex.ru"
