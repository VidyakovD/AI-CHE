"""Тесты для server/crm.py — CRM-dispatcher (Bitrix24 / amoCRM / webhook).

Покрывают:
- _build_payload: правильный mapping для каждого provider'а
- _post_to_crm: dispatch-time SSRF-чек, atomic UPDATE статуса, auto-disable
- dispatch_record_to_crm: fan-out по active connections юзера

Раньше CRM-модуль был полностью без тестов (208 строк критичного кода).
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import patch
from server.db import SessionLocal


# ════════════════════════════════════════════════════════════════════════════
# _build_payload — маппинг полей под каждую CRM
# ════════════════════════════════════════════════════════════════════════════

class TestBuildPayload:
    """Проверяет что наши record-поля попадают в правильный формат конкретной
    CRM-системы. Bitrix24 требует особого формата для PHONE/EMAIL (массив
    объектов) и обёртки {fields: ...}."""

    def test_bitrix24_phone_email_as_array(self):
        from server.crm import _build_payload
        record = {
            "customer_name": "Иван",
            "customer_phone": "+79991234567",
            "customer_email": "ivan@example.com",
            "comment": "тест",
        }
        out = _build_payload("bitrix24", record, None)
        # Bitrix24 ждёт {"fields": {...}}
        assert "fields" in out
        fields = out["fields"]
        # PHONE/EMAIL — массив объектов
        assert fields["PHONE"] == [{"VALUE": "+79991234567", "VALUE_TYPE": "WORK"}]
        assert fields["EMAIL"] == [{"VALUE": "ivan@example.com", "VALUE_TYPE": "WORK"}]
        # TITLE из customer_name
        assert fields["TITLE"] == "Иван"
        assert fields["COMMENTS"] == "тест"

    def test_amocrm_flat_payload(self):
        from server.crm import _build_payload
        record = {"customer_name": "Иван", "customer_phone": "+79991234567"}
        out = _build_payload("amocrm", record, None)
        # amoCRM не оборачивает в {fields}
        assert "fields" not in out
        assert out["name"] == "Иван"
        assert out["PHONE"] == "+79991234567"  # для amoCRM PHONE — строка

    def test_generic_passthrough(self):
        from server.crm import _build_payload
        record = {
            "customer_name": "Иван",
            "customer_phone": "+79991234567",
            "comment": "тест",
            "platform": "tg",
        }
        out = _build_payload("webhook", record, None)
        # Generic — наши имена 1:1
        assert out["customer_name"] == "Иван"
        assert out["customer_phone"] == "+79991234567"
        assert out["comment"] == "тест"
        assert out["platform"] == "tg"

    def test_empty_fields_skipped(self):
        from server.crm import _build_payload
        record = {"customer_name": "Иван", "customer_phone": "", "comment": None}
        out = _build_payload("webhook", record, None)
        assert out["customer_name"] == "Иван"
        assert "customer_phone" not in out
        assert "comment" not in out

    def test_custom_mapping_overrides_defaults(self):
        from server.crm import _build_payload
        record = {"customer_name": "Иван", "customer_email": "ivan@example.com"}
        custom_mapping = {
            "customer_name": "FullName",
            "customer_email": "EmailAddr",
        }
        out = _build_payload("webhook", record, custom_mapping)
        assert out["FullName"] == "Иван"
        assert out["EmailAddr"] == "ivan@example.com"

    def test_unknown_provider_falls_back_to_generic(self):
        from server.crm import _build_payload
        record = {"customer_name": "Иван"}
        out = _build_payload("unknown-crm", record, None)
        # Generic mapping (как webhook)
        assert out.get("customer_name") == "Иван"
        assert "fields" not in out


# ════════════════════════════════════════════════════════════════════════════
# _post_to_crm — dispatch-time SSRF reject + atomic update
# ════════════════════════════════════════════════════════════════════════════

def _user_for_crm(db, email):
    """Создаёт юзера для тестов CRM."""
    from server.models import User
    import uuid as _uuid
    u = User(
        email=email,
        password_hash="$2b$12$abcdefghijklmnopqrstuvCxyz0123456789ABCDEFGHIJKLMNOPQRSTU",
        tokens_balance=10000, is_verified=True, agreed_to_terms=True,
        referral_code=_uuid.uuid4().hex[:8].upper(),
    )
    db.add(u); db.commit(); db.refresh(u)
    return u.id


class TestPostToCrm:
    """dispatch_outbound делает re-валидацию URL → если private/loopback →
    моментально is_active=False. Без накручивания fail_count."""

    def test_private_url_immediately_disables(self):
        from server.models import CrmConnection
        from server.crm import _post_to_crm
        import uuid as _uuid
        with SessionLocal() as db:
            uid = _user_for_crm(db, f"crm-priv-{_uuid.uuid4().hex[:6]}@t.local")
            conn = CrmConnection(
                user_id=uid, provider="webhook",
                webhook_url="http://10.0.0.1/crm",  # private, пройдёт INSERT
                name="test", is_active=True,
                fail_count=0, total_calls=0,
            )
            db.add(conn); db.commit(); db.refresh(conn)
            cid = conn.id

        result = _post_to_crm(cid, {"customer_name": "Test"})
        assert result["delivered"] is False
        assert "URL rejected" in (result["error"] or "")

        with SessionLocal() as db:
            c2 = db.query(CrmConnection).filter_by(id=cid).first()
            assert c2.is_active is False

    def test_fail_counter_increments_atomically(self):
        """С public URL который не отвечает → fail_count растёт через UPDATE."""
        from server.models import CrmConnection
        from server.crm import _post_to_crm, MAX_FAIL_BEFORE_DISABLE
        import uuid as _uuid

        with SessionLocal() as db:
            uid = _user_for_crm(db, f"crm-fail-{_uuid.uuid4().hex[:6]}@t.local")
            conn = CrmConnection(
                user_id=uid, provider="webhook",
                webhook_url="http://192.0.2.1:1/never",  # public TEST-NET, port 1 закрыт
                name="test", is_active=True,
                fail_count=0, total_calls=0,
            )
            db.add(conn); db.commit(); db.refresh(conn)
            cid = conn.id

        # Bypass URL-валидации (192.0.2.1 — TEST-NET, ipaddress считает её reserved)
        with patch("server.proposal_builder.validate_outbound_url",
                   side_effect=lambda url, **kw: url):
            for _ in range(MAX_FAIL_BEFORE_DISABLE):
                _post_to_crm(cid, {"customer_name": "Test"})

        with SessionLocal() as db:
            c2 = db.query(CrmConnection).filter_by(id=cid).first()
            assert c2.fail_count >= MAX_FAIL_BEFORE_DISABLE
            assert c2.is_active is False  # auto-disable

    def test_missing_connection_returns_error(self):
        from server.crm import _post_to_crm
        result = _post_to_crm(999_999_999, {"customer_name": "Test"})
        assert result["delivered"] is False
        assert "connection gone" in (result["error"] or "")


# ════════════════════════════════════════════════════════════════════════════
# dispatch_record_to_crm — fan-out по active connections
# ════════════════════════════════════════════════════════════════════════════

class TestDispatchRecord:
    """Должен найти ВСЕ active connections юзера и стартануть thread на каждую."""

    def test_no_user_id_returns_zero(self):
        from server.crm import dispatch_record_to_crm
        assert dispatch_record_to_crm(0, {"x": 1}) == 0
        assert dispatch_record_to_crm(None, {"x": 1}) == 0

    def test_invalid_record_returns_zero(self):
        from server.crm import dispatch_record_to_crm
        assert dispatch_record_to_crm(1, "not a dict") == 0
        assert dispatch_record_to_crm(1, None) == 0

    def test_disabled_connections_skipped(self):
        from server.models import CrmConnection
        from server.crm import dispatch_record_to_crm
        import uuid as _uuid
        with SessionLocal() as db:
            uid = _user_for_crm(db, f"crm-disp-{_uuid.uuid4().hex[:6]}@t.local")
            db.add(CrmConnection(
                user_id=uid, provider="webhook",
                webhook_url="https://example.com/active1",
                name="active", is_active=True,
            ))
            db.add(CrmConnection(
                user_id=uid, provider="webhook",
                webhook_url="https://example.com/disabled",
                name="disabled", is_active=False,
            ))
            db.commit()

        # Mock _post_to_crm чтобы не делать реальные сетевые запросы
        with patch("server.crm._post_to_crm") as mock_post:
            n = dispatch_record_to_crm(uid, {"customer_name": "Test"})
            # Один active connection → один dispatch (через thread)
            assert n == 1
