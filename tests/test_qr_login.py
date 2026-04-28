"""Тесты QR-логина."""
import os, sys
import pytest
from datetime import datetime, timedelta
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.db import Base, engine, SessionLocal
from server.models import User, QrLoginSession
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
def qr_user():
    db = SessionLocal()
    email = "qr_test@test.com"
    u = db.query(User).filter_by(email=email).first()
    if not u:
        u = User(email=email, password_hash=_FAKE_BCRYPT, name="QR Tester",
                tokens_balance=10_000, is_active=True, is_verified=True,
                agreed_to_terms=True, referral_code="QRT00001")
        db.add(u); db.commit(); db.refresh(u)
    db.close()
    return u


@pytest.fixture(scope="module")
def qr_headers(qr_user):
    token = create_token(qr_user.id, qr_user.email)
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def test_init_creates_session_with_token(client):
    r = client.post("/qr-login/init")
    assert r.status_code == 200
    d = r.json()
    assert "token" in d and len(d["token"]) >= 24
    assert d["expires_in"] == 120


def test_poll_pending_after_init(client):
    init = client.post("/qr-login/init").json()
    r = client.get(f"/qr-login/poll/{init['token']}")
    assert r.status_code == 200
    assert r.json()["status"] == "pending"


def test_poll_unknown_token_404(client):
    r = client.get("/qr-login/poll/nonexistent")
    assert r.status_code == 404


def test_info_returns_pending_with_humanized_ua(client):
    init = client.post("/qr-login/init",
                       headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) Version/17.0 Mobile Safari/605.1"}).json()
    r = client.get(f"/qr-login/info/{init['token']}")
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "pending"
    assert "Смартфон" in d["from"]
    assert "iOS" in d["from"]


def test_approve_requires_auth(client):
    init = client.post("/qr-login/init").json()
    r = client.post(f"/qr-login/approve/{init['token']}")
    assert r.status_code == 401


def test_full_flow_init_approve_poll_returns_tokens(client, qr_headers, qr_user):
    # 1. Десктоп инициирует
    init = client.post("/qr-login/init").json()
    token = init["token"]

    # 2. Мобила (с auth) подтверждает
    approve_resp = client.post(f"/qr-login/approve/{token}", headers=qr_headers)
    assert approve_resp.status_code == 200, approve_resp.text
    assert approve_resp.json()["status"] == "approved"

    # 3. Десктоп poll'ом получает токены
    poll_resp = client.get(f"/qr-login/poll/{token}")
    assert poll_resp.status_code == 200
    d = poll_resp.json()
    assert d["status"] == "approved"
    assert "access" in d
    assert "refresh" in d
    assert "csrf_token" in d
    assert d["user"]["email"] == qr_user.email

    # 4. Cookies должны быть установлены
    assert "access_token" in client.cookies or "access_token" in poll_resp.cookies


def test_poll_after_consumed_does_not_leak_tokens(client, qr_headers):
    init = client.post("/qr-login/init").json()
    token = init["token"]
    client.post(f"/qr-login/approve/{token}", headers=qr_headers)
    # Первый poll — отдаёт токены
    r1 = client.get(f"/qr-login/poll/{token}")
    assert r1.json()["status"] == "approved"
    # Второй poll — уже consumed, без токенов
    r2 = client.get(f"/qr-login/poll/{token}")
    assert r2.json()["status"] == "consumed"
    assert "access" not in r2.json()


def test_approve_after_expiry_410(client, qr_headers):
    init = client.post("/qr-login/init").json()
    # Принудительно истекаем сессию
    db = SessionLocal()
    sess = db.query(QrLoginSession).filter_by(token=init["token"]).first()
    sess.expires_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    db.close()
    r = client.post(f"/qr-login/approve/{init['token']}", headers=qr_headers)
    assert r.status_code == 410


def test_cancel_flow(client, qr_headers):
    init = client.post("/qr-login/init").json()
    token = init["token"]
    r = client.post(f"/qr-login/cancel/{token}", headers=qr_headers)
    assert r.status_code == 200
    poll = client.get(f"/qr-login/poll/{token}")
    assert poll.json()["status"] == "cancelled"


def test_double_approve_409(client, qr_headers):
    init = client.post("/qr-login/init").json()
    token = init["token"]
    client.post(f"/qr-login/approve/{token}", headers=qr_headers)
    # Вторая попытка — уже approved, должно ругаться
    r = client.post(f"/qr-login/approve/{token}", headers=qr_headers)
    assert r.status_code == 409


def test_qr_image_returns_png(client):
    init = client.post("/qr-login/init").json()
    r = client.get(f"/qr-login/image/{init['token']}.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    # PNG magic bytes
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(r.content) > 200  # реальный QR


def test_qr_confirm_html_served():
    """Страница /qr/<token> отдаётся."""
    from main import app
    c = TestClient(app)
    init = c.post("/qr-login/init").json()
    r = c.get(f"/qr/{init['token']}")
    assert r.status_code == 200
    assert "qr-login" in r.text.lower() or "Подтверждение" in r.text or "QR" in r.text
