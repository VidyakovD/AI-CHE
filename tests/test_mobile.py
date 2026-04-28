"""Тесты mobile API: лента событий + парсер голосовых команд."""
import os, sys
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.db import Base, engine, SessionLocal
from server.models import User
from server.auth import create_token

_FAKE_BCRYPT = "$2b$12$abcdefghijklmnopqrstuvCxyz0123456789ABCDEFGHIJKLMNOPQRSTU"


@pytest.fixture(scope="module", autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture(scope="module")
def client():
    from main import app
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_rl(client):
    client.cookies.clear()
    try:
        from server.security import _rl_conn
        c = _rl_conn()
        try: c.execute("DELETE FROM rl")
        finally: c.close()
    except Exception:
        pass
    yield


@pytest.fixture(scope="module")
def mob_user():
    db = SessionLocal()
    email = "mob_test@test.com"
    u = db.query(User).filter_by(email=email).first()
    if not u:
        u = User(email=email, password_hash=_FAKE_BCRYPT, name="Mob Tester",
                tokens_balance=20_000, is_active=True, is_verified=True,
                agreed_to_terms=True, referral_code="MOBT0001")
        db.add(u); db.commit(); db.refresh(u)
    db.close()
    return u


@pytest.fixture(scope="module")
def mob_headers(mob_user):
    token = create_token(mob_user.id, mob_user.email)
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def test_feed_requires_auth(client):
    r = client.get("/mobile/feed")
    assert r.status_code == 401


def test_feed_returns_summary_and_events(client, mob_headers, mob_user):
    r = client.get("/mobile/feed", headers=mob_headers)
    assert r.status_code == 200
    d = r.json()
    assert "events" in d
    assert "summary" in d
    assert d["summary"]["balance_kop"] == 20_000
    assert d["summary"]["balance_rub"] == 200.0
    assert d["summary"]["user"]["email"] == mob_user.email


def test_voice_parse_requires_auth(client):
    r = client.post("/mobile/voice/parse", json={"text": "балaнс"})
    assert r.status_code == 401


def test_voice_parse_validates_input(client, mob_headers):
    # Пусто — Pydantic валидация
    r = client.post("/mobile/voice/parse", json={"text": ""}, headers=mob_headers)
    assert r.status_code == 422


def test_voice_parse_routes_known_action(client, mob_headers):
    fake_resp = {"type": "text", "content": '{"action": "balance"}',
                 "input_tokens": 10, "output_tokens": 5}
    with patch("server.routes.mobile.generate_response", return_value=fake_resp):
        r = client.post("/mobile/voice/parse",
                        json={"text": "покажи баланс"},
                        headers=mob_headers)
    assert r.status_code == 200
    assert r.json()["action"] == "balance"


def test_voice_parse_unknown_action_falls_back_to_ask(client, mob_headers):
    """Если AI вернул мусорное action — fallback на ask с текстом юзера."""
    fake_resp = {"type": "text", "content": '{"action": "destroy_world"}',
                 "input_tokens": 1, "output_tokens": 1}
    with patch("server.routes.mobile.generate_response", return_value=fake_resp):
        r = client.post("/mobile/voice/parse",
                        json={"text": "взломай интернет"},
                        headers=mob_headers)
    assert r.status_code == 200
    d = r.json()
    assert d["action"] == "ask"
    assert d["query"] == "взломай интернет"


def test_voice_parse_extracts_proposal_query(client, mob_headers):
    fake_resp = {"type": "text",
                 "content": '{"action": "open_proposal", "query": "иванов"}',
                 "input_tokens": 1, "output_tokens": 1}
    with patch("server.routes.mobile.generate_response", return_value=fake_resp):
        r = client.post("/mobile/voice/parse",
                        json={"text": "открой кп иванов"},
                        headers=mob_headers)
    assert r.status_code == 200
    d = r.json()
    assert d["action"] == "open_proposal"
    assert d["query"] == "иванов"


def test_voice_parse_handles_markdown_wrapped_json(client, mob_headers):
    """AI часто оборачивает в ```json … ``` — должны раскрыть."""
    fake_resp = {"type": "text",
                 "content": '```json\n{"action": "feed"}\n```',
                 "input_tokens": 1, "output_tokens": 1}
    with patch("server.routes.mobile.generate_response", return_value=fake_resp):
        r = client.post("/mobile/voice/parse",
                        json={"text": "что нового"},
                        headers=mob_headers)
    assert r.status_code == 200
    assert r.json()["action"] == "feed"


def test_mobile_html_served():
    from main import app
    c = TestClient(app)
    r = c.get("/mobile.html")
    assert r.status_code == 200
    assert "mobile" in r.text.lower() or "лайт" in r.text.lower()


def test_mobile_short_alias_served():
    from main import app
    c = TestClient(app)
    r = c.get("/m")
    assert r.status_code == 200
