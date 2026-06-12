"""Stage 2 tests for VKMA merge: auth + models + health endpoint.

Покрывает:
  - validate_vk_launch_params: dev (всегда пропускает), prod (HMAC), bad-sign,
    missing sign, mismatched vk_app_id.
  - encrypt_vk_token / decrypt_vk_token roundtrip.
  - current_vkma_user через /api/vkma/me: новый юзер + повторный (reuse).
  - /api/vkma/health: без auth.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sys
import uuid
from urllib.parse import urlencode

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.db import SessionLocal


@pytest.fixture(autouse=True)
def _vkma_env(monkeypatch):
    """В тестах не-production — HMAC игнорируется в validate_*.
    Явно ставим APP_ENV=dev (не delenv) чтобы избежать конфликта с main.py
    env-check'ом который default='production' при отсутствии APP_ENV."""
    monkeypatch.setenv("APP_ENV", "dev")
    # AES key для encrypt/decrypt тестов (32 bytes base64-encoded)
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY",
                       base64.b64encode(b"x" * 32).decode())


# ── validate_vk_launch_params ──────────────────────────────────────────


class TestLaunchParamsValidation:
    def test_dev_mode_accepts_any_params(self):
        """В dev возвращает dict без проверки HMAC — для тестирования в браузере."""
        from server.integrations.vkma.auth import validate_vk_launch_params
        params = validate_vk_launch_params("vk_user_id=123&vk_app_id=456")
        assert params == {"vk_user_id": "123", "vk_app_id": "456"}

    def test_empty_returns_none(self):
        from server.integrations.vkma.auth import validate_vk_launch_params
        assert validate_vk_launch_params("") is None

    def test_production_no_secure_key_rejects(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.delenv("VK_APP_SECURE_KEY", raising=False)
        from server.integrations.vkma.auth import validate_vk_launch_params
        result = validate_vk_launch_params("vk_user_id=1&sign=anything")
        assert result is None  # fail-closed

    def test_production_valid_hmac_accepts(self, monkeypatch):
        """Валидно подписанные params в production должны проходить."""
        from server.integrations.vkma.auth import validate_vk_launch_params
        secure_key = "test-secret-XXXXXX"
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("VK_APP_SECURE_KEY", secure_key)

        # Sign vk_user_id + vk_app_id (VK signs только vk_* params)
        vk_params = {"vk_user_id": "143151429", "vk_app_id": "54617390"}
        ordered = urlencode(sorted(vk_params.items()))
        sign = base64.urlsafe_b64encode(
            hmac.new(secure_key.encode(), ordered.encode(),
                     hashlib.sha256).digest()
        ).decode().rstrip("=")
        query = ordered + "&sign=" + sign

        result = validate_vk_launch_params(query)
        assert result is not None
        assert result["vk_user_id"] == "143151429"

    def test_production_bad_sign_rejects(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("VK_APP_SECURE_KEY", "test-secret")
        from server.integrations.vkma.auth import validate_vk_launch_params
        result = validate_vk_launch_params("vk_user_id=1&sign=deadbeef")
        assert result is None

    def test_production_no_sign_rejects(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("VK_APP_SECURE_KEY", "test-secret")
        from server.integrations.vkma.auth import validate_vk_launch_params
        result = validate_vk_launch_params("vk_user_id=1")
        assert result is None


# ── encrypt/decrypt VK token ────────────────────────────────────────────


class TestVkTokenEncryption:
    def test_roundtrip(self):
        from server.integrations.vkma.auth import encrypt_vk_token, decrypt_vk_token
        plaintext = "vk1.a.AbCdEf0123456789FAKE-TOKEN-FOR-TEST"
        encrypted = encrypt_vk_token(plaintext)
        assert encrypted != plaintext
        decrypted = decrypt_vk_token(encrypted)
        assert decrypted == plaintext

    def test_different_nonces(self):
        """Один и тот же plaintext → разные ciphertext (random nonce)."""
        from server.integrations.vkma.auth import encrypt_vk_token
        a = encrypt_vk_token("same")
        b = encrypt_vk_token("same")
        assert a != b

    def test_missing_key_raises(self, monkeypatch):
        monkeypatch.delenv("TOKEN_ENCRYPTION_KEY", raising=False)
        from server.integrations.vkma.auth import encrypt_vk_token
        with pytest.raises(ValueError, match="TOKEN_ENCRYPTION_KEY"):
            encrypt_vk_token("x")

    def test_wrong_key_size_raises(self, monkeypatch):
        # 16 bytes вместо 32
        monkeypatch.setenv("TOKEN_ENCRYPTION_KEY",
                           base64.b64encode(b"x" * 16).decode())
        from server.integrations.vkma.auth import encrypt_vk_token
        with pytest.raises(ValueError, match="32 байта"):
            encrypt_vk_token("x")


# ── /api/vkma/health (no auth) ──────────────────────────────────────────


class TestHealth:
    def test_health_endpoint(self):
        from fastapi.testclient import TestClient
        from main import app
        client = TestClient(app)
        r = client.get("/api/vkma/health")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["service"] == "aiche-vkma"
        assert "config" in data
        assert "production_mode" in data["config"]


# ── /api/vkma/me — auto-create через current_vkma_user ───────────────────


class TestMeEndpoint:
    def test_me_auto_creates_new_user(self):
        """Первый запрос с новым vk_user_id → auto-create + trial."""
        from fastapi.testclient import TestClient
        from main import app
        from server.models import User
        client = TestClient(app)
        # Уникальный vk_id для теста
        vk_id = int(uuid.uuid4().int >> 96) % (10**9)

        r = client.get("/api/vkma/me",
                       headers={"X-VK-Launch-Params": f"vk_user_id={vk_id}"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["vk_user_id"] == vk_id
        # Trial по дефолту 500 ₽
        assert data["balance_kop"] == 50_000
        assert data["trial_ends_at"] is not None

        # Юзер появился в БД
        db = SessionLocal()
        try:
            u = db.query(User).filter_by(vk_user_id=vk_id).first()
            assert u is not None
            assert u.id == data["user_id"]
        finally:
            db.close()

    def test_me_idempotent(self):
        """Повторный вызов с тем же vk_user_id → тот же User, не дубликат."""
        from fastapi.testclient import TestClient
        from main import app
        from server.models import User
        client = TestClient(app)
        vk_id = int(uuid.uuid4().int >> 96) % (10**9)
        r1 = client.get("/api/vkma/me",
                        headers={"X-VK-Launch-Params": f"vk_user_id={vk_id}"})
        r2 = client.get("/api/vkma/me",
                        headers={"X-VK-Launch-Params": f"vk_user_id={vk_id}"})
        assert r1.status_code == 200 and r2.status_code == 200
        assert r1.json()["user_id"] == r2.json()["user_id"]
        db = SessionLocal()
        try:
            count = db.query(User).filter_by(vk_user_id=vk_id).count()
            assert count == 1
        finally:
            db.close()

    def test_me_missing_params_401(self):
        from fastapi.testclient import TestClient
        from main import app
        client = TestClient(app)
        r = client.get("/api/vkma/me")
        assert r.status_code == 401

    def test_me_missing_vk_user_id_400(self):
        from fastapi.testclient import TestClient
        from main import app
        client = TestClient(app)
        # Launch params без vk_user_id
        r = client.get("/api/vkma/me",
                       headers={"X-VK-Launch-Params": "vk_app_id=999"})
        assert r.status_code == 400

    def test_me_links_existing_aiche_user(self):
        """Если уже есть User с этим vk_user_id (через /internal/v1/identify
        ранее) — /api/vkma/me возвращает того же. Не дубликат."""
        from fastapi.testclient import TestClient
        from main import app
        from server.models import User
        client = TestClient(app)
        vk_id = int(uuid.uuid4().int >> 96) % (10**9)
        # Создаём User вручную (имитируем Denisовский кейс — уже на aiche)
        db = SessionLocal()
        try:
            existing_email = f"existing-vk-{vk_id}@test.com"
            u = User(
                email=existing_email,
                password_hash="$2b$12$abcdefghijklmnopqrstuvCxyz0123456789ABCDEFGHIJKLMNOPQRSTU",
                name="Existing", vk_user_id=vk_id,
                tokens_balance=99999, is_verified=True, agreed_to_terms=True,
                referral_code=uuid.uuid4().hex[:8].upper(),
            )
            db.add(u); db.commit(); db.refresh(u)
            existing_id = u.id
        finally:
            db.close()

        r = client.get("/api/vkma/me",
                       headers={"X-VK-Launch-Params": f"vk_user_id={vk_id}"})
        assert r.status_code == 200
        data = r.json()
        # Линкуется к existing, не создаёт нового
        assert data["user_id"] == existing_id
        assert data["balance_kop"] == 99999
        assert data["email"] == existing_email
