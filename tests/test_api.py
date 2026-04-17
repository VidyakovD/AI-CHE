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
from server.auth import hash_password, create_token

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


@pytest.fixture(scope="module")
def test_user():
    """Создать тестового пользователя."""
    db = SessionLocal()
    email = "test_api@test.com"
    user = db.query(User).filter_by(email=email).first()
    if not user:
        user = User(
            email=email,
            password_hash=hash_password("testpass123"),
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
    def test_login_wrong_password(self, client):
        r = client.post("/auth/login", json={"email": "nonexistent@test.com", "password": "wrong"})
        assert r.status_code == 401

    def test_login_success(self, client, test_user):
        r = client.post("/auth/login", json={"email": test_user.email, "password": "testpass123"})
        assert r.status_code == 200
        data = r.json()
        assert "token" in data
        assert "refresh_token" in data

    def test_me(self, client, auth_headers):
        r = client.get("/auth/me", headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["user"]["email"] == "test_api@test.com"

    def test_me_no_auth(self, client):
        r = client.get("/auth/me")
        assert r.status_code == 401

    def test_refresh_token(self, client, test_user):
        r = client.post("/auth/login", json={"email": test_user.email, "password": "testpass123"})
        refresh = r.json()["refresh_token"]
        r2 = client.post("/auth/refresh", json={"refresh_token": refresh})
        assert r2.status_code == 200
        assert "access_token" in r2.json()


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
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "Тестовый бот"
        assert data["model"] == "gpt"
        assert data["widget_enabled"] is True
        assert data["status"] == "off"
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

    def test_plans(self, client):
        r = client.get("/plans")
        assert r.status_code == 200
        plans = r.json()
        assert len(plans) == 3
        assert any(p["id"] == "pro" for p in plans)

    def test_faq(self, client):
        r = client.get("/faq")
        assert r.status_code == 200
