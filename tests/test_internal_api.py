"""Тесты Internal API — общий backend для VK Mini App / TG bot / MAX bot.

Покрывает:
  - HMAC verification (правильный/неправильный/expired)
  - /identify happy path + auto-create
  - /link — добавление identifier, конфликт
  - /link/code/issue + redeem
  - /balance
  - /debit — успех, insufficient_funds, idempotency
  - /credit — успех, idempotency
  - /pricing
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from server.db import SessionLocal


_FAKE_BCRYPT = "$2b$12$abcdefghijklmnopqrstuvCxyz0123456789ABCDEFGHIJKLMNOPQRSTU"


@pytest.fixture(autouse=True)
def _set_secret(monkeypatch):
    """HMAC-секрет нужен для всех тестов кроме /health."""
    monkeypatch.setenv("INTERNAL_API_SECRET", "test-internal-secret")


def _client():
    from main import app
    return TestClient(app)


def _sign(body: str = "", ts: int | None = None) -> dict:
    """Подписать запрос. Возвращает заголовки X-Internal-*."""
    secret = os.environ["INTERNAL_API_SECRET"]
    ts_str = str(ts if ts is not None else int(time.time()))
    sig = hmac.new(
        secret.encode(), f"{ts_str}.{body}".encode(), hashlib.sha256
    ).hexdigest()
    return {
        "X-Internal-Timestamp": ts_str,
        "X-Internal-Signature": sig,
    }


def _post(path: str, payload: dict | None = None, ts: int | None = None) -> "Response":
    body = json.dumps(payload, ensure_ascii=False) if payload else ""
    return _client().post(
        path, content=body if body else None,
        headers={**_sign(body, ts), "Content-Type": "application/json"},
    )


def _get(path: str, ts: int | None = None) -> "Response":
    return _client().get(path, headers=_sign("", ts))


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


# ── HMAC ─────────────────────────────────────────────────────────────────


class TestHMAC:
    def test_health_no_hmac_needed(self):
        r = _client().get("/internal/v1/health")
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_no_signature_rejected(self):
        r = _client().post("/internal/v1/balance/1")
        assert r.status_code in (401, 405)  # 405 because POST on GET-only path

    def test_invalid_signature_rejected(self):
        r = _client().get("/internal/v1/pricing", headers={
            "X-Internal-Timestamp": str(int(time.time())),
            "X-Internal-Signature": "deadbeef" * 8,
        })
        assert r.status_code == 401

    def test_old_timestamp_rejected(self):
        # ts старее 5 мин
        r = _get("/internal/v1/pricing", ts=int(time.time()) - 1000)
        assert r.status_code == 401

    def test_secret_not_set_returns_503(self, monkeypatch):
        monkeypatch.setenv("INTERNAL_API_SECRET", "")
        r = _get("/internal/v1/pricing")
        assert r.status_code == 503


# ── /identify ─────────────────────────────────────────────────────────────


class TestIdentify:
    def test_identify_existing_user_by_tg(self):
        suffix = uuid.uuid4().hex[:8]
        db = SessionLocal()
        try:
            u = _make_user(db, f"id-tg-{suffix}@test.com",
                            tg_user_id=f"tg-{suffix}", tokens_balance=12345)
            uid = u.id
        finally:
            db.close()

        r = _post("/internal/v1/identify", {
            "kind": "tg_user_id", "value": f"tg-{suffix}", "auto_create": False,
        })
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["user_id"] == uid
        assert data["is_new"] is False
        assert data["balance_kop"] == 12345

    def test_identify_unknown_no_auto_create_404(self):
        r = _post("/internal/v1/identify", {
            "kind": "tg_user_id", "value": "definitely-not-existing",
            "auto_create": False,
        })
        assert r.status_code == 404

    def test_identify_unknown_auto_creates(self):
        suffix = uuid.uuid4().hex[:8]
        r = _post("/internal/v1/identify", {
            "kind": "tg_user_id", "value": f"new-tg-{suffix}",
            "display_name": "Test User",
        })
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["is_new"] is True
        assert data["tg_user_id"] == f"new-tg-{suffix}"
        assert data["balance_kop"] == 0

    def test_identify_invalid_kind_400(self):
        r = _post("/internal/v1/identify", {"kind": "fingerprint", "value": "x"})
        assert r.status_code == 400

    def test_identify_phone_normalization(self):
        """Поиск по +7 формату должен матчить юзера записанного с 8 в начале."""
        # Уникальный digit-only suffix (4 цифры, иначе телефон с буквами невалиден)
        suffix_int = int(time.time() * 1000) % 10000
        suffix_str = f"{suffix_int:04d}"
        normalized = f"+7903000{suffix_str}"
        db = SessionLocal()
        try:
            u = _make_user(db, f"id-phone-{suffix_str}-{uuid.uuid4().hex[:4]}@test.com",
                            phone=normalized)
            uid = u.id
        finally:
            db.close()
        # Ищем по «8 в начале» — должен найти
        eights = f"8903000{suffix_str}"
        r = _post("/internal/v1/identify", {
            "kind": "phone", "value": eights, "auto_create": False,
        })
        assert r.status_code == 200, r.text
        assert r.json()["user_id"] == uid


# ── /link ─────────────────────────────────────────────────────────────────


class TestLink:
    def test_link_adds_identifier(self):
        suffix = uuid.uuid4().hex[:8]
        db = SessionLocal()
        try:
            u = _make_user(db, f"link-{suffix}@test.com")
            uid = u.id
        finally:
            db.close()

        r = _post("/internal/v1/link", {
            "user_id": uid, "kind": "vk_user_id", "value": f"99{int(time.time())}",
        })
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True

    def test_link_conflict_409(self):
        """vk_user_id уже у другого юзера → 409."""
        suffix = uuid.uuid4().hex[:8]
        vk_id = int(time.time() * 1000) % (10**10)
        db = SessionLocal()
        try:
            u1 = _make_user(db, f"link-conflict-a-{suffix}@test.com",
                             vk_user_id=vk_id)
            u2 = _make_user(db, f"link-conflict-b-{suffix}@test.com")
            uid2 = u2.id
        finally:
            db.close()

        r = _post("/internal/v1/link", {
            "user_id": uid2, "kind": "vk_user_id", "value": str(vk_id),
        })
        assert r.status_code == 409


# ── /link/code ────────────────────────────────────────────────────────────


class TestLinkCode:
    def test_issue_and_redeem_roundtrip(self):
        suffix = uuid.uuid4().hex[:8]
        db = SessionLocal()
        try:
            u = _make_user(db, f"linkcode-{suffix}@test.com")
            uid = u.id
        finally:
            db.close()

        # Выдаём код
        r1 = _post("/internal/v1/link/code/issue", {
            "user_id": uid, "kind": "tg_user_id",
        })
        assert r1.status_code == 200, r1.text
        code = r1.json()["code"]
        assert len(code) == 6

        # Используем
        r2 = _post("/internal/v1/link/code/redeem", {
            "code": code, "value": f"tg-{suffix}",
        })
        assert r2.status_code == 200, r2.text
        assert r2.json()["user_id"] == uid

        # Второй redeem уже не работает
        r3 = _post("/internal/v1/link/code/redeem", {
            "code": code, "value": "x",
        })
        assert r3.status_code == 404


# ── /balance ──────────────────────────────────────────────────────────────


class TestBalance:
    def test_balance_returns_kop(self):
        suffix = uuid.uuid4().hex[:8]
        db = SessionLocal()
        try:
            u = _make_user(db, f"bal-{suffix}@test.com", tokens_balance=50000)
            uid = u.id
        finally:
            db.close()
        r = _get(f"/internal/v1/balance/{uid}")
        assert r.status_code == 200, r.text
        assert r.json()["balance_kop"] == 50000
        assert r.json()["currency"] == "RUB"

    def test_balance_unknown_user_404(self):
        r = _get("/internal/v1/balance/99999999")
        assert r.status_code == 404


# ── /debit ────────────────────────────────────────────────────────────────


class TestDebit:
    def test_debit_success(self):
        suffix = uuid.uuid4().hex[:8]
        db = SessionLocal()
        try:
            u = _make_user(db, f"debit-{suffix}@test.com", tokens_balance=10000)
            uid = u.id
        finally:
            db.close()

        r = _post("/internal/v1/debit", {
            "user_id": uid, "amount_kop": 2500,
            "reason": "vk_miniapp:chat_message",
            "idempotency_key": f"vk-test-{suffix}",
        })
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True
        assert data["new_balance_kop"] == 7500
        assert "transaction_id" in data

    def test_debit_insufficient_funds(self):
        suffix = uuid.uuid4().hex[:8]
        db = SessionLocal()
        try:
            u = _make_user(db, f"debit-ins-{suffix}@test.com", tokens_balance=100)
            uid = u.id
        finally:
            db.close()

        r = _post("/internal/v1/debit", {
            "user_id": uid, "amount_kop": 500, "reason": "test",
            "idempotency_key": f"ins-{suffix}",
        })
        assert r.status_code == 200
        assert r.json()["ok"] is False
        assert r.json()["reason"] == "insufficient_funds"
        assert r.json()["balance_kop"] == 100

    def test_debit_idempotency_replays(self):
        """Тот же idempotency_key возвращает кешированный ответ, баланс
        списан только 1 раз."""
        suffix = uuid.uuid4().hex[:8]
        db = SessionLocal()
        try:
            u = _make_user(db, f"debit-idem-{suffix}@test.com",
                            tokens_balance=10000)
            uid = u.id
        finally:
            db.close()

        idem = f"idem-{suffix}"
        r1 = _post("/internal/v1/debit", {
            "user_id": uid, "amount_kop": 3000, "reason": "test",
            "idempotency_key": idem,
        })
        r2 = _post("/internal/v1/debit", {
            "user_id": uid, "amount_kop": 3000, "reason": "test",
            "idempotency_key": idem,
        })
        assert r1.status_code == 200 and r2.status_code == 200
        assert r1.json() == r2.json()  # тот же ответ
        # Списание было только одно
        db = SessionLocal()
        try:
            from server.models import User
            cur = db.query(User).filter_by(id=uid).first().tokens_balance
        finally:
            db.close()
        assert cur == 7000, f"Должно списаться один раз, баланс {cur}"

    def test_debit_missing_idempotency_key_400(self):
        r = _post("/internal/v1/debit", {
            "user_id": 1, "amount_kop": 100, "reason": "x",
        })
        assert r.status_code == 400

    def test_debit_negative_amount_400(self):
        r = _post("/internal/v1/debit", {
            "user_id": 1, "amount_kop": -100, "reason": "x",
            "idempotency_key": "neg",
        })
        assert r.status_code == 400


# ── /credit ───────────────────────────────────────────────────────────────


class TestCredit:
    def test_credit_success(self):
        suffix = uuid.uuid4().hex[:8]
        db = SessionLocal()
        try:
            u = _make_user(db, f"credit-{suffix}@test.com", tokens_balance=1000)
            uid = u.id
        finally:
            db.close()

        r = _post("/internal/v1/credit", {
            "user_id": uid, "amount_kop": 5000, "source": "vk_pay",
            "idempotency_key": f"credit-{suffix}",
            "description": "topup via VK Pay 50 голосов",
        })
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        assert r.json()["new_balance_kop"] == 6000

    def test_credit_idempotency_replays(self):
        suffix = uuid.uuid4().hex[:8]
        db = SessionLocal()
        try:
            u = _make_user(db, f"credit-idem-{suffix}@test.com",
                            tokens_balance=0)
            uid = u.id
        finally:
            db.close()

        idem = f"credit-idem-{suffix}"
        r1 = _post("/internal/v1/credit", {
            "user_id": uid, "amount_kop": 2000, "source": "manual",
            "idempotency_key": idem,
        })
        r2 = _post("/internal/v1/credit", {
            "user_id": uid, "amount_kop": 2000, "source": "manual",
            "idempotency_key": idem,
        })
        assert r1.json() == r2.json()
        db = SessionLocal()
        try:
            from server.models import User
            cur = db.query(User).filter_by(id=uid).first().tokens_balance
        finally:
            db.close()
        assert cur == 2000  # одно начисление


# ── /pricing ──────────────────────────────────────────────────────────────


class TestPricing:
    def test_pricing_returns_keys_and_rate(self):
        r = _get("/internal/v1/pricing")
        assert r.status_code == 200, r.text
        data = r.json()
        assert "keys" in data
        assert "exchange_rate_usd_rub" in data
        assert isinstance(data["keys"], dict)
        assert isinstance(data["exchange_rate_usd_rub"], (int, float))
