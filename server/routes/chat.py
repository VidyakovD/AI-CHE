import os, json, uuid, logging
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import or_, func

from server.routes.deps import get_db, current_user, optional_user, _user_dict
from server.models import User, Message, Transaction
from server.ai import generate_response, get_token_cost, resolve_model
from server.security import validate_upload_filename

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
    cost = get_token_cost(cfg["real_model"] if cfg else req.model)
    if user and not user.is_verified:
        raise HTTPException(403, "Подтвердите email для отправки сообщений")

    existing = db.query(Message).filter_by(chat_id=req.chat_id).first()
    title = None
    if not existing:
        title = req.message[:40] if req.message else "Файл"

    stored = json.dumps({"text": req.message, "file_url": req.file_url}) \
             if req.file_url else req.message

    if user:
        db_user = db.query(User).filter_by(id=user.id).first()
        if db_user.tokens_balance < cost:
            raise HTTPException(402, "Недостаточно токенов. Пополните баланс в личном кабинете.")
        db_user.tokens_balance -= cost
        db.add(Transaction(user_id=user.id, type="usage", tokens_delta=-cost,
                           description=f"Запрос к {req.model}", model=req.model))

    db.add(Message(chat_id=req.chat_id, role="user", content=stored,
                   model=req.model, title=title,
                   user_id=user.id if user else None, tokens_used=cost))
    db.commit()

    history = db.query(Message).filter_by(chat_id=req.chat_id)\
                .order_by(Message.id).all()[-20:]

    def parse(c):
        try:
            p = json.loads(c)
            if isinstance(p, dict) and "file_url" in p: return p
        except: pass
        return c

    formatted = [{"role": "system", "content": "Ты полезный AI ассистент."}] + \
                [{"role": m.role, "content": parse(m.content)} for m in history]
    try:
        answer = generate_response(req.model, formatted, req.extra)
    except Exception as e:
        log.error(f"AI error [{req.model}]: {e}")
        if user:
            db_user = db.query(User).filter_by(id=user.id).first()
            if db_user:
                db_user.tokens_balance += cost
                log.info(f"Refunded {cost} CH to user {user.id} (AI error)")
        return {"error": "Сервис временно недоступен. Попробуйте ещё раз."}

    content   = answer.get("content", "") if isinstance(answer, dict) else answer
    resp_type = answer.get("type", "text") if isinstance(answer, dict) else "text"

    db.add(Message(chat_id=req.chat_id, role="assistant", content=content,
                   model=req.model, user_id=user.id if user else None))
    db.commit()
    return {"response": {"type": resp_type, "content": content}}


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
