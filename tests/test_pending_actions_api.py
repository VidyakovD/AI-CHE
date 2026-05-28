"""Тесты REST-обвязки PendingAgentAction:
  GET    /api/agents/me/actions
  POST   /api/agents/me/actions/{id}/confirm
  POST   /api/agents/me/actions/{id}/cancel

Покрывают: список pending, выполнение через зарегистрированный executor,
отказ повторно выполнить, cross-user изоляцию, отмену.
"""
from __future__ import annotations

import json
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.db import SessionLocal


_FAKE_BCRYPT = "$2b$12$abcdefghijklmnopqrstuvCxyz0123456789ABCDEFGHIJKLMNOPQRSTU"


def _make_user(db, email: str):
    from server.models import User
    u = db.query(User).filter_by(email=email).first()
    if u:
        return u
    u = User(
        email=email, password_hash=_FAKE_BCRYPT, name=email.split("@")[0],
        tokens_balance=0, is_verified=True, agreed_to_terms=True,
        referral_code=uuid.uuid4().hex[:8].upper(),
    )
    db.add(u); db.commit(); db.refresh(u)
    return u


def _make_pending(db, *, user_id, action_type, params, agent_id=None,
                   status="pending"):
    from server.models import PendingAgentAction
    pa = PendingAgentAction(
        user_id=user_id, agent_id=agent_id,
        module_slug=action_type.split("_")[0],
        action_type=action_type,
        params_json=json.dumps(params, ensure_ascii=False),
        preview_text=f"preview {action_type}",
        status=status,
    )
    db.add(pa); db.commit(); db.refresh(pa)
    return pa


def _client_with_token(user_id, email):
    from fastapi.testclient import TestClient
    from server.auth import create_token
    from main import app
    return TestClient(app), create_token(user_id, email)


# Регистрируем тестовый executor чтобы не дёргать настоящий SMTP
def _register_test_executor():
    from server.agent_actions import register_executor

    @register_executor("test_demo_ok")
    def _ok(params, uid):
        return {"ok": True, "result": {"echo": params.get("msg", "")}, "error": None}

    @register_executor("test_demo_fail")
    def _fail(params, uid):
        return {"ok": False, "result": None, "error": "intentional failure"}


_register_test_executor()


class TestListPendingActions:

    def test_returns_only_pending_by_default(self):
        db = SessionLocal()
        try:
            u = _make_user(db, "pa-list-default@test.com")
            _make_pending(db, user_id=u.id, action_type="test_demo_ok",
                          params={"msg": "p1"}, status="pending")
            _make_pending(db, user_id=u.id, action_type="test_demo_ok",
                          params={"msg": "p2"}, status="confirmed")
            user_id = u.id
        finally:
            db.close()

        client, token = _client_with_token(user_id, "pa-list-default@test.com")
        r = client.get("/api/agents/me/actions",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        data = r.json()
        statuses = [a["status"] for a in data["actions"]]
        assert all(s == "pending" for s in statuses), statuses

    def test_status_all_returns_all(self):
        db = SessionLocal()
        try:
            u = _make_user(db, "pa-list-all@test.com")
            _make_pending(db, user_id=u.id, action_type="test_demo_ok",
                          params={"msg": "a"}, status="pending")
            _make_pending(db, user_id=u.id, action_type="test_demo_ok",
                          params={"msg": "b"}, status="cancelled")
            user_id = u.id
        finally:
            db.close()

        client, token = _client_with_token(user_id, "pa-list-all@test.com")
        r = client.get("/api/agents/me/actions?status=all",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        statuses = {a["status"] for a in r.json()["actions"]}
        assert "pending" in statuses
        assert "cancelled" in statuses


class TestConfirmPendingAction:

    def test_confirm_runs_executor_and_marks_done(self):
        db = SessionLocal()
        try:
            u = _make_user(db, "pa-confirm-ok@test.com")
            pa = _make_pending(db, user_id=u.id, action_type="test_demo_ok",
                               params={"msg": "hello"}, status="pending")
            user_id = u.id
            action_id = pa.id
        finally:
            db.close()

        client, token = _client_with_token(user_id, "pa-confirm-ok@test.com")
        r = client.post(f"/api/agents/me/actions/{action_id}/confirm",
                        headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True
        assert data["status"] == "confirmed"
        assert data["result"] == {"echo": "hello"}

        # Проверяем что в БД статус действительно сменился
        from server.models import PendingAgentAction
        db = SessionLocal()
        try:
            row = db.query(PendingAgentAction).get(action_id)
            assert row.status == "confirmed"
            assert row.confirmed_at is not None
            assert row.result_json
            result = json.loads(row.result_json)
            assert result["ok"] is True
        finally:
            db.close()

    def test_confirm_failure_marks_error(self):
        db = SessionLocal()
        try:
            u = _make_user(db, "pa-confirm-fail@test.com")
            pa = _make_pending(db, user_id=u.id, action_type="test_demo_fail",
                               params={}, status="pending")
            user_id = u.id
            action_id = pa.id
        finally:
            db.close()

        client, token = _client_with_token(user_id, "pa-confirm-fail@test.com")
        r = client.post(f"/api/agents/me/actions/{action_id}/confirm",
                        headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is False
        assert data["status"] == "error"
        assert "intentional" in (data.get("error") or "")

    def test_confirm_non_pending_rejected(self):
        db = SessionLocal()
        try:
            u = _make_user(db, "pa-already@test.com")
            pa = _make_pending(db, user_id=u.id, action_type="test_demo_ok",
                               params={"msg": "x"}, status="confirmed")
            user_id = u.id
            action_id = pa.id
        finally:
            db.close()

        client, token = _client_with_token(user_id, "pa-already@test.com")
        r = client.post(f"/api/agents/me/actions/{action_id}/confirm",
                        headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 400
        assert "уже" in (r.json().get("detail") or "").lower()

    def test_confirm_cross_user_denied(self):
        db = SessionLocal()
        try:
            u1 = _make_user(db, "pa-owner@test.com")
            u2 = _make_user(db, "pa-attacker@test.com")
            pa = _make_pending(db, user_id=u1.id, action_type="test_demo_ok",
                               params={"msg": "secret"}, status="pending")
            attacker_id = u2.id
            action_id = pa.id
        finally:
            db.close()

        client, token = _client_with_token(attacker_id, "pa-attacker@test.com")
        r = client.post(f"/api/agents/me/actions/{action_id}/confirm",
                        headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 404

        # Убедимся что в БД действие осталось pending — НЕ выполнилось
        from server.models import PendingAgentAction
        db = SessionLocal()
        try:
            row = db.query(PendingAgentAction).get(action_id)
            assert row.status == "pending"
        finally:
            db.close()


class TestCancelPendingAction:

    def test_cancel_marks_cancelled(self):
        db = SessionLocal()
        try:
            u = _make_user(db, "pa-cancel@test.com")
            pa = _make_pending(db, user_id=u.id, action_type="test_demo_ok",
                               params={"msg": "drop"}, status="pending")
            user_id = u.id
            action_id = pa.id
        finally:
            db.close()

        client, token = _client_with_token(user_id, "pa-cancel@test.com")
        r = client.post(f"/api/agents/me/actions/{action_id}/cancel",
                        headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"

        from server.models import PendingAgentAction
        db = SessionLocal()
        try:
            row = db.query(PendingAgentAction).get(action_id)
            assert row.status == "cancelled"
        finally:
            db.close()

    def test_cancel_already_cancelled_idempotent(self):
        db = SessionLocal()
        try:
            u = _make_user(db, "pa-cancel-twice@test.com")
            pa = _make_pending(db, user_id=u.id, action_type="test_demo_ok",
                               params={}, status="cancelled")
            user_id = u.id
            action_id = pa.id
        finally:
            db.close()

        client, token = _client_with_token(user_id, "pa-cancel-twice@test.com")
        r = client.post(f"/api/agents/me/actions/{action_id}/cancel",
                        headers={"Authorization": f"Bearer {token}"})
        # Не ошибка — просто отдаём текущий статус
        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"
