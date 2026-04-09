from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
import json, logging

from server.routes.deps import get_db, current_user, optional_user
from server.models import PresentationProject, PresentationTemplate, User, Transaction
from server.ai import generate_response

log = logging.getLogger(__name__)

router = APIRouter(tags=["presentations"])

PRES_CH_COST = 30


class CreatePresentationRequest(BaseModel):
    name: str
    template_id: int | None = None
    input_data: str | None = None  # JSON
    description: str | None = None


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
    return [{"id": p.id, "name": p.name, "status": p.status,
             "price_tokens": p.price_tokens, "template_id": p.template_id,
             "created_at": p.created_at.isoformat() if p.created_at else None} for p in projects]


@router.post("/presentations/projects")
def create_presentation_project(req: CreatePresentationRequest,
                                db: Session = Depends(get_db),
                                user: User = Depends(current_user)):
    p = PresentationProject(user_id=user.id, name=req.name, template_id=req.template_id,
                            input_data=req.input_data, status="draft")
    db.add(p); db.commit(); db.refresh(p)
    return {"id": p.id, "status": "created"}


@router.get("/presentations/projects/{project_id}")
def get_presentation_project(project_id: int, db: Session = Depends(get_db),
                              user: User = Depends(current_user)):
    p = db.query(PresentationProject).filter_by(id=project_id, user_id=user.id).first()
    if not p: raise HTTPException(404, "Проект не найден")
    return {"id": p.id, "name": p.name, "status": p.status,
            "input_data": p.input_data, "generated_content": p.generated_content,
            "price_tokens": p.price_tokens, "template_id": p.template_id,
            "created_at": p.created_at.isoformat() if p.created_at else None}


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
    if db.query(User).filter_by(id=user.id).first().tokens_balance < PRES_CH_COST:
        raise HTTPException(402, "Недостаточно токенов")
    db_user = db.query(User).filter_by(id=user.id).first()
    db_user.tokens_balance -= PRES_CH_COST
    p.price_tokens += PRES_CH_COST
    db.add(Transaction(user_id=user.id, type="usage", tokens_delta=-PRES_CH_COST,
                       description="Генерация презентации/КП"))

    tf = {}
    try: tf = json.loads(p.input_data) if p.input_data else {}
    except: pass
    tpl = db.query(PresentationTemplate).filter_by(id=p.template_id).first()
    prompt = tpl.spec_prompt if tpl else "Создай коммерческое предложение / презентацию"
    if tf:
        prompt += f"\n\nДанные от клиента:\n" + "\n".join(f"- {k}: {v}" for k,v in tf.items())

    answer = generate_response("claude", [{"role": "system", "content": prompt}], None)
    content = answer.get("content", "") if isinstance(answer, dict) else ""
    p.generated_content = content
    p.status = "done"
    db.commit()
    return {"generated_content": content, "status": p.status}
