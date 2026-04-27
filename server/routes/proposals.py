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
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from server.routes.deps import get_db, current_user, optional_user
from server.models import (
    ProposalProject, ProposalBrand, ChatBot, BotPriceItem,
    User, Transaction,
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


def _brand_to_dict(b: ProposalBrand) -> dict:
    return {
        "id": b.id, "name": b.name, "company_name": b.company_name,
        "logo_url": b.logo_url,
        "primary_color": b.primary_color, "secondary_color": b.secondary_color,
        "accent_color": b.accent_color, "font_family": b.font_family,
        "style_preset": b.style_preset,
        "contacts": b.contacts, "inn": b.inn, "address": b.address,
        "signature_url": b.signature_url,
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
    client_name: str | None = None
    client_email: str | None = None
    client_request: str | None = None
    client_site_url: str | None = None
    extra_notes: str | None = None


def _project_to_dict(p: ProposalProject, full: bool = False) -> dict:
    base = {
        "id": p.id, "name": p.name, "status": p.status,
        "brand_id": p.brand_id, "bot_id": p.bot_id,
        "client_name": p.client_name, "client_email": p.client_email,
        "price_kop": p.price_kop or 0,
        "auto_generated": bool(p.auto_generated),
        "sent_at": p.sent_at.isoformat() if p.sent_at else None,
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
    p = ProposalProject(
        user_id=user.id,
        name=body.name.strip()[:200],
        brand_id=body.brand_id, bot_id=body.bot_id,
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


@router.post("/projects/{project_id}/generate")
def generate_proposal_endpoint(project_id: int, db: Session = Depends(get_db),
                                 user: User = Depends(current_user)):
    """Сгенерировать КП: парсит сайт клиента → промпт Claude → HTML+PDF.
    Списывает фикс. цену (5 ₽ = 500 коп при перегенерации, 50 ₽ за первый раз)."""
    p = db.query(ProposalProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    if not p.client_request and not p.extra_notes:
        raise HTTPException(400, "Заполните «Запрос клиента» или «Доп. инструкции» — без контекста КП не получится")

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
