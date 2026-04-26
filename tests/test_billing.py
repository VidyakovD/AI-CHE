"""
Тесты для критичной денежной логики:
- claim_welcome_bonus / claim_referral_signup_bonus — exactly-once при гонке
- credit_referral_bonus — идемпотентность по payment_id
- deduct_atomic / deduct_strict — нельзя уйти в минус
- worker_lock — fail-closed при сбое БД
- widget Origin allowlist
- _inject_chatbot_widget — корректная вставка перед последним </body>

Запуск: python -m pytest tests/test_billing.py -v
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import threading
import pytest
from sqlalchemy.orm import Session

from server.db import Base, engine, SessionLocal
from server.models import User, Transaction

# В тестовой среде passlib+bcrypt+Python 3.14 ломается на инициализации
# (bcrypt 4.x ругается на «72 bytes», ловим detection-probe). Используем
# заранее посчитанный валидный bcrypt-хеш — тесты биллинга не проверяют
# auth, им важно только наличие записи в users.password_hash.
_FAKE_BCRYPT_HASH = "$2b$12$abcdefghijklmnopqrstuvCxyz0123456789ABCDEFGHIJKLMNOPQRSTU"


# ── helpers ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module", autouse=True)
def _setup_db():
    Base.metadata.create_all(bind=engine)
    yield


def _make_user(db: Session, email: str, balance: int = 0,
                referred_by: str | None = None) -> User:
    """Создать (или вернуть) тестового юзера с заданным балансом."""
    u = db.query(User).filter_by(email=email).first()
    if u:
        u.tokens_balance = balance
        u.welcome_bonus_claimed_at = None
        u.referral_signup_bonus_paid_at = None
        u.referred_by = referred_by
        db.commit()
        return u
    import uuid
    u = User(
        email=email,
        password_hash=_FAKE_BCRYPT_HASH,
        name=email.split("@")[0],
        tokens_balance=balance,
        is_verified=True,
        agreed_to_terms=True,
        referral_code=uuid.uuid4().hex[:8].upper(),
        referred_by=referred_by,
    )
    db.add(u); db.commit(); db.refresh(u)
    return u


# ── 1. Welcome bonus — exactly-once ──────────────────────────────────────────

class TestWelcomeBonus:
    def test_claim_welcome_bonus_first_time_succeeds(self):
        from server.billing import claim_welcome_bonus
        db = SessionLocal()
        try:
            u = _make_user(db, "wb1@test.com", balance=0)
            assert claim_welcome_bonus(db, u.id, 5000) is True
            db.commit()
            db.refresh(u)
            assert u.tokens_balance == 5000
            assert u.welcome_bonus_claimed_at is not None
        finally:
            db.close()

    def test_claim_welcome_bonus_second_time_returns_false(self):
        from server.billing import claim_welcome_bonus
        db = SessionLocal()
        try:
            u = _make_user(db, "wb2@test.com", balance=0)
            assert claim_welcome_bonus(db, u.id, 5000) is True
            db.commit()
            assert claim_welcome_bonus(db, u.id, 5000) is False  # повтор
            db.commit()
            db.refresh(u)
            assert u.tokens_balance == 5000  # бонус НЕ удвоился

        finally:
            db.close()

    def test_claim_welcome_bonus_concurrent_only_one_succeeds(self):
        """Эмуляция гонки: 5 параллельных потоков пытаются клейм. Должен победить ровно один."""
        from server.billing import claim_welcome_bonus
        db = SessionLocal()
        u = _make_user(db, "wb_race@test.com", balance=0)
        user_id = u.id
        db.close()

        results = []

        def worker():
            local_db = SessionLocal()
            try:
                ok = claim_welcome_bonus(local_db, user_id, 5000)
                local_db.commit()
                results.append(ok)
            finally:
                local_db.close()

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads: t.start()
        for t in threads: t.join()

        assert sum(1 for r in results if r) == 1, f"Должен победить ровно 1: {results}"
        # Баланс ровно 5000, не 25000
        check = SessionLocal()
        try:
            u2 = check.query(User).filter_by(id=user_id).first()
            assert u2.tokens_balance == 5000
        finally:
            check.close()


# ── 2. Referral signup bonus — exactly-once ─────────────────────────────────

class TestReferralSignupBonus:
    def test_referral_signup_bonus_first_succeeds(self):
        from server.billing import claim_referral_signup_bonus
        db = SessionLocal()
        try:
            referrer = _make_user(db, "ref_owner@test.com", balance=1000)
            referred = _make_user(db, "ref_target@test.com",
                                   balance=0, referred_by=referrer.referral_code)
            ok = claim_referral_signup_bonus(db, referred.id, referrer.id, 1000)
            db.commit()
            assert ok is True
            db.refresh(referrer)
            assert referrer.tokens_balance == 2000
        finally:
            db.close()

    def test_referral_signup_bonus_no_double_payment(self):
        from server.billing import claim_referral_signup_bonus
        db = SessionLocal()
        try:
            referrer = _make_user(db, "ref_owner2@test.com", balance=0)
            referred = _make_user(db, "ref_target2@test.com",
                                   balance=0, referred_by=referrer.referral_code)
            assert claim_referral_signup_bonus(db, referred.id, referrer.id, 1000) is True
            db.commit()
            assert claim_referral_signup_bonus(db, referred.id, referrer.id, 1000) is False
            db.commit()
            db.refresh(referrer)
            assert referrer.tokens_balance == 1000
        finally:
            db.close()


# ── 3. credit_referral_bonus — idempotent by payment_id ─────────────────────

class TestReferralPaymentBonus:
    def test_credit_referral_bonus_double_call_same_payment(self):
        """Повторный credit с тем же payment_id не должен удваивать бонус.
        payment_id генерируем уникально, чтобы не конфликтовать с предыдущими прогонами."""
        from server.payments import credit_referral_bonus
        import uuid as _u
        pay_id = f"pay_test_{_u.uuid4().hex[:10]}"
        db = SessionLocal()
        try:
            referrer = _make_user(db, "rpay_owner@test.com", balance=0)
            referred = _make_user(db, "rpay_target@test.com",
                                   balance=0, referred_by=referrer.referral_code)
            initial = int(referrer.tokens_balance or 0)
            # Первый вызов начисляет 10% от 10000 = 1000
            credit_referral_bonus(db, referred, 10_000, "TestPkg", payment_id=pay_id)
            db.commit()
            db.refresh(referrer)
            assert referrer.tokens_balance == initial + 1000
            # Повторный вызов с тем же payment_id — НЕ начисляет
            credit_referral_bonus(db, referred, 10_000, "TestPkg", payment_id=pay_id)
            db.commit()
            db.refresh(referrer)
            assert referrer.tokens_balance == initial + 1000  # не удвоилось
        finally:
            db.close()

    def test_credit_referral_bonus_different_payments_both_credit(self):
        from server.payments import credit_referral_bonus
        import uuid as _u
        pay_a = f"pay_A_{_u.uuid4().hex[:10]}"
        pay_b = f"pay_B_{_u.uuid4().hex[:10]}"
        db = SessionLocal()
        try:
            referrer = _make_user(db, "rpay_owner3@test.com", balance=0)
            referred = _make_user(db, "rpay_target3@test.com",
                                   balance=0, referred_by=referrer.referral_code)
            initial = int(referrer.tokens_balance or 0)
            credit_referral_bonus(db, referred, 10_000, "Pkg1", payment_id=pay_a)
            credit_referral_bonus(db, referred, 20_000, "Pkg2", payment_id=pay_b)
            db.commit()
            db.refresh(referrer)
            # 10% от 10k + 10% от 20k = 1000 + 2000
            assert referrer.tokens_balance == initial + 3000
        finally:
            db.close()


# ── 4. deduct_atomic / deduct_strict ─────────────────────────────────────────

class TestDeduct:
    def test_deduct_strict_succeeds_when_enough(self):
        from server.billing import deduct_strict
        db = SessionLocal()
        try:
            u = _make_user(db, "ded1@test.com", balance=10_000)
            assert deduct_strict(db, u.id, 5000) is True
            db.commit()
            db.refresh(u)
            assert u.tokens_balance == 5000
        finally:
            db.close()

    def test_deduct_strict_fails_when_insufficient(self):
        from server.billing import deduct_strict
        db = SessionLocal()
        try:
            u = _make_user(db, "ded2@test.com", balance=100)
            assert deduct_strict(db, u.id, 5000) is False
            db.commit()
            db.refresh(u)
            assert u.tokens_balance == 100  # не списали ничего
        finally:
            db.close()

    def test_deduct_atomic_returns_partial(self):
        from server.billing import deduct_atomic
        db = SessionLocal()
        try:
            u = _make_user(db, "ded3@test.com", balance=100)
            charged = deduct_atomic(db, u.id, 5000)
            db.commit()
            db.refresh(u)
            assert charged == 100  # списали остаток
            assert u.tokens_balance == 0  # не ушли в минус
        finally:
            db.close()

    def test_deduct_concurrent_no_negative_balance(self):
        """Две параллельных deduct_strict на сумму > половины — обе могут пройти?
        Не должны: оба списания на 6000 при балансе 10000 — пройдёт только один."""
        from server.billing import deduct_strict
        db = SessionLocal()
        u = _make_user(db, "ded_race@test.com", balance=10_000)
        user_id = u.id
        db.close()

        results = []

        def worker():
            local_db = SessionLocal()
            try:
                ok = deduct_strict(local_db, user_id, 6000)
                local_db.commit()
                results.append(ok)
            finally:
                local_db.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join()

        # Один должен быть True, один False
        assert sorted(results) == [False, True], f"Expected [False, True], got {results}"
        check = SessionLocal()
        try:
            u2 = check.query(User).filter_by(id=user_id).first()
            assert u2.tokens_balance == 4000  # 10000 - 6000
        finally:
            check.close()


# ── 5. worker_lock — fail-closed ─────────────────────────────────────────────

class TestWorkerLock:
    def test_worker_lock_acquires_first_releases_after(self):
        from server.worker_lock import worker_lock
        with worker_lock("test_lock_basic", ttl_sec=5) as acquired:
            assert acquired is True
        # После выхода — снова можно взять
        with worker_lock("test_lock_basic", ttl_sec=5) as acquired:
            assert acquired is True

    def test_worker_lock_blocks_concurrent(self):
        from server.worker_lock import worker_lock
        with worker_lock("test_lock_concurrent", ttl_sec=10) as a1:
            assert a1 is True
            # Внутри — пробуем второй раз ту же блокировку
            with worker_lock("test_lock_concurrent", ttl_sec=10) as a2:
                assert a2 is False  # занято

    def test_worker_lock_fail_closed_on_db_error(self, monkeypatch):
        """При исключении в _try_acquire должен возвращать False, не True."""
        from server import worker_lock as wl_mod

        def boom(*a, **kw):
            raise RuntimeError("simulated DB failure")

        monkeypatch.setattr(wl_mod, "_conn", boom)
        with wl_mod.worker_lock("any", ttl_sec=5) as acquired:
            assert acquired is False  # fail-CLOSED, не True


# ── 6. Widget Origin allowlist ───────────────────────────────────────────────

class TestWidgetOrigin:
    def test_empty_allowlist_permits_any(self):
        from server.routes.widget import _origin_allowed
        assert _origin_allowed("https://anywhere.example", "") is True
        assert _origin_allowed("https://anywhere.example", None) is True

    def test_exact_match(self):
        from server.routes.widget import _origin_allowed
        assert _origin_allowed("https://example.com", "example.com") is True
        assert _origin_allowed("http://example.com:8080", "example.com") is True

    def test_subdomain_wildcard(self):
        from server.routes.widget import _origin_allowed
        assert _origin_allowed("https://app.example.com", "*.example.com") is True
        assert _origin_allowed("https://example.com", "*.example.com") is True
        assert _origin_allowed("https://other.com", "*.example.com") is False

    def test_csv_multiple_origins(self):
        from server.routes.widget import _origin_allowed
        wl = "example.com, app.example.com,  *.partner.io  "
        assert _origin_allowed("https://example.com", wl) is True
        assert _origin_allowed("https://app.example.com", wl) is True
        assert _origin_allowed("https://x.partner.io", wl) is True
        assert _origin_allowed("https://evil.com", wl) is False

    def test_empty_origin_with_allowlist_denies(self):
        from server.routes.widget import _origin_allowed
        # При непустом whitelist отсутствие Origin = отказ.
        assert _origin_allowed("", "example.com") is False


# ── 7. Widget injection в HTML ──────────────────────────────────────────────

class TestWidgetInjection:
    def test_inject_before_body(self):
        from server.routes.sites import _inject_chatbot_widget
        html = "<html><body><h1>Hi</h1></body></html>"
        out = _inject_chatbot_widget(html, 42, "https://aiche.ru")
        assert "/widget/42.js" in out
        # Виджет должен быть ПЕРЕД </body>
        assert out.index("/widget/42.js") < out.index("</body>")

    def test_inject_picks_last_body_when_multiple(self):
        from server.routes.sites import _inject_chatbot_widget
        # Корявый AI-вывод с двумя </body> (бывает при auto-continue склейке)
        html = "<html><body>A</body><body>B</body></html>"
        out = _inject_chatbot_widget(html, 42, "https://aiche.ru")
        # Должен попасть перед ПОСЛЕДНИМ </body>, иначе скрипт не выполнится
        first_body = out.index("</body>")
        last_body = out.rindex("</body>")
        assert first_body != last_body
        assert out.index("/widget/42.js") > first_body
        assert out.index("/widget/42.js") < last_body

    def test_inject_no_body_falls_back_to_html(self):
        from server.routes.sites import _inject_chatbot_widget
        html = "<html><h1>No body</h1></html>"
        out = _inject_chatbot_widget(html, 42, "https://aiche.ru")
        assert "/widget/42.js" in out
        assert "</body>" in out  # достроили
        assert out.index("/widget/42.js") < out.index("</html>")

    def test_inject_no_html_no_body(self):
        from server.routes.sites import _inject_chatbot_widget
        html = "<h1>fragment</h1>"
        out = _inject_chatbot_widget(html, 42, "https://aiche.ru")
        assert "/widget/42.js" in out
        assert "</body>" in out
        assert "</html>" in out


# ── 8. Widget JS escape ──────────────────────────────────────────────────────

class TestWidgetSafeString:
    def test_basic_string(self):
        from server.routes.widget import _safe_js_string
        assert _safe_js_string("Hello") == '"Hello"'

    def test_quotes_escaped(self):
        from server.routes.widget import _safe_js_string
        out = _safe_js_string('say "hi" and \'bye\'')
        assert '\\"' in out

    def test_script_tag_escaped(self):
        from server.routes.widget import _safe_js_string
        # </script> внутри JS-литерала закрыло бы текущий <script> тег.
        # Должно быть заменено на <\/script>.
        out = _safe_js_string("evil </script> name")
        assert "</script>" not in out
        assert "<\\/script>" in out
