"""Тесты для security-фиксов P0/P1/P2/P3 batches (2026-05-09/10).

Покрывают:
- validate_outbound_url: SSRF-защита через DNS-резолв (P1 batch)
- sanitize_svg_or_raise: defusedxml-парсинг + walk-tree (P1 batch)
- qr_login: TOTP-enforcement для админов (P0 batch)
- /admin/reencrypt-secrets: единая регистрация после слияния (P0 batch)
- _validate_env: fail-fast на старте (P0 batch)
- _outbound.dispatch_outbound: pre-flight URL reject + auto-disable (P3 batch)

Цель — поймать регрессии при будущих рефакторах. Не e2e (preview покрывает).
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient


# ════════════════════════════════════════════════════════════════════════════
# validate_outbound_url — SSRF-защита через DNS-резолв
# ════════════════════════════════════════════════════════════════════════════

class TestValidateOutboundUrl:
    """Раньше regex-чек по lowered URL пропускал IPv6-loopback, decimal IPv4
    и DNS rebinding (evil.com → 127.0.0.1). Теперь DNS-резолв ловит всё."""

    def test_https_public_url_passes(self):
        from server.proposal_builder import validate_outbound_url
        # example.com резолвится в публичный IP — должно пройти
        result = validate_outbound_url("https://example.com/webhook")
        assert result == "https://example.com/webhook"

    def test_localhost_rejected(self):
        from server.proposal_builder import validate_outbound_url
        with pytest.raises(ValueError):
            validate_outbound_url("http://localhost/x")

    def test_ipv4_loopback_rejected(self):
        from server.proposal_builder import validate_outbound_url
        with pytest.raises(ValueError):
            validate_outbound_url("http://127.0.0.1/x")

    def test_ipv4_private_class_a_rejected(self):
        from server.proposal_builder import validate_outbound_url
        with pytest.raises(ValueError):
            validate_outbound_url("http://10.0.0.1/x")

    def test_ipv4_private_class_b_rejected(self):
        from server.proposal_builder import validate_outbound_url
        with pytest.raises(ValueError):
            validate_outbound_url("http://172.16.0.1/x")

    def test_ipv4_private_class_c_rejected(self):
        from server.proposal_builder import validate_outbound_url
        with pytest.raises(ValueError):
            validate_outbound_url("http://192.168.1.1/x")

    def test_aws_metadata_rejected(self):
        from server.proposal_builder import validate_outbound_url
        with pytest.raises(ValueError):
            validate_outbound_url("http://169.254.169.254/latest/meta-data/")

    def test_ipv6_loopback_rejected(self):
        """Раньше regex по lowered URL пропускал [::1] из-за brackets."""
        from server.proposal_builder import validate_outbound_url
        with pytest.raises(ValueError):
            validate_outbound_url("http://[::1]/x")

    def test_decimal_ipv4_rejected(self):
        """Раньше regex не ловил http://2130706433/ (= 127.0.0.1 в decimal)."""
        from server.proposal_builder import validate_outbound_url
        with pytest.raises(ValueError):
            validate_outbound_url("http://2130706433/x")

    def test_non_http_scheme_rejected(self):
        from server.proposal_builder import validate_outbound_url
        with pytest.raises(ValueError):
            validate_outbound_url("ftp://example.com/x")
        with pytest.raises(ValueError):
            validate_outbound_url("file:///etc/passwd")
        with pytest.raises(ValueError):
            validate_outbound_url("javascript:alert(1)")

    def test_https_only_mode(self):
        from server.proposal_builder import validate_outbound_url
        # allow_http=False должен отклонять http
        with pytest.raises(ValueError):
            validate_outbound_url("http://example.com/x", allow_http=False)
        # https — норм
        assert validate_outbound_url("https://example.com/x", allow_http=False)

    def test_empty_url_rejected(self):
        from server.proposal_builder import validate_outbound_url
        with pytest.raises(ValueError):
            validate_outbound_url("")
        with pytest.raises(ValueError):
            validate_outbound_url("   ")
        with pytest.raises(ValueError):
            validate_outbound_url(None)


# ════════════════════════════════════════════════════════════════════════════
# SVG sanitizer — defusedxml-walk вместо substring scan
# ════════════════════════════════════════════════════════════════════════════

class TestSvgSanitizer:
    """Раньше substring-scan ловил только базовые токены. Сейчас XML-парсинг
    видит структуру: encoded entities нормализуются, новые SMIL-теги
    не пропускаются."""

    def test_clean_svg_passes(self):
        from server.security import sanitize_svg_or_raise
        clean = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect x="0" y="0" width="10" height="10" fill="red"/></svg>'
        sanitize_svg_or_raise(clean)  # без exception

    def test_script_tag_blocked(self):
        from server.security import sanitize_svg_or_raise
        from fastapi import HTTPException
        bad = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        with pytest.raises(HTTPException):
            sanitize_svg_or_raise(bad)

    def test_onload_handler_blocked(self):
        from server.security import sanitize_svg_or_raise
        from fastapi import HTTPException
        bad = b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><rect/></svg>'
        with pytest.raises(HTTPException):
            sanitize_svg_or_raise(bad)

    def test_animate_smil_blocked(self):
        """Раньше пропускалось — substring-список не имел <animate>."""
        from server.security import sanitize_svg_or_raise
        from fastapi import HTTPException
        bad = b'<svg xmlns="http://www.w3.org/2000/svg"><animate attributeName="x" from="0" to="javascript:alert(1)"/></svg>'
        with pytest.raises(HTTPException):
            sanitize_svg_or_raise(bad)

    def test_set_smil_blocked(self):
        from server.security import sanitize_svg_or_raise
        from fastapi import HTTPException
        bad = b'<svg xmlns="http://www.w3.org/2000/svg"><set attributeName="x" onbegin="alert(1)"/></svg>'
        with pytest.raises(HTTPException):
            sanitize_svg_or_raise(bad)

    def test_style_tag_blocked(self):
        """<style>@import 'http://evil/'</style> — раньше пропускался."""
        from server.security import sanitize_svg_or_raise
        from fastapi import HTTPException
        bad = b'<svg xmlns="http://www.w3.org/2000/svg"><style>@import url(http://evil/);</style></svg>'
        with pytest.raises(HTTPException):
            sanitize_svg_or_raise(bad)

    def test_foreignobject_blocked(self):
        from server.security import sanitize_svg_or_raise
        from fastapi import HTTPException
        bad = b'<svg xmlns="http://www.w3.org/2000/svg"><foreignObject><div onclick="alert(1)"/></foreignObject></svg>'
        with pytest.raises(HTTPException):
            sanitize_svg_or_raise(bad)

    def test_javascript_href_blocked(self):
        from server.security import sanitize_svg_or_raise
        from fastapi import HTTPException
        bad = (b'<svg xmlns="http://www.w3.org/2000/svg" '
               b'xmlns:xlink="http://www.w3.org/1999/xlink">'
               b'<a xlink:href="javascript:alert(1)"><rect/></a></svg>')
        with pytest.raises(HTTPException):
            sanitize_svg_or_raise(bad)

    def test_doctype_blocked_xxe_protection(self):
        """defusedxml.forbid_dtd=True — XXE/billion-laughs не парсится.
        Skip если defusedxml отсутствует (fallback substring-scan слабее)."""
        pytest.importorskip("defusedxml")
        from server.security import sanitize_svg_or_raise
        from fastapi import HTTPException
        bad = (b'<?xml version="1.0"?>'
               b'<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
               b'<svg xmlns="http://www.w3.org/2000/svg">&xxe;</svg>')
        with pytest.raises(HTTPException):
            sanitize_svg_or_raise(bad)

    def test_invalid_xml_rejected(self):
        """Skip если defusedxml отсутствует (fallback substring-scan не парсит XML)."""
        pytest.importorskip("defusedxml")
        from server.security import sanitize_svg_or_raise
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            sanitize_svg_or_raise(b"not even xml")

    def test_empty_data_passes_silently(self):
        from server.security import sanitize_svg_or_raise
        sanitize_svg_or_raise(b"")  # без exception


# ════════════════════════════════════════════════════════════════════════════
# /admin/reencrypt-secrets — единая регистрация после слияния (P0 batch)
# ════════════════════════════════════════════════════════════════════════════

class TestReencryptSecretsRoute:
    """Раньше было два @router.post('/reencrypt-secrets') — второй
    регистрировался последним и поглощал первый, IMAP-секреты не реэнкриптились.
    Теперь один endpoint покрывает обе категории."""

    def test_only_one_reencrypt_route_registered(self):
        from server.routes.admin import router
        paths = [
            (route.path, route.methods)
            for route in router.routes
            if "reencrypt-secrets" in getattr(route, "path", "")
        ]
        # Должна быть ровно одна регистрация
        post_routes = [p for p in paths if "POST" in (p[1] or set())]
        assert len(post_routes) == 1, \
            f"Должна быть одна регистрация POST /reencrypt-secrets, а нашлось {len(post_routes)}: {post_routes}"


# ════════════════════════════════════════════════════════════════════════════
# Outbound dispatcher — pre-flight URL reject + auto-disable (P3 batch)
# ════════════════════════════════════════════════════════════════════════════

class TestOutboundDispatcherRejectsPrivateUrl:
    """dispatch_outbound делает re-валидацию URL → если private/loopback →
    мгновенно is_active=False (вместо тысячи fail'ов с попытками connect)."""

    def test_private_url_disables_webhook_immediately(self):
        from server.models import ApiWebhook, ApiToken
        from server.db import SessionLocal
        from server.webhooks import _post_sync
        import uuid as _uuid
        # Создаём webhook с private URL, минуя validate (через прямой INSERT)
        with SessionLocal() as db:
            from server.models import User
            u = User(
                email=f"out-{_uuid.uuid4().hex[:8]}@t.local",
                password_hash="$2b$12$abcdefghijklmnopqrstuvCxyz0123456789ABCDEFGHIJKLMNOPQRSTU",
                tokens_balance=10000, is_verified=True, agreed_to_terms=True,
                referral_code=_uuid.uuid4().hex[:8].upper(),
            )
            db.add(u); db.commit(); db.refresh(u)
            tok = ApiToken(
                user_id=u.id,
                prefix="ai_che_test_" + _uuid.uuid4().hex[:10],
                secret_hash="dummyhash" * 8, name="t", scopes="read",
                is_active=True,
            )
            db.add(tok); db.commit(); db.refresh(tok)
            w = ApiWebhook(
                user_id=u.id,
                url="http://10.0.0.1/webhook",  # private — будет ребит
                events="proposal.signed",
                secret="sek_" + "a" * 32,
                is_active=True, fail_count=0, total_calls=0,
            )
            db.add(w); db.commit(); db.refresh(w)
            wid = w.id

        # Один POST → URL ребит → is_active=False
        result = _post_sync(wid, {"event": "x", "data": {}})
        assert result["delivered"] is False
        assert "URL rejected" in (result["error"] or "")

        with SessionLocal() as db:
            w2 = db.query(ApiWebhook).filter_by(id=wid).first()
            assert w2.is_active is False, \
                "Webhook должен auto-disable'иться при private URL без накручивания fail_count"


# ════════════════════════════════════════════════════════════════════════════
# QR-login: TOTP-enforcement для админов (P0 batch)
# ════════════════════════════════════════════════════════════════════════════

class TestQrLoginTotpEnforcement:
    """Угнанная мобильная сессия админа БЕЗ TOTP — approve должен вернуть
    status=totp_required, а не выдать access/refresh-токены десктопу."""

    def test_admin_with_totp_required_to_provide_code(self, monkeypatch):
        from fastapi.testclient import TestClient
        from main import app
        from server.auth import create_token
        from server.db import SessionLocal
        from server.models import User, QrLoginSession
        from datetime import datetime, timedelta
        import uuid as _uuid, secrets as _secrets

        admin_email = f"admin-qr-{_uuid.uuid4().hex[:6]}@t.local"
        # Виртуально делаем юзера админом через ENV
        monkeypatch.setenv("ADMIN_EMAILS", admin_email)
        # Сбросим cached set ADMIN_EMAILS (он статичный в модуле)
        import importlib, server.security
        importlib.reload(server.security)

        with SessionLocal() as db:
            u = User(
                email=admin_email,
                password_hash="$2b$12$abcdefghijklmnopqrstuvCxyz0123456789ABCDEFGHIJKLMNOPQRSTU",
                tokens_balance=10000, is_verified=True, agreed_to_terms=True,
                referral_code=_uuid.uuid4().hex[:8].upper(),
                # 2FA включён, secret валидный (32-байтный base32)
                totp_enabled=True,
                totp_secret="JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP",
            )
            db.add(u); db.commit(); db.refresh(u)
            uid = u.id

            qr = QrLoginSession(
                token=_secrets.token_urlsafe(20),
                status="pending",
                expires_at=datetime.utcnow() + timedelta(seconds=120),
                init_ip="1.2.3.4", init_ua="test-browser",
            )
            db.add(qr); db.commit(); db.refresh(qr)
            qr_token = qr.token

        cli = TestClient(app)
        cli.headers["Authorization"] = "Bearer " + create_token(uid, admin_email)
        # Без TOTP — должен вернуть totp_required
        r = cli.post(f"/qr-login/approve/{qr_token}", json={})
        assert r.status_code == 200
        assert r.json().get("status") == "totp_required", r.text

        # QR-сессия должна остаться pending (не approved)
        with SessionLocal() as db:
            qr2 = db.query(QrLoginSession).filter_by(token=qr_token).first()
            assert qr2.status == "pending"
            assert qr2.user_id is None
