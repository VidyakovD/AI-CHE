import os, json, uuid, logging
from urllib.parse import urlparse
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from server.routes.deps import get_db, current_user, _user_dict
from server.models import User, Transaction, TokenPackage
from server.payments import check_payment, credit_referral_bonus
from server.billing import credit_atomic

log = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["payments"])


_APP_URL = os.getenv("APP_URL", "http://localhost:8000").rstrip("/")
_DEFAULT_RETURN_URL = _APP_URL + "/?payment=success"
_ALLOWED_RETURN_HOST = (urlparse(_APP_URL).hostname or "").lower()


def _validate_return_url(url: str) -> str:
    """
    Защищает от open-redirect: разрешаем только URL c host == APP_URL.host.
    Если юзер передал произвольный — fallback на дефолт.
    """
    if not url:
        return _DEFAULT_RETURN_URL
    try:
        p = urlparse(url)
    except Exception:
        return _DEFAULT_RETURN_URL
    if p.scheme not in ("http", "https"):
        return _DEFAULT_RETURN_URL
    host = (p.hostname or "").lower()
    if host != _ALLOWED_RETURN_HOST:
        log.warning(f"buy-tokens: return_url={url!r} host={host!r} not allowed → fallback")
        return _DEFAULT_RETURN_URL
    return url


class BuyTokenRequest(BaseModel):
    package_id: int
    return_url: str = _DEFAULT_RETURN_URL

    @field_validator("return_url")
    @classmethod
    def _check_return_url(cls, v: str) -> str:
        return _validate_return_url(v)


@router.post("/payment/webhook")
async def payment_webhook(request: Request, db: Session = Depends(get_db)):
    """ЮKassa webhook — атомарное зачисление баланса (в копейках)."""
    import hashlib, hmac

    # ВАЖНО: читаем raw body ДО json — иначе stream съедается и HMAC будет от пустых байт.
    raw_body = await request.body()
    try:
        body = json.loads(raw_body)
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    hmac_header = request.headers.get("X-Content-Signature")
    secret = os.getenv("YOOKASSA_SECRET_KEY", "")
    # HARD FAIL без секрета: иначе webhook становится unauthenticated и
    # любой может зачислить себе баланс (нужно знать только user_id).
    # Исключение — DEV_MODE / явный ALLOW_UNVERIFIED_WEBHOOK для тестов.
    #
    # SAFETY: на production (APP_ENV=production) DEV_MODE НЕ позволяет обход —
    # это защита от случайного утечки DEV_MODE=1 в прод .env (что превратило бы
    # webhook в дыру для произвольного зачисления баланса).
    _is_prod = os.getenv("APP_ENV", "").strip().lower() == "production"
    _allow_unverified = (
        not _is_prod and (
            os.getenv("DEV_MODE", "").lower() in ("1", "true", "yes")
            or os.getenv("ALLOW_UNVERIFIED_WEBHOOK", "").lower() in ("1", "true", "yes")
        )
    )
    if not secret:
        if not _allow_unverified:
            log.error("Webhook: YOOKASSA_SECRET_KEY not set in production — rejecting")
            raise HTTPException(503, "Webhook signature secret not configured")
        log.warning("Webhook: secret not set, accepting unverified (dev mode)")
    if secret:
        if not hmac_header:
            log.warning("Webhook: missing X-Content-Signature while secret is configured")
            raise HTTPException(401, "Signature required")
        import re
        match = re.match(r"^sha256=([0-9a-f]{64})$", hmac_header)
        if not match:
            raise HTTPException(401, "Malformed signature")
        computed = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed, match.group(1)):
            raise HTTPException(401, "Invalid signature")

    obj = body.get("object", body)
    payment_id = obj.get("id")
    if not payment_id:
        raise HTTPException(400, "No payment id in webhook")

    from yookassa import Payment as YKP
    try:
        p = YKP.find_one(payment_id)
    except Exception as e:
        log.error(f"Webhook: cannot verify payment {payment_id}: {e}")
        return {"status": "verification_failed"}
    if p.status != "succeeded":
        return {"status": "not_yet_paid"}

    yk_meta = p.metadata or {}
    # Decimal-парсинг: float-арифметика теряет точность (`0.01 * 100 != 1` в
    # бинарной плавающей точке) — при оплате 99.99₽ может списаться 10000 коп
    # вместо 9999. Decimal обеспечивает точность до копейки.
    from decimal import Decimal as _Decimal, ROUND_HALF_UP
    if p.amount:
        try:
            _amt_dec = _Decimal(str(p.amount.value))
        except Exception:
            _amt_dec = _Decimal(0)
    else:
        _amt_dec = _Decimal(0)
    amount_rub = float(_amt_dec)  # для логов/совместимости
    amount_kop = int((_amt_dec * 100).quantize(_Decimal("1"), rounding=ROUND_HALF_UP))

    user_id = yk_meta.get("user_id")
    if not user_id:
        return {"status": "no_user_id"}
    db_user = db.query(User).filter_by(id=int(user_id)).first()
    if not db_user:
        return {"status": "user_not_found"}

    # Защита от двойного зачисления
    existing_tx = db.query(Transaction).filter_by(yookassa_payment_id=payment_id).first()
    if existing_tx:
        return {"status": "already_credited"}

    pkg_id = yk_meta.get("package_id")
    pkg_name = yk_meta.get("pkg_name", "")
    if pkg_id:
        pkg = db.query(TokenPackage).filter_by(id=int(pkg_id)).first()
        if pkg:
            pkg_name = pkg.name
            # Зачисляем сумму в копейках по price_rub пакета (с возможным бонусом
            # в pkg.tokens — теперь это копейки, см. миграцию × 10)
            amount_kop = int(pkg.tokens) if pkg.tokens else amount_kop

    # Порядок: сначала Transaction (UNIQUE на yookassa_payment_id ловит дубль),
    # потом credit_atomic. Если параллельный webhook вставил Tx первым — наш
    # commit упадёт IntegrityError, словим и вернём already_credited.
    credit_atomic(db, db_user.id, amount_kop)
    credit_referral_bonus(db, db_user, amount_kop, pkg_name or f"{amount_rub:.2f} ₽",
                          payment_id=payment_id)
    db.add(Transaction(
        user_id=db_user.id, type="payment", amount_rub=amount_rub,
        tokens_delta=amount_kop,
        description=f"Пополнение баланса: {pkg_name or f'{amount_rub:.2f} ₽'} (webhook)",
        yookassa_payment_id=payment_id,
    ))
    try:
        db.commit()
    except Exception as e:
        # IntegrityError на UNIQUE(yookassa_payment_id) — конкурентный
        # webhook опередил. Откатываем и возвращаем already_credited.
        db.rollback()
        log.warning(f"Webhook: race for payment {payment_id}: {e}")
        return {"status": "already_credited"}
    # 152-ФЗ best practice: в логах оставляем только идентификатор платежа
    # и факт успеха. Сумма видна в Transaction (защищённая БД), а не в
    # plaintext logs / Sentry / journalctl.
    log.info(f"Webhook: credited payment_id={payment_id} for user {user_id}")
    from server.audit_log import log_action
    log_action("payment.webhook", user_id=int(user_id), target_type="payment", target_id=payment_id,
               details={"pkg": pkg_name})
    return {"status": "ok"}


@router.post("/payment/buy-tokens")
def buy_tokens(req: BuyTokenRequest, user: User = Depends(current_user),
               db: Session = Depends(get_db)):
    """Создать платёж на пополнение баланса по выбранному пакету."""
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email для оплаты")
    # Без email нельзя создать receipt → ЮKassa с подключённым ОФД (54-ФЗ)
    # либо откажет, либо примет платёж без чека (штраф 30k₽ за каждый
    # некассовый платёж). is_verified выше гарантирует email есть, но
    # defence-in-depth: явный 400 а не silent skip receipt.
    if not (user.email or "").strip():
        raise HTTPException(400, "У аккаунта нет email — невозможно отправить чек 54-ФЗ. "
                                  "Укажите email в профиле.")
    pkg = db.query(TokenPackage).filter_by(id=req.package_id, is_active=True).first()
    if not pkg:
        raise HTTPException(404, "Пакет не найден")
    try:
        from server.payments import _init_yookassa
        _init_yookassa()
        from yookassa import Payment as YKP
        payment_data = {
            "amount": {"value": str(float(pkg.price_rub)), "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": req.return_url},
            "capture": True,
            "description": f"AI Студия Че — пополнение «{pkg.name}» (user {user.id})",
            "metadata": {
                "user_id": str(user.id),
                "package_id": str(pkg.id),
                "pkg_name": pkg.name,
            },
        }
        # Чек 54-ФЗ — обязателен, email проверен выше.
        # ЮKassa передаёт receipt в подключённый ОФД (Атол / Контур),
        # чек уходит на customer.email автоматически.
        #
        # Поля:
        # - vat_code: 1=Без НДС | 2=НДС 0% | 3=НДС 10% | 4=НДС 20% |
        #             5=НДС 10/110 | 6=НДС 20/120
        # - tax_system_code: 1=ОСН | 2=УСН доходы | 3=УСН доходы-расходы
        #                    | 4=ЕНВД | 5=ЕСН | 6=ПСН
        # - payment_subject: "service" — услуга.
        # - payment_mode: "full_payment" — полная оплата при покупке.
        #
        # На проде .env: YOOKASSA_VAT_CODE=1, YOOKASSA_TAX_SYSTEM_CODE=2
        # (ИП на УСН доходы 6%, без НДС). Меняй там при смене налоговой.
        vat_code = os.getenv("YOOKASSA_VAT_CODE", "1")
        tax_system = os.getenv("YOOKASSA_TAX_SYSTEM_CODE", "2")
        payment_data["receipt"] = {
            "customer": {"email": user.email},
            "tax_system_code": int(tax_system),
            "items": [{
                "description": f"Пополнение баланса: {pkg.name}"[:128],
                "quantity": "1",
                "amount": {"value": str(float(pkg.price_rub)), "currency": "RUB"},
                "vat_code": int(vat_code),
                "payment_subject": "service",
                "payment_mode": "full_payment",
            }],
        }
        p = YKP.create(payment_data, str(uuid.uuid4()))
        return {
            "payment_id": p.id,
            "confirmation_url": p.confirmation.confirmation_url,
            "status": p.status,
        }
    except Exception as e:
        log.error(f"buy-tokens: YooKassa create failed: {type(e).__name__}: {e}")
        raise HTTPException(500, "Ошибка создания платежа, попробуйте позже")


@router.get("/payment/confirm-tokens/{payment_id}")
def confirm_tokens(payment_id: str, user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    """Подтверждает платёж через ЮKassa API и зачисляет баланс."""
    try:
        status = check_payment(payment_id)
    except Exception as e:
        raise HTTPException(500, str(e))
    if status != "succeeded":
        return {"status": status}

    existing = db.query(Transaction).filter_by(yookassa_payment_id=payment_id).first()
    if existing:
        return {"status": "already_credited"}

    from yookassa import Payment as YKP
    p = YKP.find_one(payment_id)
    meta_user_id = (p.metadata or {}).get("user_id")
    if str(meta_user_id) != str(user.id):
        log.warning(f"User {user.id} tried to confirm payment {payment_id} of user {meta_user_id}")
        raise HTTPException(403, "Этот платёж принадлежит другому пользователю")
    pkg_id = int((p.metadata or {}).get("package_id", 0))
    pkg = db.query(TokenPackage).filter_by(id=pkg_id).first()
    if not pkg:
        raise HTTPException(404, "Пакет не найден")

    db_user = db.query(User).filter_by(id=user.id).first()
    amount_kop = int(pkg.tokens) if pkg.tokens else int(round(float(pkg.price_rub) * 100))
    credit_atomic(db, user.id, amount_kop)
    credit_referral_bonus(db, db_user, amount_kop, pkg.name, payment_id=payment_id)
    db.add(Transaction(
        user_id=user.id, type="payment",
        amount_rub=pkg.price_rub, tokens_delta=amount_kop,
        description=f"Пополнение баланса: {pkg.name}",
        yookassa_payment_id=payment_id,
    ))
    try:
        db.commit()
    except Exception as e:
        # UNIQUE(yookassa_payment_id) — webhook опередил.
        db.rollback()
        log.warning(f"Confirm-tokens: race for payment {payment_id}: {e}")
        return {"status": "already_credited"}
    from server.audit_log import log_action
    # См. payment.webhook — сумма не пишется в audit-лог (хранится только в Transaction).
    log_action("payment.confirm", user_id=user.id, target_type="payment", target_id=payment_id,
               details={"pkg": pkg.name})
    return {"status": "credited", "kopecks_added": amount_kop,
            "rub_added": amount_kop / 100}
