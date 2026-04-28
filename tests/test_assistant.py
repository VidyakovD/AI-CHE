"""
Тесты контекстного помощника (/assistant/ask) и связанных security-фиксов.
"""
import os
import sys
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
        try:
            c.execute("DELETE FROM rl")
        finally:
            c.close()
    except Exception:
        pass
    # Чистим in-mem cache помощника, чтобы каждый тест видел свежий стейт
    try:
        from server.routes.assistant import _ask_cache
        _ask_cache.clear()
    except Exception:
        pass
    yield


@pytest.fixture(scope="module")
def assistant_user():
    db = SessionLocal()
    email = "test_assistant@test.com"
    u = db.query(User).filter_by(email=email).first()
    if not u:
        u = User(
            email=email, password_hash=_FAKE_BCRYPT, name="Asst Tester",
            tokens_balance=10_000, is_active=True, is_verified=True,
            agreed_to_terms=True, referral_code="ASST0001",
        )
        db.add(u); db.commit(); db.refresh(u)
    db.close()
    return u


@pytest.fixture(scope="module")
def assistant_headers(assistant_user):
    token = create_token(assistant_user.id, assistant_user.email)
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ── Тесты системных промптов (без сети) ────────────────────────────────────

def test_prompts_have_all_sections():
    from server.assistant_prompts import SECTION_PROMPTS, build_system_prompt
    expected = {
        "index.chat", "index.cabinet",
        "proposals.projects", "proposals.brands", "proposals.prices",
        "presentations", "sites", "chatbots", "agents", "admin",
    }
    assert expected.issubset(SECTION_PROMPTS.keys())
    for sec in expected:
        p = build_system_prompt(sec)
        assert "КАРТА СЕРВИСА" in p, f"{sec}: nav-footer missing"
        assert len(p) > 200


def test_unknown_section_falls_back_to_default():
    from server.assistant_prompts import build_system_prompt
    p = build_system_prompt("does.not.exist")
    assert "КАРТА СЕРВИСА" in p
    assert len(p) > 200


# ── E2E: /assistant/ask с моком модели ─────────────────────────────────────

def _mock_ai_response(*_args, **_kwargs):
    return {
        "type": "text",
        "content": "Это в КП — [Открыть](/proposals.html). Также см. [презентации](/presentations.html).",
        "input_tokens": 50, "output_tokens": 30,
    }


def test_ask_requires_auth(client):
    r = client.post("/assistant/ask",
                    json={"section": "presentations", "message": "что это?"})
    assert r.status_code == 401


def test_ask_validates_message_length(client, assistant_headers):
    # message > 600 chars rejected
    long_msg = "а" * 700
    r = client.post("/assistant/ask",
                    json={"section": "presentations", "message": long_msg},
                    headers=assistant_headers)
    assert r.status_code == 422


def test_ask_extracts_links_from_answer(client, assistant_headers):
    with patch("server.routes.assistant.generate_response", _mock_ai_response):
        r = client.post("/assistant/ask",
                        json={"section": "presentations",
                              "message": "Хочу сделать КП, как?"},
                        headers=assistant_headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert "answer" in data
    assert "links" in data
    hrefs = [l["href"] for l in data["links"]]
    assert "/proposals.html" in hrefs
    assert "/presentations.html" in hrefs


def test_ask_strips_external_links(client, assistant_headers):
    """Защита от prompt injection: external href не попадает в links[]."""
    def _malicious(*_a, **_k):
        return {"type": "text",
                "content": "Подсказка: [phishing](https://evil.example.com)",
                "input_tokens": 1, "output_tokens": 1}
    with patch("server.routes.assistant.generate_response", _malicious):
        r = client.post("/assistant/ask",
                        json={"section": "presentations", "message": "ссылка?"},
                        headers=assistant_headers)
    assert r.status_code == 200
    assert r.json()["links"] == []


def test_ask_caches_repeat_questions(client, assistant_headers):
    calls = {"n": 0}
    def _counting(*_a, **_k):
        calls["n"] += 1
        return {"type": "text", "content": "answer-" + str(calls["n"]),
                "input_tokens": 1, "output_tokens": 1}
    with patch("server.routes.assistant.generate_response", _counting):
        r1 = client.post("/assistant/ask",
                         json={"section": "sites", "message": "одинаковый вопрос"},
                         headers=assistant_headers)
        r2 = client.post("/assistant/ask",
                         json={"section": "sites", "message": "одинаковый вопрос"},
                         headers=assistant_headers)
    assert r1.status_code == r2.status_code == 200
    assert calls["n"] == 1, "повторный вопрос должен возвращаться из кэша"
    assert r1.json()["answer"] == r2.json()["answer"]


def test_ask_unknown_section_does_not_500(client, assistant_headers):
    with patch("server.routes.assistant.generate_response", _mock_ai_response):
        r = client.post("/assistant/ask",
                        json={"section": "totally.invented",
                              "message": "помоги"},
                        headers=assistant_headers)
    assert r.status_code == 200
    assert r.json()["section"] == "default"


# ── Security regressions ────────────────────────────────────────────────────

def test_svg_sanitizer_blocks_script():
    from server.security import sanitize_svg_or_raise
    from fastapi import HTTPException
    bad = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    with pytest.raises(HTTPException) as exc:
        sanitize_svg_or_raise(bad)
    assert exc.value.status_code == 400


def test_svg_sanitizer_blocks_onload():
    from server.security import sanitize_svg_or_raise
    from fastapi import HTTPException
    bad = b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"></svg>'
    with pytest.raises(HTTPException):
        sanitize_svg_or_raise(bad)


def test_svg_sanitizer_allows_clean():
    from server.security import sanitize_svg_or_raise
    good = b'<svg xmlns="http://www.w3.org/2000/svg"><circle cx="50" cy="50" r="40"/></svg>'
    sanitize_svg_or_raise(good)  # не должно бросить


def test_dns_rebinding_check_rejects_localhost_resolve():
    from server.proposal_builder import _host_resolves_to_private
    assert _host_resolves_to_private("localhost") is True
    assert _host_resolves_to_private("127.0.0.1") is True
    assert _host_resolves_to_private("0.0.0.0") is True
    assert _host_resolves_to_private("") is True


def test_safe_asset_path_rejects_traversal():
    from server.routes.assets import _safe_asset_abs_path, ASSETS_DIR
    # Нормальный путь — резолвится в ASSETS_DIR
    p = _safe_asset_abs_path("/uploads/assets/abc.pdf")
    assert p is not None
    assert str(p).startswith(str(ASSETS_DIR.resolve()))
    # Traversal-попытка — basename отбрасывает директории
    p2 = _safe_asset_abs_path("/../../etc/passwd")
    # Берётся только basename "passwd", который попадает внутрь ASSETS_DIR
    if p2 is not None:
        assert str(p2).startswith(str(ASSETS_DIR.resolve()))
    # Пустой путь
    assert _safe_asset_abs_path("") is None
    assert _safe_asset_abs_path("/") is None
