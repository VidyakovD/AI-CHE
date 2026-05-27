"""
Public API для SaaS-интеграций.

Endpoints:
  - /api-tokens/* — управление токенами в кабинете (требует обычной auth)
  - /api/v1/* — public endpoints, авторизация через ApiToken header

Auth header: `Authorization: Bearer ai_che_<prefix>_<secret>`.
prefix — публичная часть (~8 hex), secret — приватная (~32 hex).
В БД храним только sha256(secret); сравнение через hmac.compare_digest.

Списания: каждый AI-вызов через public API списывается с tokens_balance юзера
(тот же что и для /message). Без баланса → 402.

Scope-проверка: если в токене проставлены scopes — endpoint должен входить
в whitelist. Иначе доступны все группы.
"""
import logging
import hashlib
import hmac
import secrets
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Header, Request, Body
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from server.routes.deps import get_db, current_user
from server.models import (
    User, ApiToken, ApiWebhook, ProposalProject, ProposalBrand,
)

log = logging.getLogger(__name__)


# ── Управление токенами в кабинете ─────────────────────────────────────────

mgmt_router = APIRouter(prefix="/api-tokens", tags=["api-tokens"])


_VALID_SCOPES = {"proposals", "knowledge", "solutions", "bots"}


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class TokenCreateBody(BaseModel):
    name: str = Field(..., max_length=80)
    scopes: list[str] | None = None  # пусто/null = все


@mgmt_router.post("")
def token_create(body: TokenCreateBody, db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    """Создать новый API-токен. Полный секрет показывается только при
    создании (потом только prefix)."""
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email")
    name = (body.name or "").strip()[:80]
    if len(name) < 2:
        raise HTTPException(400, "Минимум 2 символа в имени")
    cnt = db.query(ApiToken).filter_by(user_id=user.id, is_active=True).count()
    if cnt >= 10:
        raise HTTPException(409, "Лимит 10 активных токенов")

    scopes_str: str | None = None
    if body.scopes:
        valid = [s for s in body.scopes if s in _VALID_SCOPES]
        if valid:
            scopes_str = ",".join(sorted(set(valid)))

    prefix = secrets.token_hex(4)   # 8 hex
    secret = secrets.token_hex(24)  # 48 hex — итого 56-char уникальный
    full = f"ai_che_{prefix}_{secret}"

    t = ApiToken(
        user_id=user.id, name=name,
        prefix=prefix, secret_hash=_hash_secret(secret),
        scopes=scopes_str, is_active=True,
    )
    db.add(t); db.commit(); db.refresh(t)
    try:
        from server.audit_log import log_action
        log_action("api_token.created", user_id=user.id,
                   target_type="api_token", target_id=str(t.id),
                   details={"name": name, "scopes": scopes_str or "all"})
    except Exception:
        pass
    return {
        "id": t.id, "name": t.name, "prefix": t.prefix,
        "scopes": scopes_str, "secret": full,
        "warning": "Сохрани секрет — больше его не увидишь!",
    }


@mgmt_router.get("")
def token_list(db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    rows = (db.query(ApiToken).filter_by(user_id=user.id)
              .order_by(ApiToken.id.desc()).all())
    return [{
        "id": t.id, "name": t.name, "prefix": t.prefix,
        "scopes": t.scopes, "is_active": bool(t.is_active),
        "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None,
        "requests_count": t.requests_count or 0,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    } for t in rows]


@mgmt_router.delete("/{token_id}")
def token_revoke(token_id: int, db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    t = db.query(ApiToken).filter_by(id=token_id, user_id=user.id).first()
    if not t:
        raise HTTPException(404, "Токен не найден")
    t.is_active = False
    db.commit()
    try:
        from server.audit_log import log_action
        log_action("api_token.revoked", user_id=user.id,
                   target_type="api_token", target_id=str(token_id))
    except Exception:
        pass
    return {"status": "revoked"}


# ── Webhooks management ─────────────────────────────────────────────────────
# Юзер регистрирует свой URL и получает на него POST'ы при событиях.
# События: см. _WEBHOOK_EVENTS. HMAC-подпись в заголовке X-Aiche-Signature.

_WEBHOOK_EVENTS = {
    "proposal.opened", "proposal.sent", "proposal.signed",
    "record.created",
    "solution.done",
    "site.done", "site.failed",
}


class WebhookCreateBody(BaseModel):
    url: str = Field(..., max_length=2000)
    events: list[str]
    description: str | None = Field(None, max_length=200)


def _validate_webhook_url(url: str) -> str:
    """Проверка URL для webhook'а: http(s), не private/localhost.

    Делегирует общую логику в server.proposal_builder.validate_outbound_url
    которая делает DNS-резолв (защита от DNS rebinding) и покрывает
    IPv6/decimal IPv4 — раньше regex по lowered URL пропускал эти случаи.
    """
    from server.proposal_builder import validate_outbound_url
    try:
        return validate_outbound_url(url, allow_http=True)
    except ValueError as e:
        raise HTTPException(400, str(e))


@mgmt_router.post("/webhooks")
def webhook_create(body: WebhookCreateBody, db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    """Зарегистрировать новый webhook. Возвращает secret — сохраните
    его, он понадобится для верификации входящих сигнатур."""
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email")
    cnt = db.query(ApiWebhook).filter_by(user_id=user.id, is_active=True).count()
    if cnt >= 10:
        raise HTTPException(409, "Лимит 10 активных webhooks")
    url = _validate_webhook_url(body.url)
    valid_events = [e for e in (body.events or []) if e in _WEBHOOK_EVENTS]
    if not valid_events:
        raise HTTPException(400,
            f"Минимум одно событие из: {sorted(_WEBHOOK_EVENTS)}")
    secret = secrets.token_hex(16)  # 32 hex
    w = ApiWebhook(
        user_id=user.id, url=url, secret=secret,
        events=",".join(sorted(set(valid_events))),
        description=(body.description or "").strip()[:200] or None,
    )
    db.add(w); db.commit(); db.refresh(w)
    try:
        from server.audit_log import log_action
        log_action("api_webhook.created", user_id=user.id,
                   target_type="webhook", target_id=str(w.id),
                   details={"url": url[:100], "events": valid_events})
    except Exception:
        pass
    return {
        "id": w.id, "url": w.url, "events": valid_events,
        "secret": secret,
        "warning": "Сохраните secret — он понадобится для проверки входящих X-Aiche-Signature.",
    }


@mgmt_router.get("/webhooks")
def webhook_list(db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    rows = (db.query(ApiWebhook).filter_by(user_id=user.id)
              .order_by(ApiWebhook.id.desc()).all())
    return [{
        "id": w.id, "url": w.url,
        "events": (w.events or "").split(","),
        "description": w.description,
        "is_active": bool(w.is_active),
        "last_status": w.last_status,
        "last_called_at": w.last_called_at.isoformat() if w.last_called_at else None,
        "last_error": (w.last_error or "")[:200] or None,
        "fail_count": w.fail_count or 0,
        "total_calls": w.total_calls or 0,
        "created_at": w.created_at.isoformat() if w.created_at else None,
    } for w in rows]


@mgmt_router.delete("/webhooks/{webhook_id}")
def webhook_delete(webhook_id: int, db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    w = db.query(ApiWebhook).filter_by(id=webhook_id, user_id=user.id).first()
    if not w:
        raise HTTPException(404, "Webhook не найден")
    db.delete(w)
    db.commit()
    return {"status": "deleted"}


@mgmt_router.post("/webhooks/{webhook_id}/test")
def webhook_test(webhook_id: int, db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    """Послать тестовый вызов на этот webhook. Полезно проверить что URL
    корректный и подпись верифицируется. Не привязан к реальному событию."""
    w = db.query(ApiWebhook).filter_by(id=webhook_id, user_id=user.id).first()
    if not w:
        raise HTTPException(404, "Webhook не найден")
    from server.webhooks import deliver_webhook
    payload = {
        "event": "test.ping",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "data": {"message": "Это тестовое событие. Если получили — всё работает!"},
    }
    res = deliver_webhook(w.id, payload, sync=True)
    return res


# ── Public API endpoints (auth via X-API-Key) ──────────────────────────────

api_router = APIRouter(prefix="/api/v1", tags=["public-api"])


def authenticate_token(request: Request, db: Session,
                        required_scope: str | None = None) -> User:
    """Извлекает токен из Authorization: Bearer ai_che_<prefix>_<secret>,
    проверяет, обновляет last_used_at, возвращает User."""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Authorization: Bearer ai_che_<token> required")
    raw = auth[7:].strip()
    if not raw.startswith("ai_che_"):
        raise HTTPException(401, "Invalid token format")
    parts = raw.split("_")
    # формат: ai_che_<prefix>_<secret>  → ['ai','che','<prefix>','<secret>']
    if len(parts) < 4:
        raise HTTPException(401, "Malformed token")
    prefix = parts[2]
    secret = "_".join(parts[3:])
    if len(prefix) != 8 or len(secret) < 16:
        raise HTTPException(401, "Bad token structure")
    t = db.query(ApiToken).filter_by(prefix=prefix, is_active=True).first()
    if not t:
        raise HTTPException(401, "Token not found or revoked")
    expected_hash = _hash_secret(secret)
    if not hmac.compare_digest(expected_hash, t.secret_hash):
        raise HTTPException(401, "Invalid token")
    if required_scope and t.scopes:
        scopes = {s.strip() for s in t.scopes.split(",") if s.strip()}
        if required_scope not in scopes:
            raise HTTPException(403, f"Token has no scope '{required_scope}'")
    # Сохраняем scopes в request.state для последующего scope-check внутри
    # endpoint (например, MCP tools/call где scope зависит от tool name —
    # см. server/routes/mcp.py:_handle_tools_call).
    # None означает «все scope разрешены» (legacy токены без CSV).
    # strip() обязательно: иначе "proposals, solutions" даёт {"proposals", " solutions"}
    # и токен с пробелом в CSV обходит scope-check на " solutions".
    try:
        request.state.token_scopes = (
            {s.strip() for s in t.scopes.split(",") if s.strip()} if t.scopes else None
        )
    except Exception:
        pass
    user = db.query(User).filter_by(id=t.user_id, is_active=True).first()
    if not user or user.is_banned:
        raise HTTPException(403, "User account inactive")
    if not user.is_verified:
        # Если юзер был ре-верифицирован после issue токена — отзываем
        # доступ автоматически. Защита от использования API заброшенным
        # аккаунтом, у которого админ снял verified.
        raise HTTPException(403, "Email not verified")
    # Метрики (atomic UPDATE — без race в multi-worker).
    try:
        from sqlalchemy import update as _sql_update
        db.execute(
            _sql_update(ApiToken).where(ApiToken.id == t.id).values(
                last_used_at=datetime.utcnow(),
                requests_count=(ApiToken.requests_count + 1),
            )
        )
        db.commit()
    except Exception:
        db.rollback()
    return user


# ── /api/v1/proposals/generate ────────────────────────────────────────────


class PublicProposalBody(BaseModel):
    name: str
    client_name: str | None = None
    client_email: str | None = None
    client_request: str
    client_site_url: str | None = None
    extra_notes: str | None = None
    brand_id: int | None = None
    price_list_id: int | None = None
    header_layout: str | None = None


@api_router.post("/proposals/generate")
async def public_generate_proposal(body: PublicProposalBody,
                                     request: Request,
                                     db: Session = Depends(get_db)):
    """Создать + сгенерировать КП через API. Возвращает proposal_id + PDF URL.

    Эквивалент того, что делает фронт через /proposals/projects + generate.
    Списание: 50 ₽ (или цена из pricing_config).
    """
    user = authenticate_token(request, db, required_scope="proposals")
    if not body.name or not body.client_request:
        raise HTTPException(400, "name и client_request обязательны")
    if body.brand_id:
        b = db.query(ProposalBrand).filter_by(
            id=body.brand_id, user_id=user.id).first()
        if not b:
            raise HTTPException(404, "Бренд не найден")

    layout = (body.header_layout or "classic").strip().lower()
    if layout not in ("classic", "banner", "centered", "minimal"):
        layout = "classic"

    p = ProposalProject(
        user_id=user.id, name=body.name[:200],
        brand_id=body.brand_id, price_list_id=body.price_list_id,
        client_name=(body.client_name or "")[:200] or None,
        client_email=(body.client_email or "")[:254] or None,
        client_request=body.client_request[:20000],
        client_site_url=(body.client_site_url or "")[:500] or None,
        extra_notes=(body.extra_notes or "")[:5000] or None,
        header_layout=layout, status="draft",
    )
    db.add(p); db.commit(); db.refresh(p)

    # Генерация — синхронно, чтобы API возвращал готовый результат
    from server.proposal_builder import generate_proposal
    from server.billing import deduct_strict
    from server.pricing import get_price
    cost = int(get_price("proposal.create", default=5000))
    if not deduct_strict(db, user.id, cost):
        raise HTTPException(402, f"Недостаточно средств ({cost/100:.0f} ₽)")
    try:
        result = generate_proposal(db, p)
    except Exception as e:
        # Refund при ошибке AI
        from server.billing import credit_atomic
        credit_atomic(db, user.id, cost)
        db.commit()
        log.error(f"[api] proposal generate failed: {e}")
        raise HTTPException(503, f"Ошибка генерации: {type(e).__name__}")
    p.generated_html = result["html"]
    p.generated_pdf = result["pdf_path"]
    p.price_kop = cost
    p.status = "done"
    db.commit()
    try:
        from server.audit_log import log_action
        log_action("api.proposal_generated", user_id=user.id,
                   target_type="proposal", target_id=str(p.id),
                   details={"via": "public_api"})
    except Exception:
        pass
    return {
        "proposal_id": p.id,
        "name": p.name,
        "pdf_url": result["pdf_path"],
        "cost_kop": cost,
    }


@api_router.get("/proposals/{proposal_id}")
def public_get_proposal(proposal_id: int, request: Request,
                          db: Session = Depends(get_db)):
    """Получить статус и URL PDF существующего КП."""
    user = authenticate_token(request, db, required_scope="proposals")
    p = db.query(ProposalProject).filter_by(
        id=proposal_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "КП не найдено")
    return {
        "id": p.id, "name": p.name, "status": p.status,
        "pdf_url": p.generated_pdf,
        "client_name": p.client_name,
        "crm_stage": p.crm_stage,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


# ── /api/v1/me ────────────────────────────────────────────────────────────


@api_router.get("/me")
def public_me(request: Request, db: Session = Depends(get_db)):
    """Кто я (по токену) + остаток баланса."""
    user = authenticate_token(request, db)
    return {
        "user_id": user.id,
        "email": user.email,
        "balance_kop": int(user.tokens_balance or 0),
        "balance_rub": round(int(user.tokens_balance or 0) / 100, 2),
    }
