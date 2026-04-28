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


def test_ask_strips_markdown_links_from_answer(client, assistant_headers):
    """Markdown [label](href) в answer заменяется на просто label —
    текст становится чистым, ссылки рендерятся отдельным блоком."""
    def _resp(*_a, **_k):
        return {"type": "text",
                "content": "Это в КП — [Открыть КП](/proposals.html). А это [Презентации](/presentations.html).",
                "input_tokens": 1, "output_tokens": 1}
    with patch("server.routes.assistant.generate_response", _resp):
        r = client.post("/assistant/ask",
                        json={"section": "index.cabinet", "message": "куда идти?"},
                        headers=assistant_headers)
    assert r.status_code == 200
    d = r.json()
    # Markdown синтаксис убран
    assert "](/" not in d["answer"]
    assert "[Открыть" not in d["answer"]
    # Текст содержит только labels
    assert "Открыть КП" in d["answer"]
    assert "Презентации" in d["answer"]
    # Ссылки структурированы
    hrefs = [l["href"] for l in d["links"]]
    assert "/proposals.html" in hrefs
    assert "/presentations.html" in hrefs


def test_ask_dedupes_repeat_links(client, assistant_headers):
    """Если AI указал одну и ту же ссылку дважды — links[] не дублирует."""
    def _resp(*_a, **_k):
        return {"type": "text",
                "content": "Жми [Открыть КП](/proposals.html) или [КП](/proposals.html).",
                "input_tokens": 1, "output_tokens": 1}
    with patch("server.routes.assistant.generate_response", _resp):
        r = client.post("/assistant/ask",
                        json={"section": "index.cabinet", "message": "?"},
                        headers=assistant_headers)
    hrefs = [l["href"] for l in r.json()["links"]]
    assert hrefs.count("/proposals.html") == 1


def test_ask_returns_feedback_id_and_creates_record(client, assistant_headers):
    """Каждый /ask создаёт запись AssistantFeedback и возвращает её id."""
    from server.db import SessionLocal
    from server.models import AssistantFeedback
    with patch("server.routes.assistant.generate_response", _mock_ai_response):
        r = client.post("/assistant/ask",
                        json={"section": "proposals.projects",
                              "message": "как создать кп для нового клиента"},
                        headers=assistant_headers)
    assert r.status_code == 200
    fid = r.json().get("feedback_id")
    assert isinstance(fid, int) and fid > 0
    db = SessionLocal()
    try:
        fb = db.query(AssistantFeedback).filter_by(id=fid).first()
        assert fb is not None
        assert fb.section == "proposals.projects"
        assert "новый" in fb.message.lower() or "клиент" in fb.message.lower()
    finally:
        db.close()


def test_feedback_mark_thumbs_up(client, assistant_headers):
    with patch("server.routes.assistant.generate_response", _mock_ai_response):
        r = client.post("/assistant/ask",
                        json={"section": "proposals.projects", "message": "нечто"},
                        headers=assistant_headers)
    fid = r.json()["feedback_id"]
    r2 = client.post("/assistant/feedback",
                     json={"feedback_id": fid, "mark": "up"},
                     headers=assistant_headers)
    assert r2.status_code == 200
    assert r2.json()["user_mark"] == "up"


def test_feedback_idea_promotes_classification(client, assistant_headers):
    """Если юзер пометил «💡 идея» — классификация принудительно идёт в idea."""
    with patch("server.routes.assistant.generate_response", _mock_ai_response):
        r = client.post("/assistant/ask",
                        json={"section": "proposals.projects",
                              "message": "хочу новую функцию"},
                        headers=assistant_headers)
    fid = r.json()["feedback_id"]
    client.post("/assistant/feedback",
                json={"feedback_id": fid, "mark": "idea"},
                headers=assistant_headers)
    from server.db import SessionLocal
    from server.models import AssistantFeedback
    db = SessionLocal()
    try:
        fb = db.query(AssistantFeedback).filter_by(id=fid).first()
        assert fb.classification == "idea"
        assert fb.user_mark == "idea"
    finally:
        db.close()


def test_feedback_invalid_mark_rejected(client, assistant_headers):
    r = client.post("/assistant/feedback",
                    json={"feedback_id": 1, "mark": "evil"},
                    headers=assistant_headers)
    assert r.status_code == 422


def test_feedback_someone_elses_record_404(client, assistant_headers):
    """Юзер не может голосовать за чужие записи."""
    from server.db import SessionLocal
    from server.models import AssistantFeedback, User
    import uuid
    db = SessionLocal()
    try:
        suffix = uuid.uuid4().hex[:8]
        other = User(email=f"other_fb_{suffix}@test.com", password_hash=_FAKE_BCRYPT,
                     name="other", tokens_balance=0, is_active=True, is_verified=True,
                     agreed_to_terms=True, referral_code=f"OTH{suffix[:5]}".upper())
        db.add(other); db.commit(); db.refresh(other)
        fb = AssistantFeedback(user_id=other.id, message="чужое",
                               classification="question", confidence=50)
        db.add(fb); db.commit(); db.refresh(fb)
        fid = fb.id
    finally:
        db.close()
    r = client.post("/assistant/feedback",
                    json={"feedback_id": fid, "mark": "up"},
                    headers=assistant_headers)
    assert r.status_code == 404


def test_prompts_have_extended_content():
    """После расширения каждый prompt должен быть существенно длиннее baseline."""
    from server.assistant_prompts import SECTION_PROMPTS, build_system_prompt
    for sec, p in SECTION_PROMPTS.items():
        assert len(p) > 600, f"{sec}: prompt too short ({len(p)} chars)"
    full = build_system_prompt("proposals.projects")
    assert "[Открыть КП](/proposals.html)" in full
    assert "СТРОГО" in full


def test_ask_caches_repeat_questions(client, assistant_headers):
    """Кэш: повторный вопрос НЕ должен делать новых AI-вызовов.
    Первый /ask делает 2 вызова (ответ + фоновая классификация), второй — 0."""
    calls = {"n": 0}
    def _counting(*_a, **_k):
        calls["n"] += 1
        return {"type": "text", "content": "answer-" + str(calls["n"]),
                "input_tokens": 1, "output_tokens": 1}
    with patch("server.routes.assistant.generate_response", _counting):
        r1 = client.post("/assistant/ask",
                         json={"section": "sites", "message": "одинаковый вопрос"},
                         headers=assistant_headers)
        n_after_first = calls["n"]
        r2 = client.post("/assistant/ask",
                         json={"section": "sites", "message": "одинаковый вопрос"},
                         headers=assistant_headers)
    assert r1.status_code == r2.status_code == 200
    # Второй вызов не должен увеличить count — попал в кэш.
    assert calls["n"] == n_after_first, "повторный вопрос должен идти из кэша"
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
