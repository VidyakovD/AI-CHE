import os, json, uuid, logging, time, threading
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Header
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import or_, func

from server.routes.deps import get_db, current_user, optional_user, _user_dict
from server.models import User, Message, Transaction, ModelPricing, UsageLog
from server.ai import generate_response, get_token_cost, resolve_model
from server.security import validate_upload_filename
from server.billing import deduct_atomic, get_balance


# ── In-memory idempotency cache для /message ────────────────────────────────
# Если клиент передаёт `Idempotency-Key`, мы кэшируем response на 5 минут.
# Двойной клик или ретрай по сетевой ошибке вернёт тот же ответ без
# повторного вызова AI и повторного списания.
# Cache живёт в процессе — для multi-worker setup нужен Redis (TODO).
_IDEMPOTENCY_TTL_SEC = 300
_idempotency_cache: dict[tuple[int, str], tuple[float, dict]] = {}
_idempotency_lock = threading.Lock()


def _idempotency_get(user_id: int, key: str) -> dict | None:
    """Возвращает кэшированный response для (user_id, key) или None."""
    if not key:
        return None
    now = time.monotonic()
    with _idempotency_lock:
        # Чистим expired по дороге
        for k, (ts, _) in list(_idempotency_cache.items()):
            if now - ts > _IDEMPOTENCY_TTL_SEC:
                _idempotency_cache.pop(k, None)
        item = _idempotency_cache.get((user_id, key))
        if item and (now - item[0]) <= _IDEMPOTENCY_TTL_SEC:
            return item[1]
    return None


def _idempotency_put(user_id: int, key: str, value: dict) -> None:
    if not key:
        return
    with _idempotency_lock:
        _idempotency_cache[(user_id, key)] = (time.monotonic(), value)


def calculate_cost(model_id: str, input_tokens: int, output_tokens: int, db: Session) -> int:
    """Посчитать стоимость в копейках за реальное использование токенов.
    Поля ModelPricing.ch_per_1k_* теперь хранят копейки/1k токенов."""
    pricing = db.query(ModelPricing).filter_by(model_id=model_id).first()
    if pricing and (pricing.ch_per_1k_input > 0 or pricing.ch_per_1k_output > 0):
        cost = (input_tokens / 1000.0) * pricing.ch_per_1k_input + \
               (output_tokens / 1000.0) * pricing.ch_per_1k_output
        cost = max(int(round(cost)), pricing.min_ch_per_req or 1)
        return cost
    # Fallback — старая per-request схема (значение тоже теперь в копейках)
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
    file_url: str | None = None       # legacy single file
    file_urls: list[str] | None = None  # multi-attach (gpt-image-1 edit)
    extra: dict | None = None

class RenameRequest(BaseModel):
    chat_id: str
    title: str


def _assert_chat_owner(chat_id: str, user, db: Session):
    msg = db.query(Message).filter(
        Message.chat_id == chat_id,
        Message.user_id == user.id,
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
        Message.user_id == user.id
    ).filter(Message.model == model).group_by(Message.chat_id).subquery()

    title_q = db.query(Message.chat_id, Message.title)\
        .filter(Message.title.isnot(None))\
        .filter(Message.model == model)\
        .filter(Message.user_id == user.id)

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
def send_message(req: MessageRequest,
                 idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
                 db: Session = Depends(get_db), user=Depends(current_user)):
    cfg = resolve_model(req.model)
    real_model = cfg["real_model"] if cfg else req.model

    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email для отправки сообщений")

    # Idempotency: повторный запрос с тем же ключом возвращает кэшированный
    # ответ и НЕ списывает баланс повторно. Защита от двойного клика.
    # Ключ ограничиваем до 80 символов и отбрасываем пустоту.
    _idem_key = (idempotency_key or "").strip()[:80]
    if _idem_key:
        cached = _idempotency_get(user.id, _idem_key)
        if cached is not None:
            return cached

    # Предварительная блокировка: списываем минимум, чтобы отсечь пустые балансы
    min_cost = 1
    pricing = db.query(ModelPricing).filter_by(model_id=real_model).first()
    if pricing:
        min_cost = pricing.min_ch_per_req or 1
    else:
        min_cost = get_token_cost(real_model) or 1

    if get_balance(db, user.id) < min_cost:
        raise HTTPException(402, "Недостаточно средств. Пополните баланс в личном кабинете.")

    existing = db.query(Message).filter_by(chat_id=req.chat_id).first()
    title = req.message[:40] if (not existing and req.message) else ("Файл" if not existing else None)

    # Сохраняем JSON если есть файл/файлы. Поддерживаются оба формата:
    # legacy {text, file_url} и новый {text, file_urls: [...]}.
    if req.file_urls:
        stored = json.dumps({"text": req.message, "file_urls": req.file_urls,
                             "file_url": req.file_urls[0]})
    elif req.file_url:
        stored = json.dumps({"text": req.message, "file_url": req.file_url})
    else:
        stored = req.message

    db.add(Message(chat_id=req.chat_id, role="user", content=stored,
                   model=req.model, title=title,
                   user_id=user.id, tokens_used=0))
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

    # Если провайдер вернул реально использованную модель (Imagen variant,
    # Veo fallback к более дешёвой версии и т.п.) — списываем по ней.
    # Так юзер платит за то что реально получил, а не за «декларированную» модель.
    actual_model = answer.get("model") if isinstance(answer, dict) else None
    cost_model = actual_model or real_model

    # Auto-refund: если запрашивали видео/картинку, а вернулся text с ошибкой
    # («Видео не сгенерировано», «Сервис временно недоступен», 429 quota и т.п.)
    # — НЕ списываем деньги. Юзер не получил товар.
    is_media_request = req.model in ("veo", "nano", "gpt-image", "dalle", "kling", "kling-pro")
    looks_like_error = (
        resp_type == "text"
        and isinstance(content, str)
        and any(marker in content for marker in (
            "не сгенерировано", "временно недоступен", "не удалось", "ошибк", "RESOURCE_EXHAUSTED"
        ))
    )
    if is_media_request and looks_like_error:
        log.warning(f"[chat] auto-refund: {req.model} вернула ошибку, не списываем. content={content[:100]}")
        try:
            from server.audit_log import log_action
            log_action("ai.media_error", user_id=user.id, target_type="chat",
                       target_id=req.chat_id, level="warn", success=False,
                       details={"model": req.model, "error_text": content[:300]},
                       error=content[:500])
        except Exception:
            pass
        # Сохраняем ответ-сообщение чтобы юзер увидел что произошло, но без списания
        db.add(Message(chat_id=req.chat_id, role="assistant", content=content,
                       model=req.model, user_id=user.id, tokens_used=0))
        db.commit()
        refunded = {"response": {"type": "text", "content": content, "ch_charged": 0,
                                  "input_tokens": 0, "output_tokens": 0, "refunded": True}}
        if _idem_key:
            _idempotency_put(user.id, _idem_key, refunded)
        return refunded

    cost = calculate_cost(cost_model, input_tokens, output_tokens, db)

    # Атомарное списание (защита от race condition при параллельных запросах)
    if cost > 0:
        charged = deduct_atomic(db, user.id, cost)
        desc = f"{req.model}: {input_tokens}→{output_tokens} ток. ({charged/100:.2f} ₽)"
        if charged < cost:
            desc += f" (списано {charged/100:.2f}/{cost/100:.2f} ₽)"
        db.add(Transaction(user_id=user.id, type="usage", tokens_delta=-charged,
                           description=desc, model=req.model))
        db.add(UsageLog(user_id=user.id, model=real_model,
                        input_tokens=input_tokens, output_tokens=output_tokens,
                        cached_tokens=answer.get("cached_tokens", 0) if isinstance(answer, dict) else 0,
                        ch_charged=charged))

    db.add(Message(chat_id=req.chat_id, role="assistant", content=content,
                   model=req.model, user_id=user.id,
                   tokens_used=cost))
    db.commit()
    # Audit-лог AI-вызова: модель, токены, цена, тип результата
    try:
        from server.audit_log import log_action
        log_action(
            "ai.chat" if resp_type == "text" else f"ai.{resp_type}",
            user_id=user.id, target_type="chat", target_id=req.chat_id,
            details={
                "model": req.model,
                "real_model": cost_model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_kop": cost,
                "type": resp_type,
            },
        )
    except Exception:
        pass
    # Пробрасываем url + model из answer (нужны для <video> и <img> тегов
    # на фронте + лейбла «модель: veo-3.0-fast-generate-001» под видео).
    resp_dict = {
        "type": resp_type, "content": content,
        "ch_charged": cost,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    if isinstance(answer, dict):
        if answer.get("url"):
            resp_dict["url"] = answer["url"]
        if answer.get("model"):
            resp_dict["model"] = answer["model"]
    final = {"response": resp_dict}
    # Кэшируем под Idempotency-Key для защиты от ретраев / двойных кликов
    if _idem_key:
        _idempotency_put(user.id, _idem_key, final)
    return final


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

    # Проверка магических байт — блокирует polyglot-файлы (JPEG с исполняемым кодом)
    _MAGIC = {
        b"\xff\xd8\xff": "jpg", b"\x89PNG\r\n\x1a\n": "png", b"GIF8": "gif",
        b"RIFF": "webp/avi", b"%PDF-": "pdf", b"BM": "bmp",
        b"\x00\x00\x00 ftyp": "mp4", b"\x1a\x45\xdf\xa3": "mkv/webm",
        b"<?xml": "svg", b"<svg": "svg", b"II*\x00": "tiff", b"MM\x00*": "tiff",
    }
    head = data[:16]
    detected = None
    for magic, kind in _MAGIC.items():
        if head.startswith(magic) or (magic == b"RIFF" and len(head) > 8 and (head[8:12] == b"WEBP" or head[8:12] == b"AVI ")):
            detected = kind; break
        if magic == b"\x00\x00\x00 ftyp" and len(data) > 8 and data[4:8] == b"ftyp":
            detected = "mp4"; break
    # .txt/.doc/.docx не проверяем по magic (офисные файлы — ZIP с хитрой структурой)
    # Но любой img/video должен иметь magic
    if ext in (".jpg", ".jpeg") and detected != "jpg":
        raise HTTPException(400, "Файл не похож на JPEG (magic bytes не совпали)")
    if ext == ".png" and detected != "png":
        raise HTTPException(400, "Файл не похож на PNG")
    if ext == ".gif" and detected != "gif":
        raise HTTPException(400, "Файл не похож на GIF")
    if ext in (".mp4", ".mov") and detected not in ("mp4",):
        raise HTTPException(400, "Файл не похож на MP4/MOV")

    # SVG / XML — бьются по содержимому (script, foreignObject, on*=, javascript:).
    # Браузер выполнит JS внутри SVG если открыть его как <img src> или <object>.
    if ext == ".svg" or detected == "svg":
        try:
            text_lower = data[:65536].decode("utf-8", errors="ignore").lower()
        except Exception:
            text_lower = ""
        _SVG_BAD = (
            "<script", "</script", "<foreignobject", "javascript:",
            " onload=", " onerror=", " onclick=", " onmouseover=",
            " onfocus=", " onblur=", " onanimation", " ontoggle=",
        )
        if any(b in text_lower for b in _SVG_BAD):
            raise HTTPException(400, "SVG содержит исполняемый код (script/on-handler) — отклонено")

    fid  = str(uuid.uuid4())
    # Sanitize filename: убираем спецсимволы, оставляем только ASCII + . _ -
    import re
    safe_name = re.sub(r"[^\w.\-]+", "_", file.filename)[:80]
    path = f"{UPLOAD_DIR}/{fid}_{safe_name}"
    with open(path, "wb") as buf:
        buf.write(data)
    return {"url": f"/uploads/{fid}_{safe_name}"}


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
