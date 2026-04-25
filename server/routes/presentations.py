from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
import json, logging

from server.routes.deps import get_db, current_user, optional_user
from server.models import PresentationProject, PresentationTemplate, CompanyProfile, User, Transaction, ChatBot
from server.ai import generate_response

log = logging.getLogger(__name__)

router = APIRouter(tags=["presentations"])

# Фикс-цены в копейках. Пользователю — простая сетка:
#   КП = 5000 коп = 50 ₽
#   Презентация = 10000 коп = 100 ₽
#   Доработка/правка = 500 коп = 5 ₽
PRES_KP_COST    = 5000
PRES_DECK_COST  = 10000
PRES_EDIT_COST  = 500
# Backwards-compat (используется в логах/старом коде):
PRES_CH_COST = PRES_KP_COST


class CreatePresentationRequest(BaseModel):
    name: str
    doc_type: str = "kp"          # "kp" | "presentation"
    template_id: int | None = None
    input_data: str | None = None
    description: str | None = None


class CompanyProfileRequest(BaseModel):
    company_name: str | None = None
    description: str | None = None
    services: str | None = None
    prices: str | None = None
    style_notes: str | None = None
    contacts: str | None = None
    extra: str | None = None


# ── Company Profile ────────────────────────────────────────────────────────────

@router.get("/presentations/company-profile")
def get_company_profile(db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = db.query(CompanyProfile).filter_by(user_id=user.id).first()
    if not p:
        return {"company_name": "", "description": "", "services": "",
                "prices": "", "style_notes": "", "contacts": "", "extra": ""}
    return {"company_name": p.company_name or "", "description": p.description or "",
            "services": p.services or "", "prices": p.prices or "",
            "style_notes": p.style_notes or "", "contacts": p.contacts or "",
            "extra": p.extra or ""}


@router.put("/presentations/company-profile")
def save_company_profile(req: CompanyProfileRequest, db: Session = Depends(get_db),
                         user: User = Depends(current_user)):
    p = db.query(CompanyProfile).filter_by(user_id=user.id).first()
    if not p:
        p = CompanyProfile(user_id=user.id)
        db.add(p)
    p.company_name = req.company_name
    p.description  = req.description
    p.services     = req.services
    p.prices       = req.prices
    p.style_notes  = req.style_notes
    p.contacts     = req.contacts
    p.extra        = req.extra
    db.commit()
    return {"status": "ok"}


# ── Templates ─────────────────────────────────────────────────────────────────

@router.get("/presentations/templates")
def list_pres_templates(db: Session = Depends(get_db)):
    items = db.query(PresentationTemplate).filter_by(is_active=True).order_by(PresentationTemplate.sort_order).all()
    return [{"id": t.id, "title": t.title, "description": t.description,
             "input_fields": json.loads(t.input_fields) if t.input_fields else [],
             "header_html": t.header_html} for t in items]


# ── Projects ──────────────────────────────────────────────────────────────────

@router.get("/presentations/projects")
def list_presentations(db: Session = Depends(get_db), user=Depends(optional_user)):
    if not user: return []
    projects = db.query(PresentationProject).filter_by(user_id=user.id).order_by(PresentationProject.created_at.desc()).all()
    result = []
    for p in projects:
        inp = {}
        try: inp = json.loads(p.input_data) if p.input_data else {}
        except (json.JSONDecodeError, TypeError): pass
        result.append({
            "id": p.id, "name": p.name, "status": p.status,
            "doc_type": inp.get("doc_type", "kp"),
            "price_tokens": p.price_tokens, "template_id": p.template_id,
            "created_at": p.created_at.isoformat() if p.created_at else None
        })
    return result


@router.post("/presentations/projects")
def create_presentation_project(req: CreatePresentationRequest,
                                db: Session = Depends(get_db),
                                user: User = Depends(current_user)):
    input_data = json.dumps({"doc_type": req.doc_type,
                              "description": req.description or "",
                              **(json.loads(req.input_data) if req.input_data else {})})
    p = PresentationProject(user_id=user.id, name=req.name, template_id=req.template_id,
                            input_data=input_data, status="draft")
    db.add(p); db.commit(); db.refresh(p)
    return {"id": p.id, "status": "created"}


@router.get("/presentations/projects/{project_id}")
def get_presentation_project(project_id: int, db: Session = Depends(get_db),
                              user: User = Depends(current_user)):
    p = db.query(PresentationProject).filter_by(id=project_id, user_id=user.id).first()
    if not p: raise HTTPException(404, "Проект не найден")
    inp = {}
    try: inp = json.loads(p.input_data) if p.input_data else {}
    except (json.JSONDecodeError, TypeError): pass
    return {"id": p.id, "name": p.name, "status": p.status,
            "doc_type": inp.get("doc_type", "kp"),
            "input_data": p.input_data, "generated_content": p.generated_content,
            "price_tokens": p.price_tokens, "template_id": p.template_id,
            "image_paths": p.image_paths,
            "attached_bot_id": p.attached_bot_id,
            "created_at": p.created_at.isoformat() if p.created_at else None}


class AttachBotBody(BaseModel):
    bot_id: int | None = None  # None — отвязать


@router.post("/presentations/projects/{project_id}/attach-image")
def pres_project_attach_image(project_id: int, body: dict, db: Session = Depends(get_db),
                              user: User = Depends(current_user)):
    """Прикрепить уже загруженный (через /upload) файл к презентации."""
    p = db.query(PresentationProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    try:
        imgs = json.loads(p.image_paths) if p.image_paths else []
    except Exception:
        imgs = []
    file_url = body.get("file_url", "")
    if not file_url:
        raise HTTPException(400, "Нет file_url")
    imgs.append(file_url)
    p.image_paths = json.dumps(imgs)
    db.commit()
    return {"status": "ok", "image_paths": imgs}


@router.post("/presentations/projects/{project_id}/attach-bot")
def pres_project_attach_bot(project_id: int, body: AttachBotBody,
                            db: Session = Depends(get_db),
                            user: User = Depends(current_user)):
    """Привязать чат-бота к проекту КП/презентации (для виджета на публичной странице)."""
    p = db.query(PresentationProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    if body.bot_id is None:
        p.attached_bot_id = None
        db.commit()
        return {"status": "detached"}
    bot = db.query(ChatBot).filter_by(id=body.bot_id, user_id=user.id).first()
    if not bot:
        raise HTTPException(404, "Бот не найден или не ваш")
    if not bot.widget_enabled:
        raise HTTPException(400, "У бота не включён виджет — включите в /chatbots.html")
    p.attached_bot_id = bot.id
    db.commit()
    return {"status": "attached", "bot_id": bot.id, "bot_name": bot.name}


@router.put("/presentations/projects/{project_id}")
def update_presentation_project(project_id: int, body: dict,
                                 db: Session = Depends(get_db),
                                 user: User = Depends(current_user)):
    p = db.query(PresentationProject).filter_by(id=project_id, user_id=user.id).first()
    if not p: raise HTTPException(404, "Проект не найден")
    if "input_data" in body: p.input_data = body["input_data"]
    if "name" in body: p.name = body["name"]
    db.commit()
    return {"status": "ok"}


@router.delete("/presentations/projects/{project_id}")
def delete_presentation_project(project_id: int, db: Session = Depends(get_db),
                                 user: User = Depends(current_user)):
    p = db.query(PresentationProject).filter_by(id=project_id, user_id=user.id).first()
    if not p: raise HTTPException(404, "Проект не найден")
    db.delete(p); db.commit()
    return {"status": "deleted"}


@router.post("/presentations/projects/{project_id}/generate")
def generate_presentation(project_id: int, body: dict = None,
                          db: Session = Depends(get_db), user=Depends(optional_user)):
    if not user: raise HTTPException(401, "Нужна авторизация")
    p = db.query(PresentationProject).filter_by(id=project_id, user_id=user.id).first()
    if not p: raise HTTPException(404, "Проект не найден")
    inp = {}
    try: inp = json.loads(p.input_data) if p.input_data else {}
    except (json.JSONDecodeError, TypeError): pass
    doc_type = inp.get("doc_type", "kp")
    # Фикс-цена зависит от типа документа: 50 ₽ за КП, 100 ₽ за презентацию.
    # При перегенерации (status=done) — берём цену редактирования.
    if p.status == "done":
        cost = PRES_EDIT_COST
        cost_desc = "Правка/перегенерация"
    elif doc_type == "presentation":
        cost = PRES_DECK_COST
        cost_desc = "Презентация"
    else:
        cost = PRES_KP_COST
        cost_desc = "КП"
    from server.billing import deduct_strict
    if not deduct_strict(db, user.id, cost):
        raise HTTPException(402, f"Недостаточно средств (нужно {cost/100:.0f} ₽)")
    p.price_tokens += cost
    db.add(Transaction(user_id=user.id, type="usage", tokens_delta=-cost,
                       description=f"{cost_desc} ({cost/100:.0f} ₽)"))

    description = inp.get("description", "")

    # Подгружаем профиль компании
    profile = db.query(CompanyProfile).filter_by(user_id=user.id).first()
    company_ctx = ""
    if profile:
        parts = []
        if profile.company_name: parts.append(f"Компания: {profile.company_name}")
        if profile.description:  parts.append(f"Описание: {profile.description}")
        if profile.services:     parts.append(f"Услуги/товары:\n{profile.services}")
        if profile.prices:       parts.append(f"Прайс:\n{profile.prices}")
        if profile.style_notes:  parts.append(f"Стиль: {profile.style_notes}")
        if profile.contacts:     parts.append(f"Контакты: {profile.contacts}")
        if parts:
            company_ctx = "\n\n=== ИНФОРМАЦИЯ О КОМПАНИИ ===\n" + "\n".join(parts)

    if doc_type == "kp":
        prompt = (
            "Ты опытный специалист по продажам. Создай профессиональное коммерческое предложение (КП) "
            "в структурированном формате. Структура: заголовок, краткое описание компании, суть предложения, "
            "что получает клиент, прайс/условия, призыв к действию и контакты. "
            "Оформи красиво с разделами и отступами. Пиши убедительно и по делу."
        )
    else:
        prompt = (
            "Ты опытный бизнес-аналитик. Создай структуру профессиональной презентации "
            "с содержанием каждого слайда. Структура: титульный слайд, о компании, проблема/решение, "
            "продукт/услуга, преимущества, кейсы/результаты, цены, следующий шаг. "
            "Для каждого слайда укажи заголовок и ключевые тезисы. Оформи чётко и лаконично."
        )

    if description:
        prompt += f"\n\nЗапрос клиента: {description}"
    prompt += company_ctx
    prompt += "\n\nВерни готовый документ на русском языке."

    answer = generate_response("claude", [{"role": "user", "content": prompt}], None)
    content = answer.get("content", "") if isinstance(answer, dict) else ""
    p.generated_content = content
    p.status = "done"
    db.commit()
    return {"generated_content": content, "status": p.status}
