"""Тесты RAG-базы знаний: чанкер + endpoints + retrieve."""
import os, sys
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.db import Base, engine, SessionLocal
from server.models import User, ChatBot, AgentConfig, KnowledgeFile, KnowledgeChunk
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
def kb_user():
    db = SessionLocal()
    email = "kb_test@test.com"
    u = db.query(User).filter_by(email=email).first()
    if not u:
        u = User(email=email, password_hash=_FAKE_BCRYPT, name="KB Tester",
                tokens_balance=20_000, is_active=True, is_verified=True,
                agreed_to_terms=True, referral_code="KBT00001")
        db.add(u); db.commit(); db.refresh(u)
    db.close()
    return u


@pytest.fixture(scope="module")
def kb_headers(kb_user):
    token = create_token(kb_user.id, kb_user.email)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def kb_agent(kb_user):
    db = SessionLocal()
    a = AgentConfig(user_id=kb_user.id, name="Test Agent")
    db.add(a); db.commit(); db.refresh(a)
    aid = a.id
    db.close()
    return aid


@pytest.fixture(scope="module")
def kb_bot(kb_user):
    db = SessionLocal()
    b = ChatBot(user_id=kb_user.id, name="Test Bot")
    db.add(b); db.commit(); db.refresh(b)
    bid = b.id
    db.close()
    return bid


# ── Чанкер ───────────────────────────────────────────────────────────────

def test_chunk_text_short_returns_one_chunk():
    from server.knowledge import chunk_text
    chunks = chunk_text("Короткий текст.")
    assert len(chunks) == 1


def test_chunk_text_empty_returns_empty():
    from server.knowledge import chunk_text
    assert chunk_text("") == []
    assert chunk_text("   ") == []


def test_chunk_text_long_splits_into_multiple():
    from server.knowledge import chunk_text, CHUNK_TARGET_TOKENS, CHARS_PER_TOKEN
    para = "Тестовое предложение длиной около двадцати символов. " * 200
    chunks = chunk_text(para)
    assert len(chunks) > 1
    # Каждый чанк не должен сильно превышать target
    target_chars = CHUNK_TARGET_TOKENS * CHARS_PER_TOKEN
    for c in chunks:
        assert len(c) < target_chars * 2


def test_chunk_text_preserves_paragraphs():
    from server.knowledge import chunk_text
    text = "Первый абзац о компании ACME.\n\nВторой абзац о продуктах.\n\nТретий о ценах."
    chunks = chunk_text(text)
    joined = "\n\n".join(chunks)
    assert "ACME" in joined
    assert "продуктах" in joined
    assert "ценах" in joined


# ── Cosine ───────────────────────────────────────────────────────────────

def test_cosine_basic():
    from server.knowledge import _cosine
    assert _cosine([1, 0, 0], [1, 0, 0]) == 1.0
    assert _cosine([1, 0, 0], [0, 1, 0]) == 0.0
    assert _cosine([], [1, 2, 3]) == 0.0


# ── Endpoints ────────────────────────────────────────────────────────────

def test_kb_list_requires_auth(client, kb_agent):
    r = client.get(f"/knowledge?owner_type=agent&owner_id={kb_agent}")
    assert r.status_code == 401


def test_kb_list_rejects_unknown_owner(client, kb_headers, kb_agent):
    r = client.get(f"/knowledge?owner_type=agent&owner_id=999999", headers=kb_headers)
    assert r.status_code == 404


def test_kb_list_rejects_invalid_owner_type(client, kb_headers):
    r = client.get(f"/knowledge?owner_type=hacker&owner_id=1", headers=kb_headers)
    assert r.status_code == 400


def test_kb_list_empty_returns_zero_files(client, kb_headers, kb_agent):
    r = client.get(f"/knowledge?owner_type=agent&owner_id={kb_agent}", headers=kb_headers)
    assert r.status_code == 200
    d = r.json()
    assert d["summary"]["count"] == 0


def test_kb_upload_rejects_bad_extension(client, kb_headers, kb_agent):
    r = client.post(f"/knowledge/upload?owner_type=agent&owner_id={kb_agent}",
                    headers=kb_headers,
                    files={"file": ("evil.exe", b"MZ\x00", "application/octet-stream")})
    assert r.status_code == 400
    assert "не поддерживается" in r.json()["detail"].lower()


def test_kb_upload_text_indexes_in_background(client, kb_headers, kb_agent, kb_user):
    """Загружаем .txt → файл создан, BackgroundTasks запустит add_file."""
    content = b"Acme corp founded in 2020. Provides AI consulting services in Moscow."
    fake_emb = [0.1] * 1536
    with patch("server.knowledge._embed_batch", return_value=[fake_emb]):
        r = client.post(f"/knowledge/upload?owner_type=agent&owner_id={kb_agent}",
                        headers=kb_headers,
                        files={"file": ("about.txt", content, "text/plain")})
    assert r.status_code == 200
    # Проверим, что файл попал в БД
    db = SessionLocal()
    try:
        files = (db.query(KnowledgeFile)
                   .filter_by(owner_type="agent", owner_id=kb_agent)
                   .all())
        assert len(files) >= 1
        # Чанки могут быть ещё не созданы (background) — проверяем что хотя бы один file есть
    finally:
        db.close()


# ── retrieve / search ────────────────────────────────────────────────────

def test_retrieve_with_no_files_returns_empty():
    from server.knowledge import retrieve
    fake_emb = [0.1] * 1536
    with patch("server.knowledge._embed_one", return_value=fake_emb):
        results = retrieve(owner_type="agent", owner_id=999_999, query="test", top=5)
    assert results == []


def test_retrieve_finds_relevant_chunk_via_cosine(kb_user):
    """Прямой тест retrieve: создаём файл + чанк с известным embedding."""
    from server.knowledge import retrieve
    # Уникальный owner_id чтобы не конфликтовать со старыми записями
    import secrets as _s
    test_owner_id = 900_000 + (_s.randbits(16))
    db = SessionLocal()
    try:
        kf = KnowledgeFile(user_id=kb_user.id, owner_type="agent", owner_id=test_owner_id,
                           name="cosine.txt", path="/uploads/knowledge/cosine.txt",
                           chunk_count=1, indexing_status="ready")
        db.add(kf); db.commit(); db.refresh(kf)
        import json as _j
        emb_chunk = [1.0] + [0.0] * 1535
        chunk = KnowledgeChunk(kb_file_id=kf.id, chunk_index=0,
                               text="Прайс на услуги: консалтинг 50 000 ₽.",
                               embedding_json=_j.dumps(emb_chunk),
                               token_count=10)
        db.add(chunk); db.commit()
    finally:
        db.close()
    with patch("server.knowledge._embed_one", return_value=[1.0] + [0.0] * 1535):
        results = retrieve(owner_type="agent", owner_id=test_owner_id, query="сколько стоит", top=3)
    assert len(results) >= 1
    assert any("консалтинг" in r["text"] for r in results)
    assert results[0]["score"] > 0.9


def test_build_context_block_truncates():
    from server.knowledge import build_context_block
    results = [
        {"file_name": "a.txt", "chunk_index": 0, "text": "Текст " * 200},
        {"file_name": "b.txt", "chunk_index": 0, "text": "Большой текст " * 500},
    ]
    block = build_context_block(results, max_chars=500)
    assert len(block) <= 800  # с учётом обрамляющего текста
    assert "БАЗА ЗНАНИЙ" in block


# ── Bot vs Agent isolation ───────────────────────────────────────────────

def test_owner_isolation_bot_not_seen_as_agent(client, kb_headers, kb_bot, kb_agent):
    """Файл загружен боту — не должен быть виден агенту."""
    db = SessionLocal()
    try:
        kf = KnowledgeFile(owner_type="bot", owner_id=kb_bot,
                           name="bot-only.txt", path="/uploads/knowledge/bot.txt",
                           chunk_count=1, indexing_status="ready")
        db.add(kf); db.commit()
    finally:
        db.close()
    # Список для агента не должен содержать файл бота
    r_a = client.get(f"/knowledge?owner_type=agent&owner_id={kb_agent}", headers=kb_headers)
    names_a = [f["name"] for f in r_a.json()["files"]]
    assert "bot-only.txt" not in names_a
    # Зато для бота должен быть виден
    r_b = client.get(f"/knowledge?owner_type=bot&owner_id={kb_bot}", headers=kb_headers)
    names_b = [f["name"] for f in r_b.json()["files"]]
    assert "bot-only.txt" in names_b
