"""OAuth flow для Calendar (Google + Yandex CalDAV).

Покрывают:
  - State CSRF: _save_oauth_state / _consume_oauth_state (TTL, single-use)
  - GET /user/calendar/google/connect — формирование redirect URL
  - GET /user/calendar/google/callback — обмен code → tokens → UserCalendarConnection
  - POST /user/calendar/yandex/connect — валидация email/app-password

Все HTTP к Google мокаются (httpx.AsyncClient).
"""
import os
import sys
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.db import SessionLocal


_FAKE_BCRYPT = "$2b$12$abcdefghijklmnopqrstuvCxyz0123456789ABCDEFGHIJKLMNOPQRSTU"


def _make_user(db, email: str):
    from server.models import User
    u = db.query(User).filter_by(email=email).first()
    if u:
        u.is_verified = True
        u.agreed_to_terms = True
        db.commit()
        return u
    u = User(
        email=email,
        password_hash=_FAKE_BCRYPT,
        name=email.split("@")[0],
        tokens_balance=0,
        is_verified=True,
        agreed_to_terms=True,
        referral_code=uuid.uuid4().hex[:8].upper(),
    )
    db.add(u); db.commit(); db.refresh(u)
    return u


# ── State helpers (CSRF-protection) ───────────────────────────────────────────


class TestOAuthState:

    def test_save_and_consume_roundtrip(self):
        from server.routes.user import _save_oauth_state, _consume_oauth_state
        state = "u1.test-state-roundtrip-1"
        _save_oauth_state(42, state)
        assert _consume_oauth_state(state) == 42

    def test_consume_unknown_returns_none(self):
        from server.routes.user import _consume_oauth_state
        assert _consume_oauth_state("totally-unknown-state-xyz") is None

    def test_consume_is_single_use(self):
        """Повторный consume должен вернуть None — защита от повторной отправки callback."""
        from server.routes.user import _save_oauth_state, _consume_oauth_state
        state = "u7.test-state-single-use"
        _save_oauth_state(7, state)
        assert _consume_oauth_state(state) == 7
        # Второй раз — None (state удалён)
        assert _consume_oauth_state(state) is None

    def test_ttl_expired_state_rejected(self, monkeypatch):
        """State старше TTL должен быть отклонён."""
        from server.routes import user as user_routes
        state = "u9.test-state-ttl"
        # Сохраняем как обычно
        user_routes._save_oauth_state(9, state)
        # Подменяем монотонное время на «прошло 16 минут» (TTL = 15 мин)
        real_monotonic = time.monotonic
        future = real_monotonic() + (user_routes._OAUTH_STATE_TTL + 60)
        monkeypatch.setattr("time.monotonic", lambda: future)
        assert user_routes._consume_oauth_state(state) is None


# ── /user/calendar/google/connect — формирование redirect URL ───────────────


class TestGoogleConnectStart:

    def _make_authed_client(self, email: str = "google-oauth-start@test.com"):
        from fastapi.testclient import TestClient
        from server.auth import create_token
        from main import app
        db = SessionLocal()
        try:
            u = _make_user(db, email)
            user_id = u.id
        finally:
            db.close()
        token = create_token(user_id, email)
        client = TestClient(app)
        return client, token, user_id

    def test_missing_client_id_returns_503(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
        client, token, _ = self._make_authed_client()
        r = client.get("/user/calendar/google/connect",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 503
        assert "GOOGLE_CLIENT_ID" in (r.json().get("detail") or "")

    def test_returns_redirect_url_with_state_and_scope(self, monkeypatch):
        from urllib.parse import urlparse, parse_qs
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
        client, token, user_id = self._make_authed_client(
            "google-oauth-start-2@test.com",
        )
        r = client.get("/user/calendar/google/connect",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text
        url = r.json()["redirect_url"]
        assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        q = parse_qs(urlparse(url).query)
        assert q["client_id"] == ["test-client-id"]
        assert q["scope"] == ["https://www.googleapis.com/auth/calendar.events.readonly"]
        assert q["access_type"] == ["offline"]
        assert q["prompt"] == ["consent"]
        assert q["response_type"] == ["code"]
        # state начинается с user_id.<random>
        state = q["state"][0]
        assert state.startswith(f"{user_id}.")
        # И state должен быть сохранён в RAM-cache → consume даст user_id
        from server.routes.user import _consume_oauth_state
        assert _consume_oauth_state(state) == user_id


# ── /user/calendar/google/callback — обмен code → tokens → connection ───────


def _mock_async_httpx_client(responses_by_call):
    """Builds a MagicMock httpx.AsyncClient that returns prepared responses
    by order of .post() / .get() calls. Each entry: ('post'|'get', mock_response).
    """
    calls_iter = iter(responses_by_call)

    async def _post(*a, **kw):
        kind, resp = next(calls_iter)
        assert kind == "post", f"Expected post, got {kind} (args={a}, kw={kw})"
        return resp

    async def _get(*a, **kw):
        kind, resp = next(calls_iter)
        assert kind == "get", f"Expected get, got {kind} (args={a}, kw={kw})"
        return resp

    mock_client = MagicMock()
    mock_client.post = AsyncMock(side_effect=_post)
    mock_client.get = AsyncMock(side_effect=_get)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    return mock_client


class TestGoogleCallback:

    def test_callback_with_error_param_returns_html_error(self, monkeypatch):
        from fastapi.testclient import TestClient
        from main import app
        client = TestClient(app)
        r = client.get("/user/calendar/google/callback?error=access_denied",
                       follow_redirects=False)
        assert r.status_code == 400
        assert "access_denied" in r.text

    def test_callback_without_code_returns_error(self):
        from fastapi.testclient import TestClient
        from main import app
        client = TestClient(app)
        r = client.get("/user/calendar/google/callback?state=foo",
                       follow_redirects=False)
        assert r.status_code == 400
        assert "code" in r.text.lower()

    def test_callback_with_unknown_state_rejected(self):
        from fastapi.testclient import TestClient
        from main import app
        client = TestClient(app)
        r = client.get("/user/calendar/google/callback?code=abc&state=unknown",
                       follow_redirects=False)
        assert r.status_code == 400
        # state не найден — сообщение о просроченной сессии
        assert ("сесси" in r.text.lower()) or ("истек" in r.text.lower())

    def test_callback_success_creates_connection(self, monkeypatch):
        from fastapi.testclient import TestClient
        from main import app
        from server.models import UserCalendarConnection
        from server.routes.user import _save_oauth_state
        from server import calendar_sync as _cs  # noqa: только для проверки что модуль импортирован
        from server.routes import user as user_routes

        monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-cid")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-secret")

        # Создаём юзера и сохраняем state
        db = SessionLocal()
        try:
            u = _make_user(db, "google-cb-success@test.com")
            user_id = u.id
            # Удалим все старые connection чтобы провериться на чистом state
            db.query(UserCalendarConnection).filter_by(user_id=user_id,
                                                       provider="google").delete()
            db.commit()
        finally:
            db.close()

        state = f"{user_id}.cb-success-test"
        _save_oauth_state(user_id, state)

        # Готовим моки httpx: POST на token endpoint + GET на userinfo
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json = MagicMock(return_value={
            "access_token": "at_abc",
            "refresh_token": "rt_xyz",
            "expires_in": 3600,
        })
        userinfo_resp = MagicMock()
        userinfo_resp.status_code = 200
        userinfo_resp.json = MagicMock(return_value={"email": "user@gmail.com"})

        # Подменяем httpx.AsyncClient в user_routes (там делается import httpx локально)
        # — патчим глобальный модуль httpx.AsyncClient через monkeypatch.
        import httpx as _httpx

        # Каждый async with httpx.AsyncClient() создаёт новый клиент.
        # Возвращаем последовательно: token_client → userinfo_client.
        client_iter = iter([
            _mock_async_httpx_client([("post", token_resp)]),
            _mock_async_httpx_client([("get", userinfo_resp)]),
        ])
        monkeypatch.setattr(_httpx, "AsyncClient", lambda **kw: next(client_iter))

        # Дёргаем callback
        client = TestClient(app)
        r = client.get(f"/user/calendar/google/callback?code=auth_code_123&state={state}",
                       follow_redirects=False)
        assert r.status_code == 200, r.text[:500]
        assert "user@gmail.com" in r.text or "подключ" in r.text.lower()

        # Проверяем что connection создан
        db = SessionLocal()
        try:
            conn = (db.query(UserCalendarConnection)
                      .filter_by(user_id=user_id, provider="google",
                                 account_email="user@gmail.com")
                      .first())
            assert conn is not None
            assert conn.access_token == "at_abc"
            assert conn.refresh_token == "rt_xyz"
            assert conn.is_active is True
            assert conn.calendar_id == "primary"
        finally:
            db.close()

    def test_callback_token_exchange_4xx_returns_error_html(self, monkeypatch):
        from fastapi.testclient import TestClient
        from main import app
        from server.routes.user import _save_oauth_state

        monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-cid")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-secret")

        db = SessionLocal()
        try:
            u = _make_user(db, "google-cb-4xx@test.com")
            user_id = u.id
        finally:
            db.close()

        state = f"{user_id}.cb-4xx-test"
        _save_oauth_state(user_id, state)

        # Token endpoint вернёт 400
        token_resp = MagicMock()
        token_resp.status_code = 400
        token_resp.text = '{"error":"invalid_grant"}'
        token_resp.json = MagicMock(return_value={"error": "invalid_grant"})

        import httpx as _httpx
        monkeypatch.setattr(
            _httpx, "AsyncClient",
            lambda **kw: _mock_async_httpx_client([("post", token_resp)]),
        )

        client = TestClient(app)
        r = client.get(f"/user/calendar/google/callback?code=bad&state={state}",
                       follow_redirects=False)
        assert r.status_code == 400
        assert ("400" in r.text) or ("отклон" in r.text.lower())

    def test_callback_without_refresh_token_returns_error(self, monkeypatch):
        """Google вернул access без refresh — это значит prompt=consent не сработал.
        Должны не сохранять и сообщить юзеру переподключиться.
        """
        from fastapi.testclient import TestClient
        from main import app
        from server.routes.user import _save_oauth_state

        monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-cid")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-secret")

        db = SessionLocal()
        try:
            u = _make_user(db, "google-cb-norefresh@test.com")
            user_id = u.id
        finally:
            db.close()

        state = f"{user_id}.cb-norefresh-test"
        _save_oauth_state(user_id, state)

        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json = MagicMock(return_value={
            "access_token": "at_only",
            "expires_in": 3600,
            # refresh_token отсутствует — должно быть отклонено
        })

        import httpx as _httpx
        monkeypatch.setattr(
            _httpx, "AsyncClient",
            lambda **kw: _mock_async_httpx_client([("post", token_resp)]),
        )

        client = TestClient(app)
        r = client.get(f"/user/calendar/google/callback?code=ok&state={state}",
                       follow_redirects=False)
        assert r.status_code == 400
        assert "refresh_token" in r.text.lower() or "refresh" in r.text.lower()


# ── /user/calendar/yandex/connect — input validation ────────────────────────


class TestYandexCalDavConnect:

    def _make_authed_client(self, email: str = "yandex-conn@test.com"):
        from fastapi.testclient import TestClient
        from server.auth import create_token
        from main import app
        db = SessionLocal()
        try:
            u = _make_user(db, email)
            user_id = u.id
        finally:
            db.close()
        token = create_token(user_id, email)
        client = TestClient(app)
        return client, token, user_id

    def test_invalid_email_rejected(self, monkeypatch):
        client, token, _ = self._make_authed_client("yandex-bad-email@test.com")
        r = client.post(
            "/user/calendar/yandex/connect",
            headers={"Authorization": f"Bearer {token}"},
            json={"email": "not-an-email", "app_password": "x" * 16},
        )
        assert r.status_code == 400
        assert "email" in (r.json().get("detail") or "").lower()

    def test_short_app_password_rejected(self):
        client, token, _ = self._make_authed_client("yandex-bad-pwd@test.com")
        r = client.post(
            "/user/calendar/yandex/connect",
            headers={"Authorization": f"Bearer {token}"},
            json={"email": "u@yandex.ru", "app_password": "short"},
        )
        assert r.status_code == 400
        assert "password" in (r.json().get("detail") or "").lower() \
            or "app-password" in (r.json().get("detail") or "").lower()

    def test_yandex_creds_rejected_by_caldav_returns_400(self, monkeypatch):
        """Если CalDAV вернул 401 → endpoint возвращает 400 с понятной ошибкой."""
        client, token, _ = self._make_authed_client("yandex-401@test.com")

        async def _bad_creds(email, pwd, *a, **kw):
            return {"ok": False, "error": "Yandex вернул 401 (неверный app-password)"}

        from server.routes import user as user_routes
        # Подменяем yandex_caldav_check_creds через monkeypatch — он импортируется
        # внутри handler'а из server.calendar_sync. Патчим module-attr.
        from server import calendar_sync as _cs
        monkeypatch.setattr(_cs, "yandex_caldav_check_creds", _bad_creds)

        r = client.post(
            "/user/calendar/yandex/connect",
            headers={"Authorization": f"Bearer {token}"},
            json={"email": "u@yandex.ru", "app_password": "valid-looking-pwd"},
        )
        assert r.status_code == 400
        assert "Yandex" in (r.json().get("detail") or "")

    def test_yandex_creds_ok_creates_connection(self, monkeypatch):
        from server.models import UserCalendarConnection
        client, token, user_id = self._make_authed_client("yandex-ok@test.com")

        # Чистим существующие подключения юзера
        db = SessionLocal()
        try:
            db.query(UserCalendarConnection).filter_by(
                user_id=user_id, provider="yandex",
            ).delete()
            db.commit()
        finally:
            db.close()

        async def _ok_creds(email, pwd, *a, **kw):
            return {"ok": True}

        from server import calendar_sync as _cs
        monkeypatch.setattr(_cs, "yandex_caldav_check_creds", _ok_creds)

        r = client.post(
            "/user/calendar/yandex/connect",
            headers={"Authorization": f"Bearer {token}"},
            json={"email": "u@yandex.ru", "app_password": "valid-app-password"},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "connected"
        assert data["account_email"] == "u@yandex.ru"

        db = SessionLocal()
        try:
            conn = (db.query(UserCalendarConnection)
                      .filter_by(user_id=user_id, provider="yandex",
                                 account_email="u@yandex.ru")
                      .first())
            assert conn is not None
            assert conn.is_active is True
        finally:
            db.close()
