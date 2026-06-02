import os, json, uuid, logging, time, threading
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Header
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import or_, func

from server.routes.deps import get_db, current_user, optional_user, _user_dict
from server.models import User, Message, Transaction, ModelPricing, UsageLog
from server.ai import generate_response, get_token_cost, resolve_model
from server.security import validate_upload_filename
from server.billing import deduct_atomic, get_balance


# ── DB-based idempotency для /message (multi-worker safe) ───────────────────
# Если клиент передаёт `Idempotency-Key`, мы кэшируем response на 5 минут.
# Двойной клик или ретрай по сетевой ошибке вернёт тот же ответ без
# повторного вызова AI и повторного списания.
#
# Реализация через таблицу IdempotencyRecord с UNIQUE(user_id, key).
# Корректно работает при любом числе uvicorn-воркеров: SQLite-уровень UNIQUE
# гарантирует, что только один запрос пройдёт первым; остальные получат
# IntegrityError и вернут кэшированный response.
#
# Cleanup: scheduler.py каждые 60 сек удаляет записи старше 5 минут.
_IDEMPOTENCY_TTL_SEC = 300


def _idempotency_get(db, user_id: int, key: str) -> dict | None:
    """Возвращает кэшированный response для (user_id, key) если запись свежая.
    Stale-записи (старше TTL) игнорируются — будут удалены в scheduler-loop.
    """
    if not key:
        return None
    from server.models import IdempotencyRecord
    cutoff = datetime.utcnow() - timedelta(seconds=_IDEMPOTENCY_TTL_SEC)
    rec = (db.query(IdempotencyRecord)
             .filter(IdempotencyRecord.user_id == user_id,
                     IdempotencyRecord.key == key,
                     IdempotencyRecord.created_at >= cutoff)
             .first())
    if not rec or not rec.response_json:
        return None
    try:
        return json.loads(rec.response_json)
    except Exception:
        return None


def _idempotency_put(db, user_id: int, key: str, value: dict) -> bool:
    """Сохранить response. UPDATE'ит существующую запись (которая была создана
    атомарно при reservation в endpoint /message). Возвращает True если
    успешно, False если запись не нашлась или сериализация слишком большая.

    Защита от больших responses: если сериализация >50 КБ — не сохраняем.
    """
    if not key:
        return False
    from server.models import IdempotencyRecord
    try:
        payload = json.dumps(value, ensure_ascii=False)
    except Exception:
        return False
    if len(payload) > 50_000:
        return False
    try:
        rec = (db.query(IdempotencyRecord)
                 .filter(IdempotencyRecord.user_id == user_id,
                         IdempotencyRecord.key == key)
                 .first())
        if rec is None:
            # Reservation не был сделан (legacy/internal call) — INSERT новой
            rec = IdempotencyRecord(user_id=user_id, key=key, response_json=payload)
            db.add(rec)
        else:
            rec.response_json = payload
        db.commit()
        return True
    except Exception:
        db.rollback()
        return False


def _start_idempotency_sweeper():
    """Заглушка для совместимости — реальная очистка теперь в scheduler.py
    через _idempotency_cleanup_loop, который каждую минуту удаляет
    записи старше 5 минут."""
    pass


def calculate_cost(model_id: str, input_tokens: int, output_tokens: int,
                   db: Session, alt_model_id: str | None = None) -> int:
    """Backward-compat обёртка: возвращает int копеек (округление через accumulator
    делается в caller). Для точной float-стоимости — calculate_cost_float()."""
    cost_f = calculate_cost_float(model_id, input_tokens, output_tokens,
                                    db, alt_model_id)
    return max(int(round(cost_f)), 0)


def calculate_cost_float(model_id: str, input_tokens: int, output_tokens: int,
                         db: Session, alt_model_id: str | None = None) -> float:
    """Точная стоимость в копейках (float, может быть < 1).
    Поля ModelPricing.ch_per_1k_* хранят коп/1k (после dynamic recalc — могут
    быть дробными, например 0.0042). Округление до целых копеек — задача
    accumulator-биллинга (server.billing.deduct_with_accumulator).

    alt_model_id: alias-имя (req.model вроде "perplexity") если основной
    real_model ("sonar") не найден в ModelPricing.
    """
    def _lookup(mid: str) -> float | None:
        if not mid:
            return None
        p = db.query(ModelPricing).filter_by(model_id=mid).first()
        if not p:
            return None
        if p.ch_per_1k_input > 0 or p.ch_per_1k_output > 0:
            c = (input_tokens / 1000.0) * float(p.ch_per_1k_input) + \
                (output_tokens / 1000.0) * float(p.ch_per_1k_output)
            return c
        if p.cost_per_req:
            return float(p.cost_per_req)
        return None

    cost = _lookup(model_id)
    if cost is None and alt_model_id and alt_model_id != model_id:
        cost = _lookup(alt_model_id)
    if cost is not None:
        return cost
    return float(get_token_cost(model_id))

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
    # Если клиент не прислал ключ — выводим стабильный авто-ключ из (chat_id,
    # модель, content-hash) с окном 30 секунд. Это закрывает кейс двойного
    # клика / network retry даже если фронт забыл поставить заголовок.
    _idem_key = (idempotency_key or "").strip()[:80]
    if not _idem_key:
        import hashlib
        _msg_for_hash = (req.message or "")[:2000]
        _files_for_hash = ",".join(sorted(req.file_urls or [req.file_url] if req.file_url else []))
        _bucket = int(time.time() // 30)  # окно 30 сек
        _idem_key = "auto:" + hashlib.sha256(
            f"{req.chat_id}|{req.model}|{_bucket}|{_msg_for_hash}|{_files_for_hash}".encode()
        ).hexdigest()[:32]
    cached = _idempotency_get(db, user.id, _idem_key)
    if cached is not None:
        return cached

    # RESERVATION: пытаемся atomic-вставить пустую запись для key. Если
    # удалось — мы «первый», продолжаем. Если IntegrityError (UNIQUE conflict) —
    # параллельный воркер уже взял в работу: ждём до 30 сек его ответ, после
    # таймаута возвращаем 409. Это закрывает race: раньше двое воркеров оба
    # делали LLM-вызов и оба списывали деньги, проигравший только потом видел
    # raced cache. Теперь только один проходит дальше.
    from server.models import IdempotencyRecord as _IR
    try:
        db.add(_IR(user_id=user.id, key=_idem_key, response_json=None))
        db.commit()
    except Exception:
        db.rollback()
        # Конкурентный воркер взял в работу. Ждём его ответ.
        _wait_started = time.monotonic()
        while time.monotonic() - _wait_started < 30:
            cached2 = _idempotency_get(db, user.id, _idem_key)
            if cached2 is not None:
                return cached2
            time.sleep(0.5)
        raise HTTPException(409, "Параллельный запрос уже в обработке. Попробуйте позже.")

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
    _raw_in  = answer.get("input_tokens", 0) if isinstance(answer, dict) else 0
    _raw_out = answer.get("output_tokens", 0) if isinstance(answer, dict) else 0
    # CAP против битого usage от провайдера (если вернёт 1млрд токенов — юзер
    # не должен потерять весь баланс за один запрос).
    _USAGE_CAP = 1_000_000
    input_tokens  = min(int(_raw_in or 0), _USAGE_CAP) if _raw_in else 0
    output_tokens = min(int(_raw_out or 0), _USAGE_CAP) if _raw_out else 0
    if input_tokens != (_raw_in or 0) or output_tokens != (_raw_out or 0):
        log.warning(f"[chat] usage clamped: in={_raw_in}→{input_tokens}, out={_raw_out}→{output_tokens}, model={req.model}")

    # Если провайдер вернул реально использованную модель (Imagen variant,
    # Veo fallback к более дешёвой версии и т.п.) — списываем по ней.
    # Так юзер платит за то что реально получил, а не за «декларированную» модель.
    actual_model = answer.get("model") if isinstance(answer, dict) else None
    cost_model = actual_model or real_model

    # Auto-refund: если запрашивали видео/картинку, а вернулся text с ошибкой
    # («Видео не сгенерировано», «Сервис временно недоступен», 429 quota и т.п.)
    # — НЕ списываем деньги. Юзер не получил товар.
    is_media_request = req.model in (
        "veo", "nano", "gpt-image", "dalle",
        "kling", "kling-pro",  # legacy aliases
        "kling-1-6", "kling-1-6-pro",
        "kling-2", "kling-2-pro", "kling-2-1", "kling-3",
    )
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
            _idempotency_put(db, user.id, _idem_key, refunded)
        return refunded

    # Точная float-стоимость (поддерживает дробные копейки), accumulator
    # переносит остатки между запросами. Например при цене 0.0042 коп/1k и
    # 100 токенах — cost=0.00042 коп, не теряется, копится до 1 коп.
    cost_float = calculate_cost_float(cost_model, input_tokens, output_tokens,
                                       db, alt_model_id=req.model)
    cost = int(round(cost_float))  # для отображения и audit log

    # Атомарное списание через accumulator (защита от race condition).
    if cost_float > 0:
        from server.billing import deduct_with_accumulator
        charged = deduct_with_accumulator(db, user.id, cost_float)
        if charged > 0 or cost_float >= 0.005:  # пишем транзакцию даже на 0 коп если cost ощутимый
            desc = f"{req.model}: {input_tokens}→{output_tokens} ток. ({cost_float/100:.4f} ₽)"
            if charged > 0 and abs(charged - cost_float) > 0.5:
                desc += f" (списано {charged/100:.2f} ₽)"
            elif charged == 0:
                desc += " (накоплено в аккаунте)"
            db.add(Transaction(user_id=user.id, type="usage",
                               tokens_delta=-charged,
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
    # Кэшируем под Idempotency-Key для защиты от ретраев / двойных кликов.
    # При гонке (другой воркер успел записать первым) — читаем его response.
    if _idem_key:
        if not _idempotency_put(db, user.id, _idem_key, final):
            raced = _idempotency_get(db, user.id, _idem_key)
            if raced is not None:
                return raced
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
        from server.security import sanitize_svg_or_raise
        sanitize_svg_or_raise(data)

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
        raise HTTPException(503, "Видео-генератор Kling временно недоступен. Попробуйте через несколько минут.")
    try:
        r = hx.get(f"https://api.klingai.com/v1/videos/text2video/{task_id}",
                   headers={"Authorization": f"Bearer {keys[0]}"}, timeout=15)
        return r.json()
    except hx.TimeoutException:
        raise HTTPException(504, "Kling не успел обработать запрос. Видео ещё может быть готово — обновите статус через минуту.")
