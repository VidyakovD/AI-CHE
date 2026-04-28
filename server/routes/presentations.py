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
    doc_type: str = "presentation"   # "kp" (legacy) | "presentation"
    template_id: int | None = None
    input_data: str | None = None
    description: str | None = None
    # Новые поля для модуля презентаций
    topic: str | None = None
    audience: str | None = None
    slide_count: int | None = 10
    extra_info: str | None = None
    color_scheme: str | None = "dark"


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
        # Новые презентации — doc_type='presentation'. Старые КП в этой
        # таблице остались для обратной совместимости (отображаются если есть).
        result.append({
            "id": p.id, "name": p.name, "status": p.status,
            "doc_type": inp.get("doc_type", "presentation"),
            "topic": p.topic, "audience": p.audience,
            "slide_count": p.slide_count or 10,
            "color_scheme": p.color_scheme or "dark",
            "has_pptx": bool(p.pptx_path),
            "has_html": bool(p.html_preview),
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
    sc = max(3, min(40, int(req.slide_count or 10)))
    p = PresentationProject(
        user_id=user.id, name=(req.name or "")[:200], template_id=req.template_id,
        input_data=input_data, status="draft",
        topic=(req.topic or "")[:500] or None,
        audience=(req.audience or "")[:200] or None,
        slide_count=sc,
        extra_info=(req.extra_info or "")[:15000] or None,
        color_scheme=req.color_scheme if req.color_scheme in ("dark","light","corp","brand") else "dark",
    )
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
    img_list = []
    try:
        img_list = json.loads(p.image_paths) if p.image_paths else []
    except Exception: img_list = []
    return {"id": p.id, "name": p.name, "status": p.status,
            "doc_type": inp.get("doc_type", "presentation"),
            "input_data": p.input_data, "generated_content": p.generated_content,
            "price_tokens": p.price_tokens, "template_id": p.template_id,
            "image_paths": img_list,
            "attached_bot_id": p.attached_bot_id,
            # Новые поля переработанного модуля презентаций
            "topic": p.topic, "audience": p.audience,
            "slide_count": p.slide_count or 10,
            "extra_info": p.extra_info,
            "color_scheme": p.color_scheme or "dark",
            "style_preset": p.style_preset or "business",
            "html_preview": p.html_preview, "pptx_path": p.pptx_path,
            "has_slides": bool(p.slides_json),
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
    if "name" in body: p.name = (body["name"] or "")[:200]
    # Новые поля переработанного модуля презентаций
    if "topic" in body:
        p.topic = (body["topic"] or "")[:500] or None
    if "audience" in body:
        p.audience = (body["audience"] or "")[:200] or None
    if "slide_count" in body:
        try:
            sc = int(body["slide_count"])
            p.slide_count = max(3, min(40, sc))
        except Exception:
            pass
    if "extra_info" in body:
        p.extra_info = (body["extra_info"] or "")[:15000] or None
    if "color_scheme" in body:
        if body["color_scheme"] in ("dark", "light", "corp", "brand"):
            p.color_scheme = body["color_scheme"]
    if "style_preset" in body:
        if body["style_preset"] in ("business", "minimal", "bold"):
            p.style_preset = body["style_preset"]
    if "image_paths" in body:
        # body["image_paths"] — список URL'ов картинок
        if isinstance(body["image_paths"], list):
            p.image_paths = json.dumps(body["image_paths"][:20], ensure_ascii=False)
        elif body["image_paths"] is None:
            p.image_paths = None
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
    """Сгенерировать презентацию через Claude. Цена = real_tokens × 7 с
    минимумом из presentation.min_cost (50 ₽ по умолчанию). Auto-refund
    при ошибке.

    LEGACY-ветка: если у проекта есть doc_type='kp' в input_data — работает
    старый код (для обратной совместимости со старыми КП в БД). Новые
    презентации идут через presentation_builder."""
    if not user: raise HTTPException(401, "Нужна авторизация")
    p = db.query(PresentationProject).filter_by(id=project_id, user_id=user.id).first()
    if not p: raise HTTPException(404, "Проект не найден")
    inp = {}
    try: inp = json.loads(p.input_data) if p.input_data else {}
    except (json.JSONDecodeError, TypeError): pass
    doc_type = inp.get("doc_type", "presentation")

    # ── Legacy (КП через старый интерфейс — оставлено только для совместимости)
    if doc_type == "kp":
        if p.status == "done":
            cost = PRES_EDIT_COST; cost_desc = "Правка/перегенерация"
        else:
            cost = PRES_KP_COST; cost_desc = "КП"
        from server.billing import deduct_strict
        if not deduct_strict(db, user.id, cost):
            raise HTTPException(402, f"Недостаточно средств (нужно {cost/100:.0f} ₽)")
        p.price_tokens += cost
        db.add(Transaction(user_id=user.id, type="usage", tokens_delta=-cost,
                           description=f"{cost_desc} ({cost/100:.0f} ₽)"))
        description = inp.get("description", "")
        profile = db.query(CompanyProfile).filter_by(user_id=user.id).first()
        company_ctx = ""
        if profile:
            parts = []
            if profile.company_name: parts.append(f"Компания: {profile.company_name}")
            if profile.description:  parts.append(f"Описание: {profile.description}")
            if profile.services:     parts.append(f"Услуги/товары:\n{profile.services}")
            if profile.prices:       parts.append(f"Прайс:\n{profile.prices}")
            if profile.contacts:     parts.append(f"Контакты: {profile.contacts}")
            if parts:
                company_ctx = "\n\n=== ИНФОРМАЦИЯ О КОМПАНИИ ===\n" + "\n".join(parts)
        prompt = (
            "Ты опытный специалист по продажам. Создай профессиональное КП. "
            "Структура: заголовок, описание компании, предложение, прайс, призыв.\n\n"
            f"Запрос: {description}{company_ctx}\nРусский язык."
        )
        answer = generate_response("claude", [{"role": "user", "content": prompt}], None)
        content = answer.get("content", "") if isinstance(answer, dict) else ""
        p.generated_content = content
        p.status = "done"; db.commit()
        return {"generated_content": content, "status": p.status}

    # ── Новая ветка: презентация через presentation_builder (JSON → HTML+PPTX)
    from server.presentation_builder import (
        validate_presentation, generate_presentation as _gen, calc_actual_cost_kop,
    )
    from server.billing import deduct_strict, credit_atomic
    from server.models import UserApiKey

    # Pre-validation ДО списания
    try:
        validate_presentation(p)
    except ValueError as e:
        raise HTTPException(400, str(e))

    # Картинки из image_paths
    image_urls = []
    if p.image_paths:
        try:
            image_urls = json.loads(p.image_paths) if isinstance(p.image_paths, str) else []
        except Exception:
            image_urls = []

    # User-key — если есть Anthropic ключ юзера (скидка)
    uk = db.query(UserApiKey).filter_by(user_id=user.id, provider="anthropic").first()
    user_key = uk.api_key if uk else None

    # Запуск генерации (без списания — спишем по факту токенов)
    try:
        result = _gen(db, p, image_urls=image_urls, user_api_key=user_key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        log.error(f"[presentation] gen failed: {type(e).__name__}: {e}")
        p.status = "error"
        db.commit()
        raise HTTPException(503, f"Не удалось сгенерировать: {type(e).__name__}")

    usage = result.get("usage", {}) or {}
    cost = calc_actual_cost_kop(usage, db)

    # Списание (после успеха — чтобы не возвращать)
    if not deduct_strict(db, user.id, cost):
        raise HTTPException(402,
            f"Недостаточно средств. Стоимость по факту: {cost/100:.0f} ₽. Пополните баланс.")
    p.price_tokens = (p.price_tokens or 0) + cost
    db.add(Transaction(user_id=user.id, type="usage", tokens_delta=-cost,
                       description=f"Презентация «{p.name}» ({cost/100:.0f} ₽, "
                                    f"{usage.get('input_tokens',0)}+{usage.get('output_tokens',0)} ток.)"))

    p.slides_json = json.dumps(result["data"], ensure_ascii=False)
    p.html_preview = result.get("html_path")
    p.pptx_path = result.get("pptx_path")
    p.status = "done"
    db.commit()
    return {
        "status": "done", "cost_kop": cost,
        "slide_count": len((result["data"].get("slides") or [])),
        "html_preview": p.html_preview,
        "pptx_path": p.pptx_path,
        "title": (result["data"].get("title") or p.name),
    }


@router.post("/presentations/estimate-cost")
def estimate_cost_endpoint(body: dict, db: Session = Depends(get_db),
                            user=Depends(current_user)):
    """Прикидка стоимости ДО генерации (для UI «примерно X ₽»).
    body: {slide_count: int, extra_info_len: int}"""
    from server.presentation_builder import estimate_cost_kop
    sc = int((body or {}).get("slide_count") or 10)
    extra_len = int((body or {}).get("extra_info_len") or 0)
    sc = max(3, min(40, sc))
    low, high = estimate_cost_kop(sc, extra_len)
    return {
        "low_kop": low, "high_kop": high,
        "low_rub": round(low / 100, 0), "high_rub": round(high / 100, 0),
    }


@router.get("/presentations/projects/{project_id}/pptx")
def download_pptx(project_id: int, db: Session = Depends(get_db),
                   user=Depends(current_user)):
    from fastapi.responses import FileResponse
    p = db.query(PresentationProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    if not p.pptx_path:
        raise HTTPException(404, "PPTX не сгенерирован — нажмите «Сгенерировать»")
    import os as _os
    base = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    abs_path = _os.path.join(base, p.pptx_path.lstrip("/"))
    if not _os.path.exists(abs_path):
        raise HTTPException(404, "Файл удалён — пересоздайте презентацию")
    import re as _re
    safe = _re.sub(r"[^\w\-]", "_", p.name or "presentation")[:40]
    return FileResponse(abs_path,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        filename=f"{safe}.pptx")


@router.get("/presentations/projects/{project_id}/preview-html")
def preview_html(project_id: int, db: Session = Depends(get_db),
                  user=Depends(current_user)):
    """HTML-карусель для встраивания в iframe."""
    from fastapi.responses import FileResponse
    p = db.query(PresentationProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    if not p.html_preview:
        raise HTTPException(404, "Превью не сгенерировано")
    import os as _os
    base = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    abs_path = _os.path.join(base, p.html_preview.lstrip("/"))
    if not _os.path.exists(abs_path):
        raise HTTPException(404, "Файл превью удалён")
    return FileResponse(abs_path, media_type="text/html")
