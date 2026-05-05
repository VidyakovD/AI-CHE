"""Тесты для модулей добавленных в последних спринтах:
- ProposalSignature (электронная подпись КП)
- ApiWebhook (Public API webhooks)
- OrchestraSchedule (cron-расписания)
- 2FA админки (TOTP)
- TTS endpoint
- IdempotencyRecord (multi-worker safety для /message)

Все тесты используют общую TestClient + auth-helper. Цель — поймать
регрессии при рефакторе, не покрытии 100% (preview e2e уже сделан).
"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import secrets as _secrets
import pytest
from fastapi.testclient import TestClient

from server.db import SessionLocal


_FAKE_BCRYPT = "$2b$12$abcdefghijklmnopqrstuvCxyz0123456789ABCDEFGHIJKLMNOPQRSTU"


def _user(db, email, balance=10000, is_admin=False):
    """Создать тестового юзера. balance в копейках. Возвращает (id, email)
    как кортеж — чтобы избежать DetachedInstanceError при использовании
    user-объекта после закрытия сессии."""
    from server.models import User
    import uuid
    u = db.query(User).filter_by(email=email).first()
    if u:
        u.tokens_balance = balance
        u.is_verified = True
        db.commit()
        return (u.id, u.email)
    u = User(
        email=email,
        password_hash=_FAKE_BCRYPT,
        name=email.split("@")[0],
        tokens_balance=balance,
        is_verified=True,
        agreed_to_terms=True,
        referral_code=uuid.uuid4().hex[:8].upper(),
    )
    db.add(u); db.commit(); db.refresh(u)
    return (u.id, u.email)


def _client_for(user_tuple):
    """TestClient + Authorization-header через JWT.
    user_tuple = (id, email) от _user()."""
    from main import app
    from server.auth import create_token
    uid, uemail = user_tuple
    cli = TestClient(app)
    cli.headers["Authorization"] = "Bearer " + create_token(uid, uemail)
    return cli


# ════════════════════════════════════════════════════════════════════════════
# ProposalSignature — электронная подпись КП
# ════════════════════════════════════════════════════════════════════════════

class TestProposalSignature:

    def _make_proposal(self, user_id):
        """Создаёт КП с public_token + фейковый PDF на диске."""
        from server.models import ProposalProject
        import os.path as _op
        token = _secrets.token_urlsafe(20)
        # PDF на диске чтобы /p/{token}/pdf не упал
        proj_root = _op.dirname(_op.dirname(_op.abspath(__file__)))
        pdf_dir = _op.join(proj_root, "uploads", "proposals")
        os.makedirs(pdf_dir, exist_ok=True)
        pdf_path = _op.join(pdf_dir, f"test_{token[:8]}.pdf")
        with open(pdf_path, "wb") as f:
            f.write(b"%PDF-1.4\nfake")
        with SessionLocal() as db:
            p = ProposalProject(
                user_id=user_id, name="Test KP", status="done",
                public_token=token,
                generated_pdf=f"/uploads/proposals/test_{token[:8]}.pdf",
            )
            db.add(p); db.commit(); db.refresh(p)
            return p.id, token

    def test_public_page_renders_with_canvas(self):
        from main import app
        with SessionLocal() as db:
            uid, uemail = _user(db, "sig-test-1@example.com")
            pid, token = self._make_proposal(uid)
        client = TestClient(app)
        r = client.get(f"/p/{token}")
        assert r.status_code == 200
        # HTML должен содержать canvas-форму
        assert "sigCanvas" in r.text
        assert "signerName" in r.text

    def test_sign_validation(self):
        from main import app
        with SessionLocal() as db:
            uid, uemail = _user(db, "sig-test-2@example.com")
            pid, token = self._make_proposal(uid)
        client = TestClient(app)
        # Слишком короткое имя
        r = client.post(f"/p/{token}/sign", json={
            "signer_name": "X",
            "signature_data": "data:image/png;base64,iVBORw0KGgo" + "A" * 200,
        })
        assert r.status_code == 400
        # Без подписи
        r = client.post(f"/p/{token}/sign", json={
            "signer_name": "Иван Иванов",
            "signature_data": "",
        })
        assert r.status_code == 400

    def test_sign_idempotency(self):
        """Повторное подписание → 409."""
        from main import app
        with SessionLocal() as db:
            uid, uemail = _user(db, "sig-test-3@example.com")
            pid, token = self._make_proposal(uid)
        client = TestClient(app)
        sig = "data:image/png;base64,iVBORw0KGgo" + "A" * 300
        r1 = client.post(f"/p/{token}/sign", json={
            "signer_name": "Иван Иванов",
            "signature_data": sig,
        })
        assert r1.status_code == 200
        assert r1.json()["status"] == "signed"
        # Повтор — 409
        r2 = client.post(f"/p/{token}/sign", json={
            "signer_name": "Пётр Петров",
            "signature_data": sig,
        })
        assert r2.status_code == 409


# ════════════════════════════════════════════════════════════════════════════
# ApiWebhook — Public API webhooks
# ════════════════════════════════════════════════════════════════════════════

class TestApiWebhook:

    def test_create_returns_secret_once(self):
        with SessionLocal() as db:
            uid, uemail = _user(db, "wh-test@example.com")
        client = _client_for((uid, uemail))
        r = client.post("/api-tokens/webhooks", json={
            "url": "https://example.com/hook",
            "events": ["proposal.opened", "record.created"],
            "description": "test",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["id"] > 0
        assert data["secret"]  # 32 hex
        assert len(data["secret"]) == 32
        # GET не возвращает secret
        list_r = client.get("/api-tokens/webhooks")
        assert list_r.status_code == 200
        items = list_r.json()
        assert len(items) >= 1
        assert "secret" not in items[0]

    def test_invalid_url_rejected(self):
        with SessionLocal() as db:
            uid, uemail = _user(db, "wh-test-url@example.com")
        client = _client_for((uid, uemail))
        # SSRF: localhost блокируется
        r = client.post("/api-tokens/webhooks", json={
            "url": "http://127.0.0.1/hook",
            "events": ["proposal.opened"],
        })
        assert r.status_code == 400
        # http://10.x.x.x — private network
        r = client.post("/api-tokens/webhooks", json={
            "url": "http://10.0.0.1/hook",
            "events": ["proposal.opened"],
        })
        assert r.status_code == 400
        # ftp:// — не http
        r = client.post("/api-tokens/webhooks", json={
            "url": "ftp://example.com/hook",
            "events": ["proposal.opened"],
        })
        assert r.status_code == 400

    def test_unknown_events_rejected(self):
        with SessionLocal() as db:
            uid, uemail = _user(db, "wh-test-evt@example.com")
        client = _client_for((uid, uemail))
        r = client.post("/api-tokens/webhooks", json={
            "url": "https://example.com/hook",
            "events": ["fake.event", "another.bogus"],
        })
        assert r.status_code == 400


# ════════════════════════════════════════════════════════════════════════════
# OrchestraSchedule — cron-расписания
# ════════════════════════════════════════════════════════════════════════════

class TestOrchestraSchedule:

    def _make_orchestra_solution(self, db):
        from server.models import Solution, SolutionCategory
        cat = SolutionCategory(slug=f"test_{_secrets.token_hex(4)}", title="Test")
        db.add(cat); db.flush()
        sol = Solution(
            title="Test Orchestra", description="Test",
            price_tokens=10000, is_active=True, category_id=cat.id,
            orchestra_json=json.dumps({"stages": []}),
        )
        db.add(sol); db.commit(); db.refresh(sol)
        return sol.id

    def test_create_with_valid_frequency(self):
        from main import app
        from server.auth import create_token
        from server.models import OrchestraSchedule
        with SessionLocal() as db:
            uid, uemail = _user(db, "sched-test@example.com")
            # Чистим расписания предыдущих прогонов (лимит 5)
            db.query(OrchestraSchedule).filter_by(user_id=uid).delete()
            db.commit()
            sid = self._make_orchestra_solution(db)
        cli = TestClient(app)
        cli.headers["Authorization"] = "Bearer " + create_token(uid, uemail)
        r = cli.post("/orchestra-schedules", json={
            "solution_id": sid,
            "user_input": "тестовый запрос для расписания",
            "frequency": "weekly_mon_09",
            "name": "Test schedule",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["next_run_at"]
        # next_run_at должен быть в будущем
        from datetime import datetime
        nr = datetime.fromisoformat(data["next_run_at"].replace("Z", ""))
        assert nr > datetime.utcnow()

    def test_invalid_frequency_rejected(self):
        from main import app
        from server.auth import create_token
        with SessionLocal() as db:
            uid, uemail = _user(db, "sched-test-f@example.com")
            uid, uemail = uid, uemail
            sid = self._make_orchestra_solution(db)
        cli = TestClient(app)
        cli.headers["Authorization"] = "Bearer " + create_token(uid, uemail)
        r = cli.post("/orchestra-schedules", json={
            "solution_id": sid,
            "user_input": "тестовый запрос",
            "frequency": "every_5_minutes",
        })
        assert r.status_code == 400

    def test_calc_next_run_weekly(self):
        """Проверка _calc_next_run для weekly-варианта."""
        from server.routes.schedules import _calc_next_run
        from datetime import datetime
        # Понедельник 09:00 → next must be next Monday 09:00 UTC
        nr = _calc_next_run("weekly_mon_09")
        assert nr is not None
        assert nr.weekday() == 0  # Monday
        assert nr.hour == 9


# ════════════════════════════════════════════════════════════════════════════
# 2FA админки (TOTP)
# ════════════════════════════════════════════════════════════════════════════

class TestAdmin2FA:

    def test_setup_returns_qr_and_secret(self):
        # Делаем юзера админом через ADMIN_EMAILS env
        os.environ["ADMIN_EMAILS"] = "totp-admin@example.com"
        # Reload security module чтобы подхватить новый email
        from server import security as _sec
        _sec.ADMIN_EMAILS = {"totp-admin@example.com"}
        with SessionLocal() as db:
            uid, uemail = _user(db, "totp-admin@example.com")
        client = _client_for((uid, uemail))
        r = client.post("/admin/2fa/setup")
        assert r.status_code == 200
        data = r.json()
        assert data["secret"]
        assert len(data["secret"]) == 32  # base32 32-char
        assert data["qr_data_url"].startswith("data:image/png;base64,")

    def test_enable_with_correct_code(self):
        os.environ["ADMIN_EMAILS"] = "totp-en@example.com"
        from server import security as _sec
        _sec.ADMIN_EMAILS = {"totp-en@example.com"}
        with SessionLocal() as db:
            uid, uemail = _user(db, "totp-en@example.com")
        client = _client_for((uid, uemail))
        # Setup
        setup = client.post("/admin/2fa/setup").json()
        # Сгенерим валидный код
        import pyotp
        code = pyotp.TOTP(setup["secret"]).now()
        # Enable
        r = client.post("/admin/2fa/enable", json={"code": code})
        assert r.status_code == 200
        # Status
        s = client.get("/admin/2fa/status").json()
        assert s["enabled"] is True

    def test_enable_with_wrong_code(self):
        os.environ["ADMIN_EMAILS"] = "totp-wrong@example.com"
        from server import security as _sec
        _sec.ADMIN_EMAILS = {"totp-wrong@example.com"}
        with SessionLocal() as db:
            uid, uemail = _user(db, "totp-wrong@example.com")
        client = _client_for((uid, uemail))
        client.post("/admin/2fa/setup")
        r = client.post("/admin/2fa/enable", json={"code": "000000"})
        assert r.status_code == 400


# ════════════════════════════════════════════════════════════════════════════
# IdempotencyRecord — multi-worker safety
# ════════════════════════════════════════════════════════════════════════════

class TestIdempotency:

    def test_put_then_get(self):
        from server.routes.chat import _idempotency_get, _idempotency_put
        with SessionLocal() as db:
            uid, uemail = _user(db, "idem-test@example.com")
        # Уникальный ключ чтобы не упасть из-за остатков предыдущих прогонов
        key = "test-key-" + _secrets.token_hex(8)
        with SessionLocal() as db:
            ok = _idempotency_put(db, uid, key, {"response": "hello"})
            assert ok is True
        with SessionLocal() as db:
            cached = _idempotency_get(db, uid, key)
            assert cached is not None
            assert cached["response"] == "hello"

    def test_duplicate_put_returns_false(self):
        """Race condition: второй воркер пытается записать — UNIQUE-violation
        → возвращаем False, caller может прочитать существующую запись."""
        from server.routes.chat import _idempotency_put
        with SessionLocal() as db:
            uid, uemail = _user(db, "idem-race@example.com")
        key = "race-key-" + _secrets.token_hex(8)
        with SessionLocal() as db:
            ok1 = _idempotency_put(db, uid, key, {"v": 1})
            assert ok1 is True
        with SessionLocal() as db:
            ok2 = _idempotency_put(db, uid, key, {"v": 2})
            assert ok2 is False  # UNIQUE-violation, возвращаем False

    def test_empty_key_returns_none(self):
        from server.routes.chat import _idempotency_get, _idempotency_put
        with SessionLocal() as db:
            uid, uemail = _user(db, "idem-empty@example.com")
            assert _idempotency_get(db, uid, "") is None
            assert _idempotency_put(db, uid, "", {"v": 1}) is False


# ════════════════════════════════════════════════════════════════════════════
# Mobile voice TTS — endpoint существует и валидирует
# ════════════════════════════════════════════════════════════════════════════

class TestMobileTts:

    def test_empty_text_rejected(self):
        with SessionLocal() as db:
            uid, uemail = _user(db, "tts-empty@example.com")
        client = _client_for((uid, uemail))
        r = client.post("/mobile/voice/tts", json={"text": ""})
        assert r.status_code == 400

    def test_too_long_text_rejected(self):
        with SessionLocal() as db:
            uid, uemail = _user(db, "tts-long@example.com")
        client = _client_for((uid, uemail))
        r = client.post("/mobile/voice/tts", json={"text": "A" * 5000})
        assert r.status_code == 413

    def test_no_balance_rejected(self):
        with SessionLocal() as db:
            uid, uemail = _user(db, "tts-poor@example.com", balance=0)
        client = _client_for((uid, uemail))
        r = client.post("/mobile/voice/tts", json={"text": "Привет!"})
        assert r.status_code == 402


# ════════════════════════════════════════════════════════════════════════════
# Marketplace anti-pump (UNIQUE на платных установках)
# ════════════════════════════════════════════════════════════════════════════

class TestMarketplaceAntiPump:
    """Платный листинг нельзя установить дважды одному юзеру (anti-pump):
    UNIQUE-индекс на (listing_id, installer_id) WHERE paid_kop > 0.
    Защищает автора от collusion-схемы «два аккаунта установили друг другу
    100 раз и собрали 70%×100 автору». Race-safe на multi-worker."""

    def _make_paid_listing(self, author_id, price=10000):
        from server.models import BotMarketplaceListing
        with SessionLocal() as db:
            l = BotMarketplaceListing(
                author_id=author_id,
                name="Тестовый платный шаблон",
                price_kop=price,
                system_prompt="Ты помощник.",
                is_approved=True, is_active=True,
            )
            db.add(l); db.commit(); db.refresh(l)
            return l.id

    def test_paid_install_then_repeat_returns_409(self):
        with SessionLocal() as db:
            author = _user(db, "author@market.test", balance=0)
            buyer = _user(db, "buyer@market.test", balance=100_000)
        listing_id = self._make_paid_listing(author[0], price=5000)
        client = _client_for(buyer)
        r1 = client.post(f"/marketplace/listings/{listing_id}/install")
        assert r1.status_code == 200, r1.text
        # Повторно — UNIQUE сработает → 409
        r2 = client.post(f"/marketplace/listings/{listing_id}/install")
        assert r2.status_code == 409

    def test_installs_count_atomic(self):
        """installs_count должен инкрементиться через atomic UPDATE."""
        from server.models import BotMarketplaceListing
        with SessionLocal() as db:
            author = _user(db, "author2@market.test", balance=0)
            buyer1 = _user(db, "buy1@market.test", balance=100_000)
            buyer2 = _user(db, "buy2@market.test", balance=100_000)
        listing_id = self._make_paid_listing(author[0], price=3000)
        # Двое разных юзеров — оба установили
        _client_for(buyer1).post(f"/marketplace/listings/{listing_id}/install")
        _client_for(buyer2).post(f"/marketplace/listings/{listing_id}/install")
        with SessionLocal() as db:
            l = db.query(BotMarketplaceListing).filter_by(id=listing_id).first()
            assert l.installs_count == 2


# ════════════════════════════════════════════════════════════════════════════
# Webhook atomic fail_count + auto-disable
# ════════════════════════════════════════════════════════════════════════════

class TestWebhookAtomicFailCount:
    """fail_count должен инкрементиться через atomic UPDATE (без race на
    read-then-write). Auto-disable срабатывает после MAX_FAIL_BEFORE_DISABLE."""

    def test_failed_post_increments_fail_count_atomically(self):
        """Симулируем 10 ошибок → ApiWebhook.is_active должен стать False."""
        from server.models import ApiWebhook, ApiToken
        from server import webhooks as wh
        with SessionLocal() as db:
            uid, _ = _user(db, "wh-atomic@example.com")
            import uuid as _uuid
            tok = ApiToken(
                user_id=uid,
                prefix="ai_che_test_" + _uuid.uuid4().hex[:10],
                secret_hash="dummyhash" * 8,
                name="test",
                scopes="read",
                is_active=True,
            )
            db.add(tok); db.commit(); db.refresh(tok)
            w = ApiWebhook(
                user_id=uid,
                url="http://127.0.0.1:1/never-resolves",  # точно упадёт
                events="proposal.signed",
                secret="sek_" + "a" * 32,
                is_active=True,
                fail_count=0, total_calls=0,
            )
            db.add(w); db.commit(); db.refresh(w)
            wid = w.id
        # MAX_FAIL_BEFORE_DISABLE раз вызовем fire-and-forget с гарантированной ошибкой
        for _ in range(wh.MAX_FAIL_BEFORE_DISABLE):
            wh._post_sync(wid, {"event": "x", "data": {}})
        with SessionLocal() as db:
            w2 = db.query(ApiWebhook).filter_by(id=wid).first()
            assert w2.fail_count >= wh.MAX_FAIL_BEFORE_DISABLE
            assert w2.is_active is False  # auto-disable сработал


# ════════════════════════════════════════════════════════════════════════════
# 2FA rate-limit (smoke)
# ════════════════════════════════════════════════════════════════════════════

class TestAdmin2faRateLimit:
    """В RULES должен быть префикс /admin/2fa/ для защиты от брутфорса."""

    def test_rule_present(self):
        from server.security import RULES
        assert "/admin/2fa/" in RULES, \
            "Должна быть rate-limit правило для /admin/2fa/"
        max_calls, window = RULES["/admin/2fa/"]
        assert max_calls <= 30 and window >= 60, \
            "Лимит должен быть достаточно строгим"


# ════════════════════════════════════════════════════════════════════════════
# Voice/TTS rate-limit правило (smoke)
# ════════════════════════════════════════════════════════════════════════════

class TestVoiceTtsRateLimit:
    def test_rule_present(self):
        from server.security import RULES
        assert "/mobile/voice/tts" in RULES, \
            "TTS endpoint должен быть в RULES (защита от слива баланса)"
