import os, json, uuid, logging
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import or_, func

from server.routes.deps import get_db, current_user, optional_user, _user_dict
from server.models import User, Message, Transaction, ModelPricing, UsageLog
from server.ai import generate_response, get_token_cost, resolve_model
from server.security import validate_upload_filename
from server.billing import deduct_atomic, get_balance


def calculate_cost(model_id: str, input_tokens: int, output_tokens: int, db: Session) -> int:
    """Посчитать CH за реальное использование токенов."""
    pricing = db.query(ModelPricing).filter_by(model_id=model_id).first()
    if pricing and (pricing.ch_per_1k_input > 0 or pricing.ch_per_1k_output > 0):
        cost = (input_tokens / 1000.0) * pricing.ch_per_1k_input + \
               (output_tokens / 1000.0) * pricing.ch_per_1k_output
        cost = max(int(round(cost)), pricing.min_ch_per_req or 1)
        return cost
    # Fallback — старая per-request схема
    if pricing and pricing.cost_per_req:
        return pricing.cost_per_req
    return get_token_cost(model_id)

log = logging.getLogger(__name__)

UPLOAD_DIR = "uploads"
UPLOAD_MAX_IMAGE = 10 * 1024 * 1024
UPLOAD_MAX_VIDEO = 50 * 1024 * 1024
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".tiff", ".pdf"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".avi", ".mkv"}

router = APIRouter(tags=["chat"])


class CreateChatRequest(BaseModel):
    model: str

class MessageRequest(BaseModel):
    chat_id: str
    message: str
    model: str
    file_url: str | None = None
    extra: dict | None = None

class RenameRequest(BaseModel):
    chat_id: str
    title: str


def _assert_chat_owner(chat_id: str, user, db: Session):
    msg = db.query(Message).filter(
        Message.chat_id == chat_id,
        or_(Message.user_id == user.id, Message.user_id == None)
    ).first()
    if not msg:
        raise HTTPException(403, "Нет доступа к этому чату")


@router.post("/chat/create")
def create_chat(req: CreateChatRequest):
    return {"chat_id": str(uuid.uuid4()), "model": req.model}


@router.get("/chat/{chat_id}")
def get_chat(chat_id: str, db: Session = Depends(get_db), user=Depends(current_user)):
    _assert_chat_owner(chat_id, user, db)
    msgs = db.query(Message).filter_by(chat_id=chat_id).order_by(Message.id).all()
    return [{"role": m.role, "content": m.content, "created_at": m.created_at.isoformat() if m.created_at else None} for m in msgs]


@router.post("/chat/rename")
def rename_chat(req: RenameRequest, db: Session = Depends(get_db), user=Depends(current_user)):
    _assert_chat_owner(req.chat_id, user, db)
    msg = db.query(Message).filter_by(chat_id=req.chat_id).first()
    if not msg: raise HTTPException(404, "Чат не найден")
    msg.title = req.title; db.commit()
    return {"status": "ok"}


@router.delete("/chat/{chat_id}")
def delete_chat(chat_id: str, db: Session = Depends(get_db), user=Depends(current_user)):
    _assert_chat_owner(chat_id, user, db)
    msgs = db.query(Message).filter_by(chat_id=chat_id).all()
    for m in msgs: db.delete(m)
    db.commit()
    return {"status": "deleted"}


@router.get("/chats/{model}")
def get_chats(model: str, db: Session = Depends(get_db), user=Depends(optional_user)):
    if not user:
        return []
    subq = db.query(
        Message.chat_id,
        func.max(Message.created_at).label("last_msg")
    ).filter(
        or_(Message.user_id == user.id, Message.user_id == None)
    ).filter(Message.model == model).group_by(Message.chat_id).subquery()

    title_q = db.query(Message.chat_id, Message.title)\
        .filter(Message.title.isnot(None))\
        .filter(Message.model == model)\
        .filter(or_(Message.user_id == user.id, Message.user_id == None))

    titles = {}
    for cid, title in title_q.all():
        if cid not in titles:
            titles[cid] = title

    rows = db.query(subq.c.chat_id, subq.c.last_msg)\
        .order_by(subq.c.last_msg.desc()).all()

    result = []
    for cid, _ in rows:
        if cid in titles:
            result.append({"id": cid, "title": titles[cid]})
    return result


@router.post("/message")
def send_message(req: MessageRequest, db: Session = Depends(get_db), user=Depends(optional_user)):
    cfg = resolve_model(req.model)
    real_model = cfg["real_model"] if cfg else req.model

    if user and not user.is_verified:
        raise HTTPException(403, "Подтвердите email для отправки сообщений")

    # Предварительная блокировка: списываем минимум, чтобы отсечь пустые балансы
    min_cost = 1
    pricing = db.query(ModelPricing).filter_by(model_id=real_model).first()
    if pricing:
        min_cost = pricing.min_ch_per_req or 1
    else:
        min_cost = get_token_cost(real_model) or 1

    if user:
        if get_balance(db, user.id) < min_cost:
            raise HTTPException(402, "Недостаточно токенов. Пополните баланс в личном кабинете.")

    existing = db.query(Message).filter_by(chat_id=req.chat_id).first()
    title = req.message[:40] if (not existing and req.message) else ("Файл" if not existing else None)

    stored = json.dumps({"text": req.message, "file_url": req.file_url}) \
             if req.file_url else req.message

    db.add(Message(chat_id=req.chat_id, role="user", content=stored,
                   model=req.model, title=title,
                   user_id=user.id if user else None, tokens_used=0))
    db.commit()

    history = db.query(Message).filter_by(chat_id=req.chat_id)\
                .order_by(Message.id).all()[-20:]

    def parse(c):
        try:
            p = json.loads(c)
            if isinstance(p, dict) and "file_url" in p: return p
        except (json.JSONDecodeError, TypeError):
            pass
        return c

    formatted = [{"role": "system", "content": "Ты полезный AI ассистент."}] + \
                [{"role": m.role, "content": parse(m.content)} for m in history]
    try:
        answer = generate_response(req.model, formatted, req.extra)
    except Exception as e:
        log.error(f"AI error [{req.model}]: {e}")
        return {"error": "Сервис временно недоступен. Попробуйте ещё раз."}

    content   = answer.get("content", "") if isinstance(answer, dict) else answer
    resp_type = answer.get("type", "text") if isinstance(answer, dict) else "text"
    input_tokens  = answer.get("input_tokens", 0) if isinstance(answer, dict) else 0
    output_tokens = answer.get("output_tokens", 0) if isinstance(answer, dict) else 0

    # Реальная стоимость по токенам
    cost = calculate_cost(real_model, input_tokens, output_tokens, db)

    # Атомарное списание (защита от race condition при параллельных запросах)
    if user and cost > 0:
        charged = deduct_atomic(db, user.id, cost)
        desc = f"{req.model}: {input_tokens}→{output_tokens} ток."
        if charged < cost:
            desc += f" (списано {charged}/{cost})"
        db.add(Transaction(user_id=user.id, type="usage", tokens_delta=-charged,
                           description=desc, model=req.model))
        db.add(UsageLog(user_id=user.id, model=real_model,
                        input_tokens=input_tokens, output_tokens=output_tokens,
                        cached_tokens=answer.get("cached_tokens", 0) if isinstance(answer, dict) else 0,
                        ch_charged=charged))

    db.add(Message(chat_id=req.chat_id, role="assistant", content=content,
                   model=req.model, user_id=user.id if user else None,
                   tokens_used=cost))
    db.commit()
    return {
        "response": {
            "type": resp_type, "content": content,
            "ch_charged": cost,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
    }


@router.post("/upload")
def upload_file(file: UploadFile = File(...), user=Depends(optional_user)):
    if not user:
        raise HTTPException(401, "Нужна авторизация для загрузки файлов")
    validate_upload_filename(file.filename)

    data = file.file.read()
    file.file.seek(0)
    ext = os.path.splitext(file.filename)[1].lower()
    if ext in IMAGE_EXTS:
        limit = UPLOAD_MAX_IMAGE
        label = "10 МБ"
    elif ext in VIDEO_EXTS:
        limit = UPLOAD_MAX_VIDEO
        label = "50 МБ"
    else:
        raise HTTPException(400, f"Неподдерживаемый тип файла: {ext}")

    if len(data) > limit:
        raise HTTPException(413, f"Файл слишком большой (макс. {label})")

    fid  = str(uuid.uuid4())
    path = f"{UPLOAD_DIR}/{fid}_{file.filename}"
    with open(path, "wb") as buf:
        buf.write(data)
    return {"url": f"/uploads/{fid}_{file.filename}"}


@router.get("/kling/status/{task_id}")
def kling_status(task_id: str, db: Session = Depends(get_db), user=Depends(current_user)):
    msg = db.query(Message).filter(
        Message.user_id == user.id,
        Message.content.contains(task_id)
    ).first()
    if not msg:
        raise HTTPException(403, "Нет доступа к этой задаче")
    import httpx as hx
    keys = [k.strip() for k in os.getenv("KLING_API_KEYS", "").split(",") if k.strip()]
    if not keys:
        raise HTTPException(503, "No Kling keys")
    try:
        r = hx.get(f"https://api.klingai.com/v1/videos/text2video/{task_id}",
                   headers={"Authorization": f"Bearer {keys[0]}"}, timeout=15)
        return r.json()
    except hx.TimeoutException:
        raise HTTPException(504, "Kling API timeout")
