"""Internal API для multi-surface integration (2026-06-01).

═══ КОНТЕКСТ ═══

aiche.ru — source of truth для User/баланса. Mini App (aichevk.ru),
TG-бот @aiche_bot, MAX-бот @aiche_max — клиенты этого backend'а.

Они НЕ хранят свой баланс. На каждое списание/начисление — HTTP-вызов
сюда. Защита: HMAC-SHA256 от (timestamp + body) с общим INTERNAL_API_SECRET.

═══ ENDPOINTS ═══

  POST /internal/v1/identify     — найти/создать юзера по identifier
  POST /internal/v1/link         — добавить identifier к существующему юзеру
  POST /internal/v1/link/code/issue   — выдать одноразовый код привязки
  POST /internal/v1/link/code/redeem  — обменять код на привязку
  GET  /internal/v1/balance/{user_id}
  POST /internal/v1/debit        — списать (idempotent)
  POST /internal/v1/credit       — начислить (idempotent)
  GET  /internal/v1/pricing      — текущие тарифы + курс ЦБ
  POST /internal/v1/topup/create — создать ЮKassa-платёж от имени юзера

═══ HMAC FLOW ═══

Caller формирует:
  ts = current unix timestamp
  body = JSON string (или "" для GET)
  signature = HMAC-SHA256(ts + "." + body, INTERNAL_API_SECRET).hex()

Шлёт headers:
  X-Internal-Timestamp: <ts>
  X-Internal-Signature: <signature>
  Content-Type: application/json

Сервер валидирует: ts в окне ±300 сек, signature совпадает.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Body, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from server.db import db_session
from server.models import (
    IdempotencyRecord, PricingConfig, Transaction, User,
)
from server.billing import credit_atomic, deduct_strict, get_balance

log = logging.getLogger("internal-api")
router = APIRouter(prefix="/internal/v1", tags=["internal"])


# ── HMAC verification ──────────────────────────────────────────────────────


_TIMESTAMP_TOLERANCE_SEC = 300  # ±5 минут для clock-skew


def _internal_secret() -> str:
    """Общий секрет для HMAC. В env INTERNAL_API_SECRET. Без него API заблокирован."""
    return (os.getenv("INTERNAL_API_SECRET") or "").strip()


def _compute_signature(ts: str, body: str, secret: str) -> str:
    payload = f"{ts}.{body}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


async def _verify_hmac(request: Request) -> bytes:
    """Проверяет X-Internal-Timestamp + X-Internal-Signature. Возвращает raw body
    чтобы caller мог его распарсить (важно: body должен читаться ровно один раз)."""
    secret = _internal_secret()
    if not secret:
        log.error("[internal] INTERNAL_API_SECRET не задан — все запросы reject")
        raise HTTPException(503, "Internal API disabled (no secret)")
    ts = request.headers.get("X-Internal-Timestamp", "").strip()
    sig = request.headers.get("X-Internal-Signature", "").strip()
    if not ts or not sig:
        raise HTTPException(401, "Missing X-Internal-Timestamp or X-Internal-Signature")
    try:
        ts_int = int(ts)
    except ValueError:
        raise HTTPException(401, "Invalid timestamp")
    now = int(time.time())
    if abs(now - ts_int) > _TIMESTAMP_TOLERANCE_SEC:
        raise HTTPException(401, "Timestamp out of window")
    raw_body = await request.body()
    body_str = raw_body.decode("utf-8") if raw_body else ""
    expected = _compute_signature(ts, body_str, secret)
    if not hmac.compare_digest(expected, sig):
        raise HTTPException(401, "Invalid signature")
    return raw_body


def _parse_body(raw_body: bytes) -> dict:
    if not raw_body:
        return {}
    try:
        return json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON body")


# ── Helpers ───────────────────────────────────────────────────────────────


_PHONE_RE = re.compile(r"\+?\d{10,15}$")


def _normalize_phone(raw: str) -> Optional[str]:
    """Нормализуем телефон к +7XXXXXXXXXX (для РФ — главный кейс).
    Возвращаем None если формат некорректен."""
    if not raw:
        return None
    cleaned = re.sub(r"[^\d+]", "", raw)
    if cleaned.startswith("8") and len(cleaned) == 11:
        cleaned = "+7" + cleaned[1:]
    elif cleaned.startswith("7") and len(cleaned) == 11:
        cleaned = "+" + cleaned
    elif not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    if not _PHONE_RE.match(cleaned):
        return None
    return cleaned


_IDENT_KINDS = {"phone", "vk_user_id", "tg_user_id", "max_user_id", "email"}


def _find_user_by_identifier(db: Session, kind: str, value: str) -> Optional[User]:
    """Найти юзера по identifier. Возвращает первого подходящего (UNIQUE-like)."""
    if kind == "email":
        return db.query(User).filter_by(email=value.lower().strip()).first()
    if kind == "phone":
        normalized = _normalize_phone(value)
        if not normalized:
            return None
        return db.query(User).filter_by(phone=normalized).first()
    if kind == "vk_user_id":
        try:
            vk = int(value)
        except (TypeError, ValueError):
            return None
        # Сначала по новой колонке, потом fallback на oauth_sub (legacy)
        u = db.query(User).filter_by(vk_user_id=vk).first()
        if u:
            return u
        return db.query(User).filter_by(
            oauth_provider="vk", oauth_sub=str(vk)
        ).first()
    if kind == "tg_user_id":
        return db.query(User).filter_by(tg_user_id=str(value)).first()
    if kind == "max_user_id":
        return db.query(User).filter_by(max_user_id=str(value)).first()
    return None


def _set_identifier(user: User, kind: str, value: str) -> bool:
    """Записать identifier на user-объект. Возвращает True если что-то изменилось."""
    if kind == "phone":
        norm = _normalize_phone(value)
        if not norm or user.phone == norm:
            return False
        user.phone = norm
        return True
    if kind == "vk_user_id":
        try:
            vk = int(value)
        except (TypeError, ValueError):
            return False
        if user.vk_user_id == vk:
            return False
        user.vk_user_id = vk
        # Заодно подтянем oauth_provider/oauth_sub для backward-compat
        if not user.oauth_provider:
            user.oauth_provider = "vk"
            user.oauth_sub = str(vk)
        return True
    if kind == "tg_user_id":
        if user.tg_user_id == str(value):
            return False
        user.tg_user_id = str(value)
        return True
    if kind == "max_user_id":
        if user.max_user_id == str(value):
            return False
        user.max_user_id = str(value)
        return True
    return False


def _user_to_dict(u: User) -> dict:
    return {
        "user_id": u.id,
        "email": u.email,
        "phone": u.phone,
        "vk_user_id": u.vk_user_id,
        "tg_user_id": u.tg_user_id,
        "max_user_id": u.max_user_id,
        "balance_kop": int(u.tokens_balance or 0),
        "trial_ends_at": u.trial_ends_at.isoformat() if u.trial_ends_at else None,
        "is_verified": bool(u.is_verified),
    }


# ── /identify ──────────────────────────────────────────────────────────────


@router.post("/identify")
async def identify(request: Request) -> dict:
    """Найти или создать юзера по identifier.

    Body: {
        kind: "phone"|"vk_user_id"|"tg_user_id"|"max_user_id"|"email",
        value: str,
        display_name: str|None,    # имя для нового юзера если auto-create
        auto_create: bool=true,    # если не нашли — создать?
    }

    Response: {user_id, balance_kop, is_new, ...identifiers}
    """
    raw_body = await _verify_hmac(request)
    payload = _parse_body(raw_body)
    kind = (payload.get("kind") or "").strip()
    value = (payload.get("value") or "").strip()
    if kind not in _IDENT_KINDS:
        raise HTTPException(400, f"Unknown kind. Valid: {sorted(_IDENT_KINDS)}")
    if not value:
        raise HTTPException(400, "value required")

    display_name = (payload.get("display_name") or "").strip() or None
    auto_create = bool(payload.get("auto_create", True))

    with db_session() as db:
        existing = _find_user_by_identifier(db, kind, value)
        if existing:
            return {**_user_to_dict(existing), "is_new": False}

        if not auto_create:
            raise HTTPException(404, "User not found")

        # Auto-create. Email обязателен в схеме (UNIQUE NOT NULL), генерируем
        # синтетический по типу tg:123456@aiche.local — никто туда не пишет,
        # но constraint удовлетворён. Юзер может позже добавить настоящий email.
        synthetic_email = None
        if kind == "email":
            synthetic_email = value.lower().strip()
        else:
            slug = re.sub(r"[^a-z0-9]+", "", str(value).lower())[:30] or "anon"
            synthetic_email = f"{kind}-{slug}-{secrets.token_hex(4)}@aiche.local"

        # Trial-кредит для non-email auto-creates (TG/VK/MAX/phone).
        # Email-юзеры идут через verify-email flow и получают welcome-bonus там.
        from datetime import timedelta
        from server.pricing import get_price
        trial_kop = 0
        trial_ends = None
        if kind != "email":
            trial_kop = max(0, int(get_price("multi_surface.trial_credits_kop",
                                              default=50_000)))
            trial_days = max(0, int(get_price("multi_surface.trial_days",
                                                default=14)))
            if trial_days > 0:
                trial_ends = datetime.utcnow() + timedelta(days=trial_days)

        new_user = User(
            email=synthetic_email,
            password_hash="!",  # неактивный пароль — login только через линк
            name=display_name or kind.replace("_user_id", "").upper(),
            is_verified=False,
            is_active=True,
            agreed_to_terms=True,
            tokens_balance=trial_kop,
            trial_ends_at=trial_ends,
            referral_code=secrets.token_hex(4).upper(),
        )
        _set_identifier(new_user, kind, value)
        db.add(new_user)
        db.flush()  # получить user.id для Transaction

        # Лог trial-начисления отдельной транзакцией для audit-trail.
        if trial_kop > 0:
            db.add(Transaction(
                user_id=new_user.id,
                type="bonus",
                tokens_delta=trial_kop,
                description=f"[internal] trial credits on first identify via {kind}",
            ))
        db.commit()
        db.refresh(new_user)
        log.info(f"[internal] auto-created user_id={new_user.id} via {kind}={value} "
                 f"trial_kop={trial_kop}")
        return {**_user_to_dict(new_user), "is_new": True}


# ── /link ──────────────────────────────────────────────────────────────────


@router.post("/link")
async def link_identifier(request: Request) -> dict:
    """Добавить identifier к существующему юзеру.

    Body: {user_id, kind, value}
    Response: {ok, user: {...}}
    """
    raw_body = await _verify_hmac(request)
    payload = _parse_body(raw_body)
    user_id = payload.get("user_id")
    kind = (payload.get("kind") or "").strip()
    value = (payload.get("value") or "").strip()

    if not user_id or kind not in _IDENT_KINDS or not value:
        raise HTTPException(400, "user_id, kind, value required")
    if kind == "email":
        raise HTTPException(400, "email cannot be linked via this endpoint")

    with db_session() as db:
        u = db.query(User).filter_by(id=user_id).first()
        if not u:
            raise HTTPException(404, "User not found")
        # Проверка что identifier не занят другим юзером
        other = _find_user_by_identifier(db, kind, value)
        if other and other.id != u.id:
            raise HTTPException(409, f"{kind} already linked to another user")
        changed = _set_identifier(u, kind, value)
        if changed:
            db.commit()
        return {"ok": True, "user": _user_to_dict(u)}


# ── /link/code/issue + /link/code/redeem ───────────────────────────────────


@router.post("/link/code/issue")
async def link_code_issue(request: Request) -> dict:
    """Выдать одноразовый код привязки.

    Body: {user_id, kind}    # для какого канала ожидается redeem
    Response: {code, expires_in_sec}
    """
    from server.link_codes import issue_code, DEFAULT_TTL_SEC
    raw_body = await _verify_hmac(request)
    payload = _parse_body(raw_body)
    user_id = payload.get("user_id")
    kind = (payload.get("kind") or "").strip()
    if not user_id or kind not in _IDENT_KINDS:
        raise HTTPException(400, "user_id, kind required")
    try:
        code = issue_code(int(user_id), kind)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"code": code, "expires_in_sec": DEFAULT_TTL_SEC}


@router.post("/link/code/redeem")
async def link_code_redeem(request: Request) -> dict:
    """Обменять код на привязку.

    Body: {code, value}    # kind берётся из issue
    Response: {ok, user_id, kind}
    """
    from server.link_codes import redeem_code
    raw_body = await _verify_hmac(request)
    payload = _parse_body(raw_body)
    code = (payload.get("code") or "").strip()
    value = (payload.get("value") or "").strip()
    if not code or not value:
        raise HTTPException(400, "code, value required")
    result = redeem_code(code, value)
    if not result:
        raise HTTPException(404, "Code not found or expired")
    user_id, kind = result
    with db_session() as db:
        u = db.query(User).filter_by(id=user_id).first()
        if not u:
            raise HTTPException(404, "User not found")
        other = _find_user_by_identifier(db, kind, value)
        if other and other.id != u.id:
            raise HTTPException(409, f"{kind} already linked to another user")
        _set_identifier(u, kind, value)
        db.commit()
        return {"ok": True, "user_id": u.id, "kind": kind}


# ── /balance/{user_id} ─────────────────────────────────────────────────────


@router.get("/balance/{user_id}")
async def balance(user_id: int, request: Request) -> dict:
    """Текущий баланс юзера в копейках."""
    await _verify_hmac(request)
    with db_session() as db:
        u = db.query(User).filter_by(id=user_id).first()
        if not u:
            raise HTTPException(404, "User not found")
        return {
            "user_id": u.id,
            "balance_kop": int(u.tokens_balance or 0),
            "currency": "RUB",
            "trial_ends_at": u.trial_ends_at.isoformat() if u.trial_ends_at else None,
        }


# ── /debit + /credit ───────────────────────────────────────────────────────


def _idempotency_check(db: Session, user_id: int, key: str) -> Optional[dict]:
    """Если key уже встречался — вернёт сохранённый response. Иначе None."""
    rec = (db.query(IdempotencyRecord)
             .filter_by(user_id=user_id, key=key)
             .order_by(IdempotencyRecord.id.desc())
             .first())
    if not rec:
        return None
    # TTL ~5 мин (как и для обычной идемпотентности в /message)
    if rec.created_at < datetime.utcnow() - timedelta(minutes=10):
        return None
    if not rec.response_json:
        return None
    try:
        return json.loads(rec.response_json)
    except Exception:
        return None


def _idempotency_save(db: Session, user_id: int, key: str, response: dict) -> None:
    rec = IdempotencyRecord(
        user_id=user_id, key=key,
        response_json=json.dumps(response, ensure_ascii=False)[:50000],
    )
    db.add(rec)
    db.commit()


@router.post("/debit")
async def debit(request: Request) -> dict:
    """Атомарно списать копейки. Idempotency-key обязателен.

    Body: {user_id, amount_kop, reason, idempotency_key,
           resource_type?, resource_id?}
    Response success: {ok: true, new_balance_kop, transaction_id}
    Response fail:    {ok: false, reason: "insufficient_funds", balance_kop}
    """
    raw_body = await _verify_hmac(request)
    payload = _parse_body(raw_body)
    user_id = payload.get("user_id")
    amount = payload.get("amount_kop")
    reason = (payload.get("reason") or "").strip()
    idem_key = (payload.get("idempotency_key") or "").strip()
    resource_type = (payload.get("resource_type") or "").strip() or None
    resource_id = payload.get("resource_id")

    if not user_id:
        raise HTTPException(400, "user_id required")
    if not isinstance(amount, int) or amount <= 0:
        raise HTTPException(400, "amount_kop must be positive int")
    if not reason:
        raise HTTPException(400, "reason required")
    if not idem_key:
        raise HTTPException(400, "idempotency_key required")
    if amount > 10_000_000:  # > 100k ₽ за один debit — анти-абуз
        raise HTTPException(400, "amount too large")

    with db_session() as db:
        prev = _idempotency_check(db, int(user_id), idem_key)
        if prev is not None:
            return prev

        ok = deduct_strict(db, int(user_id), int(amount))
        if not ok:
            balance_kop = get_balance(db, int(user_id))
            resp = {"ok": False, "reason": "insufficient_funds",
                    "balance_kop": balance_kop}
            _idempotency_save(db, int(user_id), idem_key, resp)
            return resp

        # Логируем транзакцию
        tx = Transaction(
            user_id=int(user_id),
            type="usage",
            tokens_delta=-int(amount),
            description=f"[internal] {reason}",
        )
        db.add(tx)
        db.commit()
        new_balance = get_balance(db, int(user_id))
        resp = {"ok": True, "new_balance_kop": new_balance,
                "transaction_id": tx.id}
        _idempotency_save(db, int(user_id), idem_key, resp)
        return resp


@router.post("/credit")
async def credit(request: Request) -> dict:
    """Атомарно начислить копейки. Idempotency-key обязателен.

    Body: {user_id, amount_kop, source, idempotency_key, description?}
    source: "yookassa" | "vk_pay" | "promo" | "manual" | ...
    Response: {ok, new_balance_kop, transaction_id}
    """
    raw_body = await _verify_hmac(request)
    payload = _parse_body(raw_body)
    user_id = payload.get("user_id")
    amount = payload.get("amount_kop")
    source = (payload.get("source") or "").strip()
    idem_key = (payload.get("idempotency_key") or "").strip()
    description = (payload.get("description") or "").strip()

    if not user_id:
        raise HTTPException(400, "user_id required")
    if not isinstance(amount, int) or amount <= 0:
        raise HTTPException(400, "amount_kop must be positive int")
    if not source:
        raise HTTPException(400, "source required")
    if not idem_key:
        raise HTTPException(400, "idempotency_key required")
    if amount > 100_000_000:  # > 1M ₽ за один credit — анти-абуз
        raise HTTPException(400, "amount too large")

    with db_session() as db:
        prev = _idempotency_check(db, int(user_id), idem_key)
        if prev is not None:
            return prev

        ok = credit_atomic(db, int(user_id), int(amount))
        if not ok:
            raise HTTPException(404, "User not found")
        tx = Transaction(
            user_id=int(user_id),
            type="payment" if source in ("yookassa", "vk_pay") else "bonus",
            tokens_delta=int(amount),
            description=f"[internal:{source}] {description}"[:300],
        )
        db.add(tx)
        db.commit()
        new_balance = get_balance(db, int(user_id))
        resp = {"ok": True, "new_balance_kop": new_balance,
                "transaction_id": tx.id}
        _idempotency_save(db, int(user_id), idem_key, resp)
        return resp


# ── /pricing ───────────────────────────────────────────────────────────────


@router.get("/pricing")
async def pricing(request: Request) -> dict:
    """Текущие значения pricing_config + курс USD→RUB. Кэшируется клиентами 60 сек.

    Response: {keys: {key: value}, exchange_rate_usd_rub: float}
    """
    await _verify_hmac(request)
    with db_session() as db:
        rows = db.query(PricingConfig).all()
        keys = {r.key: r.value_kop for r in rows}
    # USD/RUB курс — через usd_rate
    try:
        from server.usd_rate import get_usd_rub
        rate = float(get_usd_rub() or 100.0)
    except Exception:
        rate = 100.0
    return {"keys": keys, "exchange_rate_usd_rub": rate}


# ── Public health ──────────────────────────────────────────────────────────


@router.get("/health")
def health() -> dict:
    """Без HMAC. Просто чтобы клиенты могли пингануть доступность."""
    return {"ok": True, "service": "aiche-internal", "version": "1"}
