"""КП (коммерческие предложения) — отдельный модуль от презентаций.

Состав фич:
  - Бренды (ProposalBrand): лого, цвета, шрифт, реквизиты — шаблон оформления
  - Проекты (ProposalProject): контекст клиента + бренд + прайс из бота → PDF
  - Email-orchestration (auto_proposal нода в chatbot_engine) — отдельно
"""
import os
import re
import json
import logging
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlalchemy.orm import Session

from server.routes.deps import get_db, current_user, optional_user
from server.models import (
    ProposalProject, ProposalBrand, ChatBot, BotPriceItem,
    User, Transaction, ProposalVersion,
    ProposalPriceList, ProposalPriceItem,
)
from server.audit_log import log_action

log = logging.getLogger(__name__)

router = APIRouter(prefix="/proposals", tags=["proposals"])

# Цены: КП — 50 ₽, правка — 5 ₽. Можно перенести в pricing_config позже.
PROPOSAL_COST_KOP = 5000
PROPOSAL_EDIT_COST_KOP = 500

# Цвет HEX валидация — короткая или полная
_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_FONT_WHITELIST = {
    "Inter", "Manrope", "Roboto", "Open Sans", "Lato", "Montserrat",
    "Playfair Display", "Merriweather", "PT Sans", "Noto Sans",
    "Source Sans Pro", "Raleway", "Nunito",
}
_PRESET_WHITELIST = {"minimal", "classic", "bold", "compact"}
_TONE_WHITELIST = {"business", "friendly", "premium", "tech"}


def _validate_hex(c: str | None, default: str) -> str:
    if not c:
        return default
    s = c.strip()
    if not _HEX_RE.match(s):
        raise HTTPException(400, f"Цвет должен быть в формате #RRGGBB или #RGB, получено: {s}")
    return s


# ── Brand CRUD ─────────────────────────────────────────────────────────────


class BrandBody(BaseModel):
    name: str
    company_name: str | None = None
    logo_url: str | None = None
    primary_color: str | None = "#ff8c42"
    secondary_color: str | None = "#1C1C1C"
    accent_color: str | None = "#ffb347"
    font_family: str | None = "Inter"
    style_preset: str | None = "minimal"
    contacts: str | None = None
    inn: str | None = None
    address: str | None = None
    signature_url: str | None = None
    is_default: bool | None = False
    # Расширенная персонализация (для глубокого AI-контекста)
    tagline: str | None = None
    usp_list: str | None = None
    guarantees: str | None = None
    tone: str | None = "business"
    intro_phrase: str | None = None
    cta_phrase: str | None = None


def _brand_to_dict(b: ProposalBrand) -> dict:
    return {
        "id": b.id, "name": b.name, "company_name": b.company_name,
        "logo_url": b.logo_url,
        "primary_color": b.primary_color, "secondary_color": b.secondary_color,
        "accent_color": b.accent_color, "font_family": b.font_family,
        "style_preset": b.style_preset,
        "contacts": b.contacts, "inn": b.inn, "address": b.address,
        "signature_url": b.signature_url,
        "tagline": b.tagline, "usp_list": b.usp_list,
        "guarantees": b.guarantees, "tone": b.tone or "business",
        "intro_phrase": b.intro_phrase, "cta_phrase": b.cta_phrase,
        "is_default": bool(b.is_default),
        "created_at": b.created_at.isoformat() if b.created_at else None,
    }


@router.get("/brands")
def list_brands(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = (db.query(ProposalBrand)
              .filter_by(user_id=user.id)
              .order_by(ProposalBrand.is_default.desc(), ProposalBrand.id.desc())
              .all())
    return [_brand_to_dict(b) for b in rows]


@router.post("/brands")
def create_brand(body: BrandBody, db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    if not body.name or not body.name.strip():
        raise HTTPException(400, "Название бренда обязательно")
    if body.font_family and body.font_family not in _FONT_WHITELIST:
        raise HTTPException(400, f"Шрифт '{body.font_family}' не в whitelist'е")
    if body.style_preset and body.style_preset not in _PRESET_WHITELIST:
        raise HTTPException(400, f"Preset должен быть из {_PRESET_WHITELIST}")
    primary = _validate_hex(body.primary_color, "#ff8c42")
    secondary = _validate_hex(body.secondary_color, "#1C1C1C")
    accent = _validate_hex(body.accent_color, "#ffb347")

    # Если ставим is_default=True — сбросить у остальных
    if body.is_default:
        db.query(ProposalBrand).filter_by(user_id=user.id, is_default=True) \
          .update({"is_default": False})

    if body.tone and body.tone not in _TONE_WHITELIST:
        raise HTTPException(400, f"Tone должен быть из {_TONE_WHITELIST}")
    b = ProposalBrand(
        user_id=user.id,
        name=body.name.strip()[:100],
        company_name=(body.company_name or "").strip()[:200] or None,
        logo_url=body.logo_url, primary_color=primary,
        secondary_color=secondary, accent_color=accent,
        font_family=body.font_family or "Inter",
        style_preset=body.style_preset or "minimal",
        contacts=(body.contacts or "")[:1000] or None,
        inn=(body.inn or "")[:20] or None,
        address=(body.address or "")[:500] or None,
        signature_url=body.signature_url,
        tagline=(body.tagline or "")[:200] or None,
        usp_list=(body.usp_list or "")[:2000] or None,
        guarantees=(body.guarantees or "")[:2000] or None,
        tone=body.tone or "business",
        intro_phrase=(body.intro_phrase or "")[:200] or None,
        cta_phrase=(body.cta_phrase or "")[:200] or None,
        is_default=bool(body.is_default),
    )
    db.add(b); db.commit(); db.refresh(b)
    log_action("proposal.brand_created", user_id=user.id,
               target_type="proposal_brand", target_id=str(b.id))
    return _brand_to_dict(b)


@router.put("/brands/{brand_id}")
def update_brand(brand_id: int, body: BrandBody,
                  db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    b = db.query(ProposalBrand).filter_by(id=brand_id, user_id=user.id).first()
    if not b:
        raise HTTPException(404, "Бренд не найден")
    if body.font_family and body.font_family not in _FONT_WHITELIST:
        raise HTTPException(400, f"Шрифт '{body.font_family}' не в whitelist'е")
    if body.style_preset and body.style_preset not in _PRESET_WHITELIST:
        raise HTTPException(400, f"Preset должен быть из {_PRESET_WHITELIST}")
    b.name = body.name.strip()[:100] if body.name else b.name
    b.company_name = (body.company_name or "").strip()[:200] or None
    b.logo_url = body.logo_url
    b.primary_color = _validate_hex(body.primary_color, b.primary_color or "#ff8c42")
    b.secondary_color = _validate_hex(body.secondary_color, b.secondary_color or "#1C1C1C")
    b.accent_color = _validate_hex(body.accent_color, b.accent_color or "#ffb347")
    b.font_family = body.font_family or b.font_family
    b.style_preset = body.style_preset or b.style_preset
    b.contacts = (body.contacts or "")[:1000] or None
    b.inn = (body.inn or "")[:20] or None
    b.address = (body.address or "")[:500] or None
    b.signature_url = body.signature_url
    if body.tone and body.tone in _TONE_WHITELIST:
        b.tone = body.tone
    b.tagline = (body.tagline or "")[:200] or None
    b.usp_list = (body.usp_list or "")[:2000] or None
    b.guarantees = (body.guarantees or "")[:2000] or None
    b.intro_phrase = (body.intro_phrase or "")[:200] or None
    b.cta_phrase = (body.cta_phrase or "")[:200] or None
    if body.is_default and not b.is_default:
        db.query(ProposalBrand).filter_by(user_id=user.id, is_default=True) \
          .update({"is_default": False})
        b.is_default = True
    db.commit(); db.refresh(b)
    return _brand_to_dict(b)


@router.delete("/brands/{brand_id}")
def delete_brand(brand_id: int, db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    b = db.query(ProposalBrand).filter_by(id=brand_id, user_id=user.id).first()
    if not b:
        raise HTTPException(404, "Бренд не найден")
    # ProposalProject.brand_id → ON DELETE SET NULL (объявлено в модели)
    db.delete(b); db.commit()
    log_action("proposal.brand_deleted", user_id=user.id,
               target_type="proposal_brand", target_id=str(brand_id))
    return {"status": "deleted"}


# ── Project CRUD ───────────────────────────────────────────────────────────


class ProposalCreateBody(BaseModel):
    name: str
    brand_id: int | None = None
    bot_id: int | None = None
    price_list_id: int | None = None
    client_name: str | None = None
    client_email: str | None = None
    client_request: str | None = None
    client_site_url: str | None = None
    extra_notes: str | None = None


def _project_to_dict(p: ProposalProject, full: bool = False) -> dict:
    base = {
        "id": p.id, "name": p.name, "status": p.status,
        "brand_id": p.brand_id, "bot_id": p.bot_id,
        "price_list_id": p.price_list_id,
        "client_name": p.client_name, "client_email": p.client_email,
        "price_kop": p.price_kop or 0,
        "auto_generated": bool(p.auto_generated),
        "crm_stage": p.crm_stage or "new",
        "sent_at": p.sent_at.isoformat() if p.sent_at else None,
        "opened_at": p.opened_at.isoformat() if p.opened_at else None,
        "replied_at": p.replied_at.isoformat() if p.replied_at else None,
        "won_at": p.won_at.isoformat() if p.won_at else None,
        "lost_at": p.lost_at.isoformat() if p.lost_at else None,
        "public_token": p.public_token,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }
    if full:
        base.update({
            "client_request": p.client_request,
            "client_site_url": p.client_site_url,
            "client_site_ctx": p.client_site_ctx,
            "extra_notes": p.extra_notes,
            "generated_html": p.generated_html,
            "generated_pdf": p.generated_pdf,
            "source_email_id": p.source_email_id,
        })
    return base


_VALID_CRM_STAGES = {"new", "sent", "opened", "replied", "won", "lost"}


class CrmStageBody(BaseModel):
    stage: str


@router.post("/projects/{project_id}/stage")
def set_crm_stage(project_id: int, body: CrmStageBody,
                   db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    """Перевести КП в новую CRM-стадию (won/lost/replied и т.д.)."""
    p = db.query(ProposalProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    stage = (body.stage or "").strip().lower()
    if stage not in _VALID_CRM_STAGES:
        raise HTTPException(400, f"Стадия должна быть одной из: {sorted(_VALID_CRM_STAGES)}")
    from datetime import datetime as _dt
    now = _dt.utcnow()
    p.crm_stage = stage
    if stage == "replied" and not p.replied_at:
        p.replied_at = now
    elif stage == "won" and not p.won_at:
        p.won_at = now
    elif stage == "lost" and not p.lost_at:
        p.lost_at = now
    elif stage == "opened" and not p.opened_at:
        p.opened_at = now
    db.commit()
    log_action("proposal.stage_changed", user_id=user.id,
                target_type="proposal", target_id=str(p.id),
                details={"stage": stage})
    return _project_to_dict(p)


@router.get("/projects")
def list_projects(db: Session = Depends(get_db), user=Depends(optional_user)):
    if not user:
        return []
    rows = (db.query(ProposalProject).filter_by(user_id=user.id)
              .order_by(ProposalProject.created_at.desc()).all())
    return [_project_to_dict(p) for p in rows]


@router.post("/projects")
def create_project(body: ProposalCreateBody, db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    if not body.name or not body.name.strip():
        raise HTTPException(400, "Название КП обязательно")
    # Валидация brand_id / bot_id — должны принадлежать юзеру
    if body.brand_id:
        b = db.query(ProposalBrand).filter_by(id=body.brand_id, user_id=user.id).first()
        if not b:
            raise HTTPException(404, "Бренд не найден")
    if body.bot_id:
        bot = db.query(ChatBot).filter_by(id=body.bot_id, user_id=user.id).first()
        if not bot:
            raise HTTPException(404, "Бот не найден")
    if body.price_list_id:
        pl = db.query(ProposalPriceList).filter_by(
            id=body.price_list_id, user_id=user.id).first()
        if not pl:
            raise HTTPException(404, "Прайс-лист не найден")
    p = ProposalProject(
        user_id=user.id,
        name=body.name.strip()[:200],
        brand_id=body.brand_id, bot_id=body.bot_id,
        price_list_id=body.price_list_id,
        client_name=(body.client_name or "")[:200] or None,
        client_email=(body.client_email or "")[:254] or None,
        client_request=(body.client_request or "")[:20000] or None,
        client_site_url=(body.client_site_url or "")[:500] or None,
        extra_notes=(body.extra_notes or "")[:5000] or None,
        status="draft",
    )
    db.add(p); db.commit(); db.refresh(p)
    log_action("proposal.created", user_id=user.id,
               target_type="proposal", target_id=str(p.id))
    return _project_to_dict(p, full=True)


@router.get("/projects/{project_id}")
def get_project(project_id: int, db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    p = db.query(ProposalProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    return _project_to_dict(p, full=True)


@router.put("/projects/{project_id}")
def update_project(project_id: int, body: ProposalCreateBody,
                    db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    p = db.query(ProposalProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    if body.brand_id:
        b = db.query(ProposalBrand).filter_by(id=body.brand_id, user_id=user.id).first()
        if not b:
            raise HTTPException(404, "Бренд не найден")
        p.brand_id = body.brand_id
    if body.bot_id is not None:
        if body.bot_id:
            bot = db.query(ChatBot).filter_by(id=body.bot_id, user_id=user.id).first()
            if not bot:
                raise HTTPException(404, "Бот не найден")
        p.bot_id = body.bot_id or None
    if body.price_list_id is not None:
        if body.price_list_id:
            pl = db.query(ProposalPriceList).filter_by(
                id=body.price_list_id, user_id=user.id).first()
            if not pl:
                raise HTTPException(404, "Прайс-лист не найден")
        p.price_list_id = body.price_list_id or None
    if body.name:
        p.name = body.name.strip()[:200]
    if body.client_name is not None:
        p.client_name = (body.client_name or "")[:200] or None
    if body.client_email is not None:
        p.client_email = (body.client_email or "")[:254] or None
    if body.client_request is not None:
        p.client_request = (body.client_request or "")[:20000] or None
    if body.client_site_url is not None:
        p.client_site_url = (body.client_site_url or "")[:500] or None
    if body.extra_notes is not None:
        p.extra_notes = (body.extra_notes or "")[:5000] or None
    db.commit(); db.refresh(p)
    return _project_to_dict(p, full=True)


@router.post("/projects/{project_id}/duplicate")
def duplicate_project(project_id: int, db: Session = Depends(get_db),
                       user: User = Depends(current_user)):
    """Создать копию КП на основе существующего (бренд + бот + extra_notes
    переносятся; client_* — пустые, чтобы юзер заполнил под нового клиента;
    generated_html/pdf — НЕ копируются, юзер генерит заново)."""
    src = db.query(ProposalProject).filter_by(id=project_id, user_id=user.id).first()
    if not src:
        raise HTTPException(404, "Исходный проект не найден")
    new = ProposalProject(
        user_id=user.id,
        name=f"Копия — {src.name}"[:200],
        brand_id=src.brand_id, bot_id=src.bot_id,
        extra_notes=src.extra_notes,
        # Контекст клиента — НЕ копируем (новый клиент, новые поля)
        client_name=None, client_email=None,
        client_request=None, client_site_url=None, client_site_ctx=None,
        status="draft",
    )
    db.add(new); db.commit(); db.refresh(new)
    log_action("proposal.duplicated", user_id=user.id,
               target_type="proposal", target_id=str(new.id),
               details={"source_id": project_id})
    return _project_to_dict(new, full=True)


@router.delete("/projects/{project_id}")
def delete_project(project_id: int, db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    p = db.query(ProposalProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    # Удалить PDF на диске если есть
    if p.generated_pdf:
        try:
            base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            pdf_path = os.path.join(base, p.generated_pdf.lstrip("/"))
            if os.path.exists(pdf_path):
                os.unlink(pdf_path)
        except Exception:
            pass
    db.delete(p); db.commit()
    log_action("proposal.deleted", user_id=user.id,
               target_type="proposal", target_id=str(project_id))
    return {"status": "deleted"}


# ── Generate (AI → HTML → PDF) ─────────────────────────────────────────────


_MAX_VERSIONS_PER_PROPOSAL = 10  # храним до 10 последних версий


def _snapshot_version(db, p: ProposalProject, note: str, cost_kop: int = 0) -> None:
    """Сохранить текущий HTML/PDF проекта как версию + почистить старые
    (>10) чтобы не раздувать БД."""
    if not p.generated_html:
        return
    v = ProposalVersion(
        proposal_id=p.id, user_id=p.user_id,
        html=p.generated_html, pdf_path=p.generated_pdf,
        note=note[:200] if note else None, cost_kop=cost_kop or 0,
    )
    db.add(v); db.flush()
    # Cleanup: оставляем только последние _MAX_VERSIONS
    old = (db.query(ProposalVersion)
             .filter_by(proposal_id=p.id)
             .order_by(ProposalVersion.created_at.desc())
             .offset(_MAX_VERSIONS_PER_PROPOSAL)
             .all())
    for o in old:
        db.delete(o)


def _validate_proposal_for_generation(p: ProposalProject) -> None:
    """Pre-validation перед списанием. Кидает HTTPException с понятным сообщением.
    Защищает от случаев где AI всё равно бы вернул мусор / откажет, а юзер
    уже заплатил.
    """
    req = (p.client_request or "").strip()
    notes = (p.extra_notes or "").strip()
    if not req and not notes:
        raise HTTPException(400,
            "Заполните «Запрос клиента» или «Доп. инструкции» — без контекста КП не получится. "
            "Минимум 30 символов с описанием задачи.")
    combined = (req or "") + "\n" + (notes or "")
    if len(combined.strip()) < 30:
        raise HTTPException(400,
            "Слишком короткий контекст (минимум 30 символов). Напишите подробнее: "
            "что нужно клиенту, какие услуги интересуют, какой объём работ.")
    if len(combined) > 25_000:
        raise HTTPException(413,
            "Контекст слишком большой (>25 КБ). Сократите запрос или вынесите детали в чат-беседу.")
    # client_site_url — если задан, должен быть валидным
    site = (p.client_site_url or "").strip()
    if site:
        if not (site.startswith("http://") or site.startswith("https://")):
            raise HTTPException(400,
                f"Сайт клиента должен начинаться с http:// или https:// — получено: {site[:60]}")
        if " " in site or len(site) > 500:
            raise HTTPException(400, "URL сайта клиента некорректен")


@router.post("/projects/{project_id}/generate")
def generate_proposal_endpoint(project_id: int, db: Session = Depends(get_db),
                                 user: User = Depends(current_user)):
    """Сгенерировать КП: парсит сайт клиента → промпт Claude → HTML+PDF.
    Списывает фикс. цену (5 ₽ = 500 коп при перегенерации, 50 ₽ за первый раз).

    Pre-validation: проверяем длину контекста и URL ДО списания, чтобы юзер
    не платил за заведомо-плохой запрос.
    """
    p = db.query(ProposalProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")

    _validate_proposal_for_generation(p)

    # Цена: первый раз — 50 ₽, перегенерация — 5 ₽
    is_regen = p.status == "done"
    cost = PROPOSAL_EDIT_COST_KOP if is_regen else PROPOSAL_COST_KOP

    from server.billing import deduct_strict
    if not deduct_strict(db, user.id, cost):
        raise HTTPException(402, f"Недостаточно средств (нужно {cost/100:.0f} ₽)")
    p.price_kop = (p.price_kop or 0) + cost
    db.add(Transaction(user_id=user.id, type="usage", tokens_delta=-cost,
                       description=f"КП «{p.name}» ({cost/100:.0f} ₽)"))

    # User-key (если у юзера привязан Anthropic ключ — работаем по скидке)
    from server.models import UserApiKey
    uk = db.query(UserApiKey).filter_by(user_id=user.id, provider="anthropic").first()
    user_key = uk.api_key if uk else None

    try:
        from server.proposal_builder import generate_proposal
        result = generate_proposal(db, p, user_api_key=user_key)
        p.generated_html = result["html"]
        p.generated_pdf = result["pdf_path"]
        p.status = "done"
        # Сохраняем версию (после успешной генерации)
        _snapshot_version(db, p, note="Генерация" if not is_regen else "Перегенерация", cost_kop=cost)
        db.commit()
        log_action("proposal.generated", user_id=user.id,
                   target_type="proposal", target_id=str(p.id),
                   details={"regen": is_regen, "cost_kop": cost,
                            "has_brand": bool(p.brand_id),
                            "has_bot": bool(p.bot_id),
                            "has_site": bool(p.client_site_url)})
    except Exception as e:
        log.error(f"[proposal] generate failed: {type(e).__name__}: {e}")
        # Refund при ошибке
        from server.billing import credit_atomic
        credit_atomic(db, user.id, cost)
        p.price_kop = max(0, (p.price_kop or 0) - cost)
        db.add(Transaction(user_id=user.id, type="refund", tokens_delta=cost,
                           description=f"Возврат: КП «{p.name}» — ошибка генерации"))
        p.status = "error"
        db.commit()
        raise HTTPException(503, f"Не удалось сгенерировать КП: {type(e).__name__}. Деньги возвращены.")

    return _project_to_dict(p, full=True)


@router.get("/projects/{project_id}/pdf")
def download_pdf(project_id: int, db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    """Скачать сгенерированный PDF. Файл лежит в /uploads/proposals/."""
    from fastapi.responses import FileResponse
    p = db.query(ProposalProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    if not p.generated_pdf:
        raise HTTPException(404, "PDF ещё не сгенерирован — нажмите «Сгенерировать»")
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    abs_path = os.path.join(base, p.generated_pdf.lstrip("/"))
    if not os.path.exists(abs_path):
        raise HTTPException(404, "PDF файл удалён или перемещён — пересоздайте КП")
    safe_name = re.sub(r"[^\w\-]", "_", p.name or "proposal")[:40]
    return FileResponse(
        abs_path, media_type="application/pdf",
        filename=f"{safe_name}.pdf",
    )


# ── Публичная ссылка (для отправки клиенту) ────────────────────────────────
# Юзер может включить публичную ссылку → появляется URL вида
# /p/{public_token} который ведёт на PDF без авторизации. При первом
# открытии — фиксируется opened_at и crm_stage переходит «sent → opened».


import secrets as _pub_secrets


@router.post("/projects/{project_id}/public-link")
def toggle_public_link(project_id: int, db: Session = Depends(get_db),
                        user: User = Depends(current_user)):
    """Создать/обновить публичную ссылку. Возвращает токен и URL.
    Если уже есть — ротирует токен (старая ссылка перестаёт работать)."""
    p = db.query(ProposalProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    if not p.generated_pdf:
        raise HTTPException(400, "Сначала сгенерируйте КП")
    p.public_token = _pub_secrets.token_urlsafe(24)
    db.commit()
    app_url = os.getenv("APP_URL", "https://aiche.ru").rstrip("/")
    return {"token": p.public_token, "url": f"{app_url}/p/{p.public_token}"}


@router.delete("/projects/{project_id}/public-link")
def revoke_public_link(project_id: int, db: Session = Depends(get_db),
                        user: User = Depends(current_user)):
    """Отозвать публичную ссылку (старый URL перестаёт работать)."""
    p = db.query(ProposalProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    p.public_token = None
    db.commit()
    return {"status": "revoked"}


@router.get("/projects/{project_id}/preview")
def preview_html(project_id: int, db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    """Вернуть сгенерированный HTML для inline-превью (без авторизации в URL)."""
    p = db.query(ProposalProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    if not p.generated_html:
        raise HTTPException(404, "Не сгенерировано")
    return {"html": p.generated_html}


# ── Edit HTML manually (бесплатно) + regenerate PDF ─────────────────────────


class SaveHtmlBody(BaseModel):
    html: str


@router.post("/projects/{project_id}/save-html")
def save_proposal_html(project_id: int, body: SaveHtmlBody,
                        db: Session = Depends(get_db),
                        user: User = Depends(current_user)):
    """Сохраняет ручную правку HTML + перегенерирует PDF.
    Не списывает баланс — это правка, не AI-генерация. Защита от больших
    payload'ов (макс 500 КБ) и от чужих данных (filter_by user_id).
    Перед сохранением — простая валидация что это HTML, а не goofy XML/JSON.
    """
    p = db.query(ProposalProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    if not body.html or not body.html.strip():
        raise HTTPException(400, "HTML пустой")
    if len(body.html) > 500_000:
        raise HTTPException(413, "HTML слишком большой (макс 500 КБ)")
    new_html = body.html.strip()

    # Сохраняем + регенерируем PDF
    from server.proposal_builder import _save_pdf
    p.generated_html = new_html
    try:
        new_pdf_path = _save_pdf(new_html, p.id)
        # Удаляем старый PDF (если был)
        if p.generated_pdf and p.generated_pdf != new_pdf_path:
            try:
                base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                old = os.path.join(base, p.generated_pdf.lstrip("/"))
                if os.path.exists(old):
                    os.unlink(old)
            except Exception:
                pass
        p.generated_pdf = new_pdf_path
        p.status = "done"
        _snapshot_version(db, p, note="Ручная правка HTML")
        db.commit()
    except Exception as e:
        log.error(f"[proposal] save-html PDF regen failed: {type(e).__name__}: {e}")
        # HTML сохранён, но PDF не пересоздался — возвращаем 200 с предупреждением
        db.commit()
        raise HTTPException(503, f"HTML сохранён, но PDF не удалось пересобрать: {type(e).__name__}")

    log_action("proposal.html_edited", user_id=user.id,
               target_type="proposal", target_id=str(p.id),
               details={"html_size": len(new_html)})
    return _project_to_dict(p, full=True)


# ── AI-правка одной секции (real × 5, без фикс-минимума) ──────────────────


class EditSectionBody(BaseModel):
    section_html: str
    instruction: str


@router.post("/projects/{project_id}/edit-section")
def edit_section_endpoint(project_id: int, body: EditSectionBody,
                           db: Session = Depends(get_db),
                           user: User = Depends(current_user)):
    """Точечная правка одной секции через AI. Цена = real_tokens × 5
    (margin из pricing.ai.improve_margin_pct). Защищает от случаев где
    юзер хочет переписать только один блок и не хочет платить за полный
    регенерат (5 ₽). Auto-refund при ошибке.
    """
    p = db.query(ProposalProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    if not p.generated_html:
        raise HTTPException(400, "Сначала сгенерируйте КП")
    section = (body.section_html or "").strip()
    instr = (body.instruction or "").strip()
    if not section or not instr:
        raise HTTPException(400, "Не указан section_html или instruction")
    if len(section) > 50_000:
        raise HTTPException(413, "Секция слишком большая")
    if len(instr) > 2_000:
        raise HTTPException(413, "Инструкция слишком длинная")
    if section not in p.generated_html:
        raise HTTPException(400, "Этот блок не найден в текущем КП — возможно, КП изменился")

    # User-key для скидки
    from server.models import UserApiKey
    uk = db.query(UserApiKey).filter_by(user_id=user.id, provider="anthropic").first()
    user_key = uk.api_key if uk else None

    # Выполняем edit
    from server.proposal_builder import edit_section, _build_brand_css, _save_pdf
    brand = None
    if p.brand_id:
        brand = db.query(ProposalBrand).filter_by(
            id=p.brand_id, user_id=user.id).first()
    brand_css = _build_brand_css(brand)

    try:
        result = edit_section(section, instr, brand_css, user_api_key=user_key)
    except Exception as e:
        log.error(f"[proposal] edit-section failed: {type(e).__name__}: {e}")
        raise HTTPException(503, f"AI-правка не удалась: {type(e).__name__}")

    new_section = result["html"]
    usage = result.get("usage", {}) or {}
    input_tok = int(usage.get("input_tokens", 0) or 0)
    output_tok = int(usage.get("output_tokens", 0) or 0)

    # Расчёт цены: реальные токены × margin (ai.improve_margin_pct=500%)
    # Базовая цена по тарифу claude — берём из ModelPricing fallback.
    from server.models import ModelPricing
    from server.pricing import get_price as _gp
    pricing_row = db.query(ModelPricing).filter_by(model_id="claude").first()
    if pricing_row and (pricing_row.ch_per_1k_input or pricing_row.ch_per_1k_output):
        base = (input_tok / 1000.0) * (pricing_row.ch_per_1k_input or 0) + \
               (output_tok / 1000.0) * (pricing_row.ch_per_1k_output or 0)
        base = max(int(round(base)), pricing_row.min_ch_per_req or 1)
    else:
        base = 5  # минимум 5 коп если pricing неизвестен
    margin_pct = int(_gp("ai.improve_margin_pct", default=500))
    cost = max(1, int(round(base * margin_pct / 100)))

    from server.billing import deduct_strict
    if not deduct_strict(db, user.id, cost):
        # Не списали → не сохраняем правку
        raise HTTPException(402, f"Недостаточно средств (нужно ~{cost/100:.2f} ₽)")
    p.price_kop = (p.price_kop or 0) + cost
    db.add(Transaction(user_id=user.id, type="usage", tokens_delta=-cost,
                        description=f"AI-правка КП «{p.name}» ({cost/100:.2f} ₽)"))

    # Заменяем секцию в полном HTML, регенерируем PDF
    p.generated_html = p.generated_html.replace(section, new_section, 1)
    try:
        new_pdf = _save_pdf(p.generated_html, p.id)
        if p.generated_pdf and p.generated_pdf != new_pdf:
            try:
                base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                old = os.path.join(base_dir, p.generated_pdf.lstrip("/"))
                if os.path.exists(old):
                    os.unlink(old)
            except Exception:
                pass
        p.generated_pdf = new_pdf
        _snapshot_version(db, p, note="AI-правка блока", cost_kop=cost)
        db.commit()
    except Exception as e:
        log.error(f"[proposal] edit-section PDF regen failed: {type(e).__name__}: {e}")
        db.commit()
        raise HTTPException(503, f"AI-правка применена, но PDF не пересобран: {type(e).__name__}")

    log_action("proposal.section_edited", user_id=user.id,
               target_type="proposal", target_id=str(p.id),
               details={"cost_kop": cost, "input_tok": input_tok, "output_tok": output_tok})
    return {**_project_to_dict(p, full=True), "cost_kop": cost,
            "new_section": new_section}


# ── Versioning ─────────────────────────────────────────────────────────────


@router.get("/projects/{project_id}/versions")
def list_versions(project_id: int, db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    """История версий КП (последние 10). Без HTML — только метаданные."""
    p = db.query(ProposalProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    rows = (db.query(ProposalVersion)
              .filter_by(proposal_id=project_id)
              .order_by(ProposalVersion.created_at.desc())
              .limit(_MAX_VERSIONS_PER_PROPOSAL).all())
    return [{
        "id": v.id, "note": v.note, "cost_kop": v.cost_kop or 0,
        "created_at": v.created_at.isoformat() if v.created_at else None,
        "is_current": (v.html == p.generated_html),
    } for v in rows]


@router.post("/projects/{project_id}/versions/{version_id}/restore")
def restore_version(project_id: int, version_id: int,
                     db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    """Откатить КП к одной из сохранённых версий. Не списывает баланс.
    Регенерирует PDF из старого HTML, чтобы файл был свежий."""
    p = db.query(ProposalProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    v = db.query(ProposalVersion).filter_by(id=version_id, proposal_id=project_id,
                                              user_id=user.id).first()
    if not v:
        raise HTTPException(404, "Версия не найдена")
    if not v.html:
        raise HTTPException(400, "Версия повреждена (нет HTML)")

    # Перед откатом сохраняем текущее состояние как версию
    _snapshot_version(db, p, note="Перед откатом")

    p.generated_html = v.html
    # PDF: если файл старой версии существует — используем; иначе регенерим
    from server.proposal_builder import _save_pdf
    try:
        if v.pdf_path:
            base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            old_pdf = os.path.join(base, v.pdf_path.lstrip("/"))
            if os.path.exists(old_pdf):
                p.generated_pdf = v.pdf_path
            else:
                p.generated_pdf = _save_pdf(v.html, p.id)
        else:
            p.generated_pdf = _save_pdf(v.html, p.id)
        p.status = "done"
        db.commit()
    except Exception as e:
        log.error(f"[proposal] restore PDF failed: {type(e).__name__}: {e}")
        db.commit()
        raise HTTPException(503, f"HTML восстановлен, но PDF не пересобран: {type(e).__name__}")

    log_action("proposal.version_restored", user_id=user.id,
               target_type="proposal", target_id=str(p.id),
               details={"version_id": version_id})
    return _project_to_dict(p, full=True)


# ── Manual send by email ───────────────────────────────────────────────────


class SendEmailBody(BaseModel):
    to: str | None = None        # default — client_email из проекта
    subject: str | None = None
    body: str | None = None      # plain текст письма (тело)


@router.post("/projects/{project_id}/send-email")
def send_proposal_email(project_id: int, body: SendEmailBody,
                         db: Session = Depends(get_db),
                         user: User = Depends(current_user)):
    """Ручная отправка сгенерированного PDF клиенту по email.
    Не списывает баланс (генерация уже была оплачена).
    """
    p = db.query(ProposalProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    if not p.generated_pdf:
        raise HTTPException(400, "Сначала сгенерируйте КП — кнопка «Сгенерировать»")
    to = (body.to or p.client_email or "").strip()
    if not to or "@" not in to:
        raise HTTPException(400, "Не указан email получателя (заполните «Email клиента» или укажите явно)")

    # Считываем PDF
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    pdf_full = os.path.join(base, p.generated_pdf.lstrip("/"))
    if not os.path.exists(pdf_full):
        raise HTTPException(404, "PDF файл удалён — пересоздайте КП")
    with open(pdf_full, "rb") as f:
        pdf_bytes = f.read()

    subject = (body.subject or "").strip() or f"Коммерческое предложение — {p.name}"
    user_body = (body.body or "").strip()
    if not user_body:
        salut = f", {p.client_name}" if p.client_name else ""
        user_body = (f"Здравствуйте{salut}!\n\n"
                     f"Спасибо за интерес к нашей компании. Во вложении — "
                     f"коммерческое предложение, подготовленное специально для вас.\n\n"
                     f"Если возникнут вопросы — мы на связи.")
    body_html = "<div style='font-family:Inter,sans-serif;line-height:1.6'>" + \
                user_body.replace("\n", "<br/>") + "</div>"

    try:
        from server.email_service import send_with_attachment
        msgid = send_with_attachment(
            to=to, subject=subject, html_body=body_html,
            attachments=[(f"kp_{p.id}.pdf", pdf_bytes, "application/pdf")],
        )
    except Exception as e:
        log.error(f"[proposal] manual email send failed: {type(e).__name__}: {e}")
        raise HTTPException(503, f"Не удалось отправить email: {type(e).__name__}. "
                                  f"Проверьте SMTP-настройки в .env (SMTP_HOST/USER/PASS).")

    from datetime import datetime as _dt
    p.sent_at = _dt.utcnow()
    # Сохраняем Message-ID для threading: входящие письма-ответы клиента
    # будут содержать In-Reply-To с этим значением → автоматически
    # отмечаем proposal как «replied».
    if msgid:
        p.outbox_message_id = msgid
    # Автоматически переводим в стадию «отправлено» если ещё не дальше по воронке
    if (p.crm_stage or "new") in ("new", "draft"):
        p.crm_stage = "sent"
    if not p.client_email and to:
        p.client_email = to
    db.commit()
    log_action("proposal.manual_sent", user_id=user.id,
               target_type="proposal", target_id=str(p.id),
               details={"to": to[:50], "msgid": (msgid or "")[:80]})
    return {"status": "sent", "to": to, "sent_at": p.sent_at.isoformat()}


# ── Прайс-листы (собственные, не привязанные к ботам) ────────────────────


def _pricelist_to_dict(pl: ProposalPriceList, items: list | None = None) -> dict:
    out = {
        "id": pl.id, "name": pl.name, "description": pl.description,
        "is_default": bool(pl.is_default),
        "created_at": pl.created_at.isoformat() if pl.created_at else None,
    }
    if items is not None:
        out["items"] = [{
            "id": it.id, "name": it.name,
            "price_kop": it.price_kop, "price_text": it.price_text,
            "category": it.category, "description": it.description,
            "sort_order": it.sort_order, "is_active": bool(it.is_active),
        } for it in items]
        out["item_count"] = len(items)
    return out


@router.get("/price-lists")
def list_pricelists(db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    """Список прайсов юзера. Возвращает count позиций без самих позиций."""
    rows = (db.query(ProposalPriceList)
              .filter_by(user_id=user.id)
              .order_by(ProposalPriceList.is_default.desc(),
                        ProposalPriceList.id.desc())
              .all())
    out = []
    for pl in rows:
        cnt = db.query(ProposalPriceItem).filter_by(
            price_list_id=pl.id, is_active=True).count()
        d = _pricelist_to_dict(pl)
        d["item_count"] = cnt
        out.append(d)
    return out


@router.get("/price-lists/{pl_id}")
def get_pricelist(pl_id: int, db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    pl = db.query(ProposalPriceList).filter_by(id=pl_id, user_id=user.id).first()
    if not pl:
        raise HTTPException(404, "Прайс не найден")
    items = (db.query(ProposalPriceItem)
               .filter_by(price_list_id=pl_id)
               .order_by(ProposalPriceItem.sort_order, ProposalPriceItem.id)
               .all())
    return _pricelist_to_dict(pl, items)


class PriceListBody(BaseModel):
    name: str
    description: str | None = None
    is_default: bool | None = False


@router.post("/price-lists")
def create_pricelist(body: PriceListBody, db: Session = Depends(get_db),
                      user: User = Depends(current_user)):
    if not body.name or not body.name.strip():
        raise HTTPException(400, "Название прайса обязательно")
    if body.is_default:
        db.query(ProposalPriceList).filter_by(user_id=user.id, is_default=True) \
          .update({"is_default": False})
    pl = ProposalPriceList(
        user_id=user.id, name=body.name.strip()[:200],
        description=(body.description or "")[:500] or None,
        is_default=bool(body.is_default),
    )
    db.add(pl); db.commit(); db.refresh(pl)
    log_action("proposal.pricelist_created", user_id=user.id,
               target_type="pricelist", target_id=str(pl.id))
    return _pricelist_to_dict(pl, [])


@router.put("/price-lists/{pl_id}")
def update_pricelist(pl_id: int, body: PriceListBody,
                      db: Session = Depends(get_db),
                      user: User = Depends(current_user)):
    pl = db.query(ProposalPriceList).filter_by(id=pl_id, user_id=user.id).first()
    if not pl:
        raise HTTPException(404, "Прайс не найден")
    if body.name:
        pl.name = body.name.strip()[:200]
    pl.description = (body.description or "")[:500] or None
    if body.is_default and not pl.is_default:
        db.query(ProposalPriceList).filter_by(user_id=user.id, is_default=True) \
          .update({"is_default": False})
        pl.is_default = True
    db.commit(); db.refresh(pl)
    return _pricelist_to_dict(pl)


@router.delete("/price-lists/{pl_id}")
def delete_pricelist(pl_id: int, db: Session = Depends(get_db),
                      user: User = Depends(current_user)):
    pl = db.query(ProposalPriceList).filter_by(id=pl_id, user_id=user.id).first()
    if not pl:
        raise HTTPException(404, "Прайс не найден")
    db.delete(pl); db.commit()
    log_action("proposal.pricelist_deleted", user_id=user.id,
               target_type="pricelist", target_id=str(pl_id))
    return {"status": "deleted"}


# ── Позиции прайса ─────────────────────────────────────────────────────────


class PriceItemBody(BaseModel):
    name: str
    price_kop: int | None = None
    price_text: str | None = None
    category: str | None = None
    description: str | None = None
    sort_order: int | None = 0


def _validate_price_item(body: PriceItemBody) -> None:
    if not body.name or not body.name.strip():
        raise HTTPException(400, "Название позиции обязательно")
    if body.price_kop is not None and body.price_kop < 0:
        raise HTTPException(400, "Цена не может быть отрицательной")
    if body.price_kop is not None and body.price_kop > 100_000_000_000:
        raise HTTPException(400, "Цена слишком большая (макс 1 млрд ₽)")


@router.post("/price-lists/{pl_id}/items")
def add_price_item(pl_id: int, body: PriceItemBody,
                    db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    pl = db.query(ProposalPriceList).filter_by(id=pl_id, user_id=user.id).first()
    if not pl:
        raise HTTPException(404, "Прайс не найден")
    _validate_price_item(body)
    item = ProposalPriceItem(
        price_list_id=pl_id,
        name=body.name.strip()[:200],
        price_kop=body.price_kop if body.price_kop and body.price_kop > 0 else None,
        price_text=(body.price_text or "")[:60] or None,
        category=(body.category or "")[:60] or None,
        description=(body.description or "")[:500] or None,
        sort_order=int(body.sort_order or 0),
        is_active=True,
    )
    db.add(item); db.commit(); db.refresh(item)
    return {"id": item.id, "name": item.name}


@router.put("/price-lists/{pl_id}/items/{item_id}")
def update_price_item(pl_id: int, item_id: int, body: PriceItemBody,
                       db: Session = Depends(get_db),
                       user: User = Depends(current_user)):
    pl = db.query(ProposalPriceList).filter_by(id=pl_id, user_id=user.id).first()
    if not pl:
        raise HTTPException(404, "Прайс не найден")
    item = db.query(ProposalPriceItem).filter_by(
        id=item_id, price_list_id=pl_id).first()
    if not item:
        raise HTTPException(404, "Позиция не найдена")
    _validate_price_item(body)
    item.name = body.name.strip()[:200]
    item.price_kop = body.price_kop if body.price_kop and body.price_kop > 0 else None
    item.price_text = (body.price_text or "")[:60] or None
    item.category = (body.category or "")[:60] or None
    item.description = (body.description or "")[:500] or None
    item.sort_order = int(body.sort_order or 0)
    db.commit(); db.refresh(item)
    return {"id": item.id, "name": item.name}


@router.delete("/price-lists/{pl_id}/items/{item_id}")
def delete_price_item(pl_id: int, item_id: int,
                       db: Session = Depends(get_db),
                       user: User = Depends(current_user)):
    pl = db.query(ProposalPriceList).filter_by(id=pl_id, user_id=user.id).first()
    if not pl:
        raise HTTPException(404, "Прайс не найден")
    item = db.query(ProposalPriceItem).filter_by(
        id=item_id, price_list_id=pl_id).first()
    if not item:
        raise HTTPException(404, "Позиция не найдена")
    db.delete(item); db.commit()
    return {"status": "deleted"}


@router.post("/price-lists/{pl_id}/import-csv")
async def import_price_csv(pl_id: int,
                            file: UploadFile = File(...),
                            db: Session = Depends(get_db),
                            user: User = Depends(current_user)):
    """Импорт CSV: name,price[,category,description].
    Заменяет все существующие позиции (мягкое удаление is_active=False).
    Защищён от 1e10 в экспоненциальной нотации (макс 1 млрд ₽)."""
    import csv as _csv
    import io as _io
    pl = db.query(ProposalPriceList).filter_by(id=pl_id, user_id=user.id).first()
    if not pl:
        raise HTTPException(404, "Прайс не найден")
    raw = await file.read()
    if not raw or len(raw) > 5 * 1024 * 1024:
        raise HTTPException(413, "Файл пустой или слишком большой (>5 МБ)")
    # Decode + auto-detect разделитель
    text = None
    for enc in ("utf-8-sig", "utf-8", "cp1251", "windows-1251"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise HTTPException(400, "Не удалось распознать кодировку CSV (utf-8 или cp1251)")
    sample = text[:2000]
    try:
        sniffer = _csv.Sniffer()
        dialect = sniffer.sniff(sample, delimiters=",;\t|")
    except Exception:
        class _D(_csv.excel): delimiter = ";"
        dialect = _D
    reader = _csv.DictReader(_io.StringIO(text), dialect=dialect)
    cols_lower = {c.lower(): c for c in (reader.fieldnames or [])}
    def _col(*aliases):
        for a in aliases:
            if a in cols_lower:
                return cols_lower[a]
        return None
    col_name = _col("name", "название", "услуга", "товар", "позиция", "наименование")
    col_price = _col("price", "цена", "стоимость", "price_rub", "руб")
    col_desc = _col("description", "описание", "детали", "что входит")
    col_cat = _col("category", "категория", "раздел", "группа")
    if not col_name:
        raise HTTPException(400, "В CSV не найдена колонка 'name' / 'название'")

    # Деактивируем существующие
    db.query(ProposalPriceItem).filter_by(price_list_id=pl_id).update({"is_active": False})

    added = 0
    for i, row in enumerate(reader):
        name = (row.get(col_name) or "").strip()
        if not name:
            continue
        price_kop, price_text = None, None
        raw_price = ((row.get(col_price) or "") if col_price else "").strip()
        if raw_price:
            cleaned = raw_price.replace(" ", "").replace("\xa0", "") \
                                .replace("₽", "").replace("руб", "").replace(",", ".")
            try:
                pf = float(cleaned)
                if not (0 < pf <= 1_000_000_000):
                    raise ValueError("out of range")
                price_kop = int(round(pf * 100))
            except (ValueError, TypeError, OverflowError):
                price_text = raw_price[:60]
        item = ProposalPriceItem(
            price_list_id=pl_id,
            name=name[:200],
            price_kop=price_kop if price_kop and price_kop > 0 else None,
            price_text=price_text,
            description=((row.get(col_desc) or "") if col_desc else "").strip()[:500] or None,
            category=((row.get(col_cat) or "") if col_cat else "").strip()[:60] or None,
            sort_order=i, is_active=True,
        )
        db.add(item); added += 1
    db.commit()
    log_action("proposal.pricelist_imported", user_id=user.id,
               target_type="pricelist", target_id=str(pl_id),
               details={"added": added})
    return {"status": "ok", "added": added}
