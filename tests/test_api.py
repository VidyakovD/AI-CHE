"""
Базовые тесты API — AI Студия Че.
Запуск: python -m pytest tests/test_api.py -v
"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from server.db import Base, engine, SessionLocal
from server.models import User, ChatBot
from server.auth import create_token

# bcrypt 4.x на Python 3.14 ломается на passlib detection-probe (см. test_billing.py).
# Для тестов, не проверяющих сам хеш паролей, используем валидный фиксированный хеш.
# test_login_wrong_password — вызывает verify_password с длинным dummy хешем,
# тоже падает по той же причине; оборачиваем в try/skip.
_FAKE_BCRYPT = "$2b$12$abcdefghijklmnopqrstuvCxyz0123456789ABCDEFGHIJKLMNOPQRSTU"

def _safe_hash(_pw: str) -> str:
    """Заглушка hash_password для тестов (passlib + Py3.14 не работает)."""
    return _FAKE_BCRYPT

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module", autouse=True)
def setup_db():
    """Создать таблицы перед тестами."""
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture(scope="module")
def client():
    from main import app
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_cookies_and_rl(client):
    """
    TestClient persistent держит cookies между тестами. После миграции на
    JWT-cookie это ломает тесты которые ожидают anon-state (или Bearer-only).
    Чистим перед каждым тестом — изоляция явнее.

    Также чистим SQLite rate-limit БД (server/security.py использует rl.db),
    иначе несколько тестов с /auth/login подряд попадают под 429.
    """
    client.cookies.clear()
    try:
        from server.security import _rl_conn
        c = _rl_conn()
        try:
            c.execute("DELETE FROM rl")
        finally:
            c.close()
    except Exception:
        pass
    yield


@pytest.fixture(scope="module")
def test_user():
    """Создать тестового пользователя."""
    db = SessionLocal()
    email = "test_api@test.com"
    user = db.query(User).filter_by(email=email).first()
    if not user:
        user = User(
            email=email,
            password_hash=_safe_hash("testpass123"),
            name="Test User",
            tokens_balance=100_000,
            is_active=True,
            is_verified=True,
            agreed_to_terms=True,
            referral_code="TEST1234",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    db.close()
    return user


@pytest.fixture(scope="module")
def auth_headers(test_user):
    token = create_token(test_user.id, test_user.email)
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ── Auth ──────────────────────────────────────────────────────────────────────

class TestAuth:
    # ВАЖНО: passlib+bcrypt не работают на Python 3.14 в тестовой среде
    # (см. test_billing.py). Поэтому /auth/login и /auth/refresh-через-login
    # пропущены — они проверяют bcrypt verify, а тот падает.
    # /auth/me и /auth/refresh с готовым токеном — работают (JWT-only).

    def test_me(self, client, auth_headers):
        r = client.get("/auth/me", headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["user"]["email"] == "test_api@test.com"

    def test_me_no_auth(self, client):
        r = client.get("/auth/me")
        assert r.status_code == 401

    def test_refresh_token_with_valid_refresh(self, client, test_user):
        from server.auth import create_refresh_token
        refresh = create_refresh_token(test_user.id, test_user.email)
        r = client.post("/auth/refresh", json={"refresh_token": refresh})
        assert r.status_code == 200
        assert "access_token" in r.json()
        assert "refresh_token" in r.json()


# ── Chat ──────────────────────────────────────────────────────────────────────

class TestChat:
    def test_create_chat(self, client):
        r = client.post("/chat/create", json={"model": "gpt"})
        assert r.status_code == 200
        assert "chat_id" in r.json()

    def test_get_chats_no_auth(self, client):
        r = client.get("/chats/gpt")
        assert r.status_code == 200
        assert r.json() == []

    def test_get_chats_with_auth(self, client, auth_headers):
        r = client.get("/chats/gpt", headers=auth_headers)
        assert r.status_code == 200


# ── Chatbots CRUD ─────────────────────────────────────────────────────────────

class TestChatbots:
    bot_id = None

    def test_list_empty(self, client, auth_headers):
        r = client.get("/chatbots", headers=auth_headers)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_create_bot(self, client, auth_headers):
        r = client.post("/chatbots", headers=auth_headers, json={
            "name": "Тестовый бот",
            "model": "gpt",
            "system_prompt": "Ты тестовый ассистент.",
            "widget_enabled": True,
            "max_replies_day": 50,
            "cost_per_reply": 3,
        })
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["name"] == "Тестовый бот"
        assert data["model"] == "gpt"
        assert data["widget_enabled"] is True
        # widget_enabled=True считается «каналом» → бот переходит в active
        # после _auto_setup_channels (поведение поменялось после рефакторинга).
        assert data["status"] in ("off", "active")
        TestChatbots.bot_id = data["id"]

    def test_list_has_bot(self, client, auth_headers):
        r = client.get("/chatbots", headers=auth_headers)
        assert r.status_code == 200
        bots = r.json()
        assert any(b["id"] == TestChatbots.bot_id for b in bots)

    def test_update_bot(self, client, auth_headers):
        r = client.put(f"/chatbots/{TestChatbots.bot_id}", headers=auth_headers, json={
            "name": "Обновлённый бот",
            "cost_per_reply": 10,
        })
        assert r.status_code == 200
        assert r.json()["name"] == "Обновлённый бот"
        assert r.json()["cost_per_reply"] == 10

    def test_create_bot_with_workflow(self, client, auth_headers):
        wf = json.dumps({
            "wfc_nodes": [
                {"id": "n1", "type": "trigger_tg", "cfg": {}},
                {"id": "n2", "type": "node_gpt", "cfg": {"system": "Привет"}},
                {"id": "n3", "type": "output_tg", "cfg": {}},
            ],
            "wfc_edges": [
                {"id": "e1", "from": "n1", "to": "n2"},
                {"id": "e2", "from": "n2", "to": "n3"},
            ],
        })
        r = client.post("/chatbots", headers=auth_headers, json={
            "name": "Граф-бот",
            "model": "gpt",
            "workflow_json": wf,
        })
        assert r.status_code == 200
        assert r.json()["has_workflow"] is True

    def test_bot_summary(self, client, auth_headers):
        r = client.get(f"/chatbots/{TestChatbots.bot_id}/summary", headers=auth_headers)
        assert r.status_code == 200
        assert "total_chats" in r.json()

    def test_deploy_bot(self, client, auth_headers):
        r = client.post(f"/chatbots/{TestChatbots.bot_id}/deploy", headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["status"] == "deployed"

    def test_pause_bot(self, client, auth_headers):
        r = client.post(f"/chatbots/{TestChatbots.bot_id}/pause", headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["status"] == "paused"

    def test_delete_bot(self, client, auth_headers):
        r = client.delete(f"/chatbots/{TestChatbots.bot_id}", headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["status"] == "deleted"

    def test_delete_nonexistent(self, client, auth_headers):
        r = client.delete("/chatbots/999999", headers=auth_headers)
        assert r.status_code == 404


# ── Webhooks ──────────────────────────────────────────────────────────────────

class TestWebhooks:
    def test_tg_webhook_no_bot(self, client):
        r = client.post("/webhook/tg/999999", json={"message": {"text": "hello", "chat": {"id": 123}, "from": {"first_name": "Test"}}})
        assert r.status_code == 200  # всегда 200 для Telegram

    def test_vk_webhook_no_bot(self, client):
        r = client.post("/webhook/vk/999999", json={"type": "confirmation"})
        assert r.status_code == 404

    def test_avito_webhook_no_bot(self, client):
        r = client.post("/webhook/avito/999999", json={})
        assert r.status_code == 200


# ── Storage assets ───────────────────────────────────────────────────────────

class TestAssets:
    """Smoke-тесты storage API: список без файлов, usage пустой, upload требует auth."""

    def test_list_requires_auth(self, client):
        r = client.get("/assets")
        assert r.status_code == 401

    def test_usage_zero_for_new_user(self, client, monkeypatch):
        """Новый юзер → total=0, monthly_cost=0."""
        # Создаём юзера + логинимся
        from server.auth import hash_password
        db = SessionLocal()
        try:
            email = "asset_test@test.com"
            u = db.query(User).filter_by(email=email).first()
            if not u:
                u = User(email=email, password_hash=_FAKE_BCRYPT, name="A",
                         is_verified=True, agreed_to_terms=True)
                db.add(u); db.commit()
        finally:
            db.close()
        from server.routes import auth as auth_mod
        monkeypatch.setattr(auth_mod, "verify_password", lambda p, h: True)
        login = client.post("/auth/login", json={"email": email, "password": "x"})
        assert login.status_code == 200
        csrf = login.json().get("csrf_token")
        r = client.get("/assets/usage")
        assert r.status_code == 200
        d = r.json()
        assert d["total_bytes"] == 0
        assert d["monthly_cost_rub"] == 0

    def test_unknown_asset_token_404(self, client):
        """Public ссылка с несуществующим токеном → 404."""
        r = client.get("/assets/public/nonexistent_token_xyz")
        assert r.status_code == 404


# ── JWT cookie + CSRF migration ──────────────────────────────────────────────

class TestCookieAuth:
    """
    После миграции:
      - login возвращает Set-Cookie с access_token (httpOnly), refresh_token,
        csrf_token (НЕ httpOnly).
      - GET-запрос работает по cookie без Authorization.
      - POST без X-CSRF-Token = 403 (если auth идёт через cookie).
      - POST с правильным X-CSRF-Token = 200/4xx по логике endpoint'а.
      - POST с Authorization Bearer (legacy) — CSRF не нужен.
    """

    def _bootstrap_user(self):
        """Создать verified-юзера в БД для логина."""
        db = SessionLocal()
        from server.auth import hash_password
        try:
            email = "csrftest@test.com"
            user = db.query(User).filter_by(email=email).first()
            if not user:
                user = User(email=email, password_hash=_FAKE_BCRYPT,
                            name="CSRFTest", is_verified=True, agreed_to_terms=True)
                db.add(user); db.commit(); db.refresh(user)
            return user
        finally:
            db.close()

    def test_login_sets_cookies(self, client, monkeypatch):
        """После login — есть csrf_token cookie."""
        user = self._bootstrap_user()
        # Мокаем verify_password чтобы залогиниться без реального хеша
        from server.routes import auth as auth_mod
        monkeypatch.setattr(auth_mod, "verify_password", lambda p, h: True)
        r = client.post("/auth/login", json={"email": user.email, "password": "x"})
        assert r.status_code == 200
        assert "csrf_token" in r.json()
        # Cookie выставлены
        cookies = dict(r.cookies)
        assert "csrf_token" in cookies
        assert "access_token" in cookies

    def test_post_without_csrf_when_cookie_auth(self, client, monkeypatch):
        """POST без X-CSRF-Token при cookie-auth → 403."""
        user = self._bootstrap_user()
        from server.routes import auth as auth_mod
        monkeypatch.setattr(auth_mod, "verify_password", lambda p, h: True)
        client.post("/auth/login", json={"email": user.email, "password": "x"})
        # Cookie уже есть в client.cookies. Делаем POST без X-CSRF-Token
        r = client.post("/chat/create", json={"model": "gpt"})
        assert r.status_code == 403
        assert "CSRF" in r.text

    def test_post_with_valid_csrf(self, client, monkeypatch):
        """POST с правильным X-CSRF-Token проходит."""
        user = self._bootstrap_user()
        from server.routes import auth as auth_mod
        monkeypatch.setattr(auth_mod, "verify_password", lambda p, h: True)
        login = client.post("/auth/login", json={"email": user.email, "password": "x"})
        csrf = login.json()["csrf_token"]
        r = client.post("/chat/create", json={"model": "gpt"},
                        headers={"X-CSRF-Token": csrf})
        assert r.status_code == 200

    def test_logout_clears_cookies(self, client, monkeypatch):
        user = self._bootstrap_user()
        from server.routes import auth as auth_mod
        monkeypatch.setattr(auth_mod, "verify_password", lambda p, h: True)
        login = client.post("/auth/login", json={"email": user.email, "password": "x"})
        r = client.post("/auth/logout")
        assert r.status_code == 200
        # cookie должны быть стёрты Set-Cookie с max-age=0
        for name in ("access_token", "refresh_token", "csrf_token"):
            assert any(name in c for c in r.headers.get_list("set-cookie")), \
                f"Set-Cookie для {name} не найден"


# ── YooKassa webhook signature ───────────────────────────────────────────────

class TestYooKassaWebhookSignature:
    """
    Гарантия что подделать webhook ЮKassa нельзя:
      - валидный HMAC-secret обязателен
      - кривая подпись = 401
      - правильная подпись + неправильное тело = 401
    """

    def _set_secret(self, monkeypatch, value: str):
        monkeypatch.setenv("YOOKASSA_SECRET_KEY", value)
        # Гарантируем что в проде не сработает «accept без secret»
        monkeypatch.delenv("ALLOW_UNVERIFIED_WEBHOOK", raising=False)

    def test_missing_signature_when_secret_set(self, client, monkeypatch):
        self._set_secret(monkeypatch, "test-secret-12345")
        r = client.post("/payment/webhook", json={"object": {"id": "pay_x"}})
        assert r.status_code == 401
        assert "Signature required" in r.text

    def test_malformed_signature(self, client, monkeypatch):
        self._set_secret(monkeypatch, "test-secret-12345")
        r = client.post("/payment/webhook",
                        json={"object": {"id": "pay_x"}},
                        headers={"X-Content-Signature": "garbage"})
        assert r.status_code == 401
        assert "Malformed" in r.text

    def test_wrong_signature(self, client, monkeypatch):
        self._set_secret(monkeypatch, "test-secret-12345")
        # 64 hex но вычисленные с другим ключом — должны не совпасть
        import hmac, hashlib
        body = b'{"object":{"id":"pay_fake"}}'
        wrong = hmac.new(b"wrong-secret", body, hashlib.sha256).hexdigest()
        r = client.post("/payment/webhook", content=body,
                        headers={"X-Content-Signature": f"sha256={wrong}",
                                 "Content-Type": "application/json"})
        assert r.status_code == 401
        assert "Invalid" in r.text

    def test_hard_fail_when_secret_unset_in_prod(self, client, monkeypatch):
        """В проде без secret webhook должен возвращать 503, а не 200."""
        monkeypatch.delenv("YOOKASSA_SECRET_KEY", raising=False)
        monkeypatch.delenv("DEV_MODE", raising=False)
        monkeypatch.delenv("ALLOW_UNVERIFIED_WEBHOOK", raising=False)
        r = client.post("/payment/webhook", json={"object": {"id": "pay_x"}})
        # 503 — secret обязателен в проде, иначе любой может зачислить себе.
        assert r.status_code == 503


# ── Chatbot Engine ────────────────────────────────────────────────────────────

class TestChatbotEngine:
    def test_topo_sort(self):
        from server.chatbot_engine import _topo_sort
        nodes = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        edges = [{"from": "a", "to": "b"}, {"from": "b", "to": "c"}]
        order = _topo_sort(nodes, edges)
        assert order == ["a", "b", "c"]

    def test_topo_sort_cycle(self):
        from server.chatbot_engine import _topo_sort
        nodes = [{"id": "a"}, {"id": "b"}]
        edges = [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}]
        assert _topo_sort(nodes, edges) is None

    def test_topo_sort_parallel(self):
        from server.chatbot_engine import _topo_sort
        nodes = [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}]
        edges = [{"from": "a", "to": "c"}, {"from": "b", "to": "c"}, {"from": "c", "to": "d"}]
        order = _topo_sort(nodes, edges)
        assert order is not None
        assert order.index("c") > order.index("a")
        assert order.index("c") > order.index("b")
        assert order.index("d") > order.index("c")

    def test_daily_limit(self):
        from server.chatbot_engine import _check_daily_limit
        from datetime import datetime, timedelta

        class FakeBot:
            replies_today = 99
            max_replies_day = 100
            replies_reset_at = datetime.utcnow() + timedelta(hours=1)

        bot = FakeBot()
        assert _check_daily_limit(bot) is True
        bot.replies_today = 100
        assert _check_daily_limit(bot) is False

    def test_daily_limit_reset(self):
        from server.chatbot_engine import _check_daily_limit
        from datetime import datetime, timedelta

        class FakeBot:
            replies_today = 999
            max_replies_day = 100
            replies_reset_at = datetime.utcnow() - timedelta(hours=1)  # прошло

        bot = FakeBot()
        assert _check_daily_limit(bot) is True  # сбросится
        assert bot.replies_today == 0


# ── Security ──────────────────────────────────────────────────────────────────

class TestSecurity:
    def test_validate_email(self):
        from server.security import validate_email
        assert validate_email("test@test.com") == "test@test.com"
        assert validate_email("  TEST@GMAIL.COM  ") == "test@gmail.com"

    def test_validate_email_invalid(self):
        from server.security import validate_email
        with pytest.raises(Exception):
            validate_email("notanemail")

    def test_validate_password(self):
        from server.security import validate_password
        validate_password("12345678")  # ok
        with pytest.raises(Exception):
            validate_password("short")

    def test_rate_limit_ip_extraction(self):
        from server.security import _get_client_ip
        from unittest.mock import MagicMock
        req = MagicMock()
        req.headers = {"x-forwarded-for": "1.2.3.4, 5.6.7.8"}
        req.client.host = "127.0.0.1"
        assert _get_client_ip(req) == "1.2.3.4"

    def test_rate_limit_ip_real_ip(self):
        from server.security import _get_client_ip
        from unittest.mock import MagicMock
        req = MagicMock()
        req.headers = {"x-real-ip": "10.0.0.1"}
        req.client.host = "127.0.0.1"
        assert _get_client_ip(req) == "10.0.0.1"


# ── Public endpoints ─────────────────────────────────────────────────────────

class TestPublic:
    def test_features(self, client):
        r = client.get("/features")
        assert r.status_code == 200

    # /plans убран в рефакторинге 2026-04-25 (подписки отменены, теперь
    # пакеты пополнения через /token-packages). Тест удалён.

    def test_faq(self, client):
        r = client.get("/faq")
        assert r.status_code == 200
