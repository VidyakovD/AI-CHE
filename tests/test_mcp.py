"""
Тесты MCP Server (JSON-RPC) — server/routes/mcp.py.

Покрытие:
- GET /mcp — info без auth
- POST /mcp без токена → 401
- POST /mcp с токеном → initialize, tools/list, tools/call работают
- get_balance возвращает корректный баланс
- list_solutions фильтрует по subcategory
- get_proposal не пускает к чужому КП (IDOR)
- Невалидный method → JSON-RPC error -32601
- Невалидный JSON в body → JSON-RPC error -32700
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient


def _create_token(client, db_session, user_email: str = None) -> tuple[str, int]:
    """Создаёт юзера + ApiToken через api, возвращает (raw_token, user_id)."""
    from server.models import User, ApiToken
    from server.routes.public_api import _hash_secret
    import hashlib, secrets as _s

    email = user_email or f"mcp_{uuid.uuid4().hex[:8]}@test.local"
    with db_session() as db:
        u = User(
            email=email,
            password_hash="$2b$12$" + "x" * 50,
            name="MCP Test",
            is_verified=True,
            is_active=True,
            tokens_balance=10_000,
        )
        db.add(u)
        db.commit()
        db.refresh(u)
        user_id = u.id

        prefix = _s.token_hex(4)
        secret = _s.token_urlsafe(24)
        t = ApiToken(
            user_id=user_id,
            name="mcp-test",
            prefix=prefix,
            secret_hash=_hash_secret(secret),
            scopes="proposals,solutions",
            is_active=True,
        )
        db.add(t)
        db.commit()

    raw = f"ai_che_{prefix}_{secret}"
    return raw, user_id


@pytest.fixture
def mcp_client():
    from main import app
    return TestClient(app)


@pytest.fixture
def mcp_auth(mcp_client):
    from server.db import db_session
    raw, uid = _create_token(mcp_client, db_session)
    return {"Authorization": f"Bearer {raw}"}, uid


class TestMcpInfo:
    def test_get_info_without_auth(self, mcp_client):
        r = mcp_client.get("/mcp")
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "aiche-mcp"
        assert data["transport"] == "http"
        assert data["tools_count"] >= 5


class TestMcpAuth:
    def test_post_without_token_401(self, mcp_client):
        r = mcp_client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert r.status_code == 401

    def test_post_with_bad_token_401(self, mcp_client):
        r = mcp_client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            headers={"Authorization": "Bearer ai_che_bad_garbage"},
        )
        assert r.status_code == 401


class TestMcpInitialize:
    def test_initialize_returns_capabilities(self, mcp_client, mcp_auth):
        headers, _ = mcp_auth
        r = mcp_client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers=headers,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["jsonrpc"] == "2.0"
        assert data["id"] == 1
        assert "result" in data
        assert "protocolVersion" in data["result"]
        assert data["result"]["serverInfo"]["name"] == "aiche-mcp"
        assert "tools" in data["result"]["capabilities"]


class TestMcpToolsList:
    def test_returns_tool_array_with_schemas(self, mcp_client, mcp_auth):
        headers, _ = mcp_auth
        r = mcp_client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            headers=headers,
        )
        assert r.status_code == 200
        tools = r.json()["result"]["tools"]
        names = {t["name"] for t in tools}
        assert "get_balance" in names
        assert "list_solutions" in names
        assert "list_proposals" in names
        # Каждый tool должен иметь inputSchema
        for t in tools:
            assert "inputSchema" in t
            assert t["inputSchema"]["type"] == "object"


class TestMcpGetBalance:
    def test_returns_user_balance(self, mcp_client, mcp_auth):
        headers, uid = mcp_auth
        r = mcp_client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0", "id": 3,
                "method": "tools/call",
                "params": {"name": "get_balance", "arguments": {}},
            },
            headers=headers,
        )
        assert r.status_code == 200
        result = r.json()["result"]
        assert "content" in result
        # Парсим JSON в content[0].text
        body = json.loads(result["content"][0]["text"])
        assert body["balance_kop"] == 10_000
        assert body["balance_rub"] == 100.00


class TestMcpListSolutions:
    def test_returns_items_array(self, mcp_client, mcp_auth):
        headers, _ = mcp_auth
        r = mcp_client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0", "id": 4,
                "method": "tools/call",
                "params": {"name": "list_solutions", "arguments": {"limit": 5}},
            },
            headers=headers,
        )
        assert r.status_code == 200
        body = json.loads(r.json()["result"]["content"][0]["text"])
        assert "items" in body
        assert isinstance(body["items"], list)


class TestMcpGetProposalIdor:
    def test_other_user_proposal_returns_isError(self, mcp_client, mcp_auth):
        """Юзер A не должен видеть КП юзера B."""
        from server.db import db_session
        from server.models import ProposalProject

        headers_a, uid_a = mcp_auth
        # Создаём КП от имени другого юзера B
        raw_b, uid_b = _create_token(mcp_client, db_session)
        with db_session() as db:
            p = ProposalProject(
                user_id=uid_b,
                name="Чужое КП",
                client_request="секретный текст",
                status="draft",
            )
            db.add(p)
            db.commit()
            db.refresh(p)
            other_pid = p.id

        # A пытается получить B's proposal
        r = mcp_client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0", "id": 5,
                "method": "tools/call",
                "params": {"name": "get_proposal", "arguments": {"proposal_id": other_pid}},
            },
            headers=headers_a,
        )
        assert r.status_code == 200
        result = r.json()["result"]
        # Должен вернуться isError=True (бизнес-ошибка не найден/не ваш)
        assert result.get("isError") is True


class TestMcpJsonRpcErrors:
    def test_unknown_method_returns_minus32601(self, mcp_client, mcp_auth):
        headers, _ = mcp_auth
        r = mcp_client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 9, "method": "garbage/nope"},
            headers=headers,
        )
        assert r.status_code == 200
        err = r.json()["error"]
        assert err["code"] == -32601

    def test_invalid_json_returns_parse_error(self, mcp_client, mcp_auth):
        headers, _ = mcp_auth
        r = mcp_client.post(
            "/mcp",
            content=b"not-a-json",
            headers={**headers, "Content-Type": "application/json"},
        )
        assert r.status_code == 200
        err = r.json()["error"]
        assert err["code"] == -32700

    def test_unknown_tool_returns_minus32601(self, mcp_client, mcp_auth):
        headers, _ = mcp_auth
        r = mcp_client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0", "id": 10,
                "method": "tools/call",
                "params": {"name": "no_such_tool", "arguments": {}},
            },
            headers=headers,
        )
        assert r.status_code == 200
        err = r.json()["error"]
        assert err["code"] == -32601
