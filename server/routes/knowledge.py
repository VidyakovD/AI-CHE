"""
RAG Knowledge API: загрузка / список / удаление / поиск файлов в базе знаний
агента или чат-бота.

Endpoint URL'ы общие — owner_type указывается query-параметром:
  POST   /knowledge/upload?owner_type=agent&owner_id=42  (multipart с файлом)
  GET    /knowledge?owner_type=agent&owner_id=42
  DELETE /knowledge/{file_id}?owner_type=agent&owner_id=42
  GET    /knowledge/search?owner_type=agent&owner_id=42&q=...

Доступ: проверяется что владелец (bot/agent) принадлежит current_user.
"""
import os
import logging
import secrets
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, BackgroundTasks
from sqlalchemy.orm import Session

from server.routes.deps import get_db, current_user
from sqlalchemy import func
from server.models import User, ChatBot, AgentConfig, KnowledgeFile
from server.knowledge import (
    add_file as kb_add_file,
    get_files, delete_file, retrieve, build_context_block,
    set_enabled, set_category,
    MAX_FILE_BYTES, MAX_FILES_PER_OWNER, MAX_TOTAL_BYTES_PER_USER,
)
from server.knowledge_classifier import CATEGORIES as KB_CATEGORIES

log = logging.getLogger(__name__)
router = APIRouter(prefix="/knowledge", tags=["knowledge"])

KB_DIR = Path("uploads/knowledge")
KB_DIR.mkdir(parents=True, exist_ok=True)
_KB_DIR_RESOLVED = KB_DIR.resolve()

_ALLOWED_EXT = {".pdf", ".docx", ".xlsx", ".xlsm", ".csv", ".tsv",
                ".txt", ".md", ".json", ".html", ".htm"}


def _check_owner(db: Session, user: User, owner_type: str, owner_id: int):
    """Проверяет, что юзер владеет указанным агентом/ботом, либо что
    owner_type='user' и owner_id == user.id (общая база юзера)."""
    if owner_type == "bot":
        bot = db.query(ChatBot).filter_by(id=owner_id, user_id=user.id).first()
        if not bot:
            raise HTTPException(404, "Бот не найден")
        return bot
    if owner_type == "agent":
        cfg = db.query(AgentConfig).filter_by(id=owner_id, user_id=user.id).first()
        if not cfg:
            raise HTTPException(404, "Агент не найден")
        return cfg
    if owner_type == "user":
        # Общая база юзера — owner_id обязан совпадать с user.id
        # (защита от попытки заливать в чужую базу под видом своей).
        if owner_id != user.id:
            raise HTTPException(403, "Можно загружать только в свою общую базу")
        return user
    raise HTTPException(400, "owner_type должен быть 'bot', 'agent' или 'user'")


def _safe_kb_path(rel: str) -> Path | None:
    if not rel:
        return None
    fname = Path(rel).name
    if not fname or fname in (".", ".."):
        return None
    candidate = (KB_DIR / fname).resolve()
    try:
        candidate.relative_to(_KB_DIR_RESOLVED)
    except ValueError:
        return None
    return candidate


@router.post("/upload")
async def kb_upload(
    background_tasks: BackgroundTasks,
    owner_type: str,
    owner_id: int,
    file: UploadFile = File(...),
    tags: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Загрузить файл в базу знаний агента или бота. Индексация в фоне."""
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email")
    _check_owner(db, user, owner_type, owner_id)

    # Валидация расширения и размера
    fname = (file.filename or "file").strip()
    ext = os.path.splitext(fname)[1].lower()
    if ext not in _ALLOWED_EXT:
        raise HTTPException(400,
            f"Тип файла не поддерживается ({ext}). Допустимы: " + ", ".join(sorted(_ALLOWED_EXT)))

    contents = await file.read(MAX_FILE_BYTES + 1)
    if len(contents) > MAX_FILE_BYTES:
        raise HTTPException(413, f"Файл больше {MAX_FILE_BYTES // 1024 // 1024} МБ")
    if len(contents) == 0:
        raise HTTPException(400, "Пустой файл")

    # Лимит файлов на конкретного владельца (бот/агент)
    cnt = (db.query(KnowledgeFile)
             .filter_by(owner_type=owner_type, owner_id=owner_id).count())
    if cnt >= MAX_FILES_PER_OWNER:
        raise HTTPException(409,
            f"Превышен лимит {MAX_FILES_PER_OWNER} файлов на бота/агента. Удалите ненужные.")
    # Глобальный лимит суммарного объёма по юзеру (across всех ботов/агентов)
    # — пока RAG-файлы не учитываются в storage-биллинге, иначе юзер может
    # бесплатно занять диск на много гигабайт через несколько ботов.
    total_bytes = (db.query(func.coalesce(func.sum(KnowledgeFile.size), 0))
                     .filter(KnowledgeFile.user_id == user.id).scalar()) or 0
    if total_bytes + len(contents) > MAX_TOTAL_BYTES_PER_USER:
        used_mb = total_bytes / 1024 / 1024
        max_mb = MAX_TOTAL_BYTES_PER_USER / 1024 / 1024
        raise HTTPException(413,
            f"Превышен суммарный лимит базы знаний ({max_mb:.0f} МБ на пользователя; "
            f"использовано {used_mb:.0f} МБ). Удалите старые файлы.")

    # Сохраняем на диск
    safe_id = secrets.token_urlsafe(12)
    safe_name = "".join(c for c in fname if c.isalnum() or c in "._-")[:60]
    if not safe_name:
        safe_name = f"file{ext}"
    abs_path = KB_DIR / f"{safe_id}_{safe_name}"
    abs_path.write_bytes(contents)
    rel_path = f"/uploads/knowledge/{safe_id}_{safe_name}"

    mime = file.content_type or None

    # Pre-check биллинг за embedding-индексацию (OpenAI text-embedding-3-small
    # стоит реальных денег: $0.02/1M токенов. На 100 МБ ≈ $0.5 нашего бюджета).
    # Если файл маленький (< embed_min_charge = 1 ₽) — индексация бесплатна.
    from server.pricing import get_price
    from server.billing import deduct_strict, credit_atomic
    from server.models import Transaction
    size_mb = max(1, len(contents) // (1024 * 1024) + (1 if len(contents) % (1024 * 1024) else 0))
    embed_cost_kop = size_mb * get_price("knowledge.embed_per_mb", default=100)
    embed_min = get_price("knowledge.embed_min_charge", default=100)
    embed_charged = 0
    if embed_cost_kop >= embed_min:
        if not deduct_strict(db, user.id, embed_cost_kop):
            # Откатим файл с диска чтобы не оставлять висеть
            try: abs_path.unlink()
            except Exception: pass
            raise HTTPException(
                402,
                f"Недостаточно средств. Индексация ~{size_mb} МБ стоит "
                f"{embed_cost_kop/100:.2f} ₽. Пополните баланс."
            )
        db.add(Transaction(
            user_id=user.id, type="usage", tokens_delta=-embed_cost_kop,
            description=f"Knowledge: индексация {fname[:80]} ({size_mb} МБ)",
            model="text-embedding-3-small",
        ))
        db.commit()
        embed_charged = embed_cost_kop

    # Запуск индексации в фоне (длительный embeddings-вызов).
    # Если падает — refund списанной за embedding суммы.
    _user_id = user.id  # capture для closure (избежать lazy-eval User-объекта)
    def _do_index():
        try:
            kb_add_file(
                owner_type=owner_type, owner_id=owner_id, user_id=_user_id,
                name=fname[:200], path=rel_path, mime=mime,
                size=len(contents), tags=tags,
            )
        except Exception as e:
            log.error(f"[KB] index task failed: {type(e).__name__}: {e}")
            if embed_charged > 0:
                # Refund — индексация упала, юзер не получил услугу
                try:
                    from server.db import db_session as _ds
                    with _ds() as _db:
                        credit_atomic(_db, _user_id, embed_charged)
                        _db.add(Transaction(
                            user_id=_user_id, type="refund",
                            tokens_delta=embed_charged,
                            description=f"Knowledge refund: индексация {fname[:80]} упала",
                        ))
                        _db.commit()
                except Exception as re:
                    log.error(f"[KB] refund failed: {type(re).__name__}: {re}")

    background_tasks.add_task(_do_index)

    try:
        from server.audit_log import log_action
        log_action("knowledge.upload", user_id=user.id, target_type="kb",
                   target_id=f"{owner_type}:{owner_id}",
                   details={"name": fname[:80], "size": len(contents)})
    except Exception:
        pass

    return {
        "status": "indexing",
        "name": fname,
        "size": len(contents),
        "path": rel_path,
        "embed_charged_kop": embed_charged,
    }


@router.get("")
def kb_list(owner_type: str, owner_id: int,
            categories: str = "",
            db: Session = Depends(get_db),
            user: User = Depends(current_user)):
    """Список файлов. categories=pricing,legal — фильтр через запятую (Knowledge Hub)."""
    _check_owner(db, user, owner_type, owner_id)
    cat_filter: list[str] | None = None
    if categories:
        cats = [c.strip() for c in categories.split(",") if c.strip() in KB_CATEGORIES]
        cat_filter = cats or None
    files = get_files(owner_type, owner_id, categories=cat_filter)
    # Распределение по категориям — для UI dropdown без второго запроса
    all_files = get_files(owner_type, owner_id) if cat_filter else files
    cat_counts: dict[str, int] = {}
    for f in all_files:
        c = f.get("category") or "other"
        cat_counts[c] = cat_counts.get(c, 0) + 1
    total_size = sum(f.get("size", 0) for f in files)
    total_chunks = sum(f.get("chunk_count", 0) for f in files)
    return {
        "files": files,
        "summary": {
            "count": len(files),
            "total_bytes": total_size,
            "total_chunks": total_chunks,
            "max_files": MAX_FILES_PER_OWNER,
            "max_file_mb": MAX_FILE_BYTES // 1024 // 1024,
            "categories": list(KB_CATEGORIES),
            "category_counts": cat_counts,
        },
    }


@router.patch("/{file_id}/category")
def kb_set_category(file_id: int, owner_type: str, owner_id: int, category: str,
                    db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    """Ручной override Knowledge Hub категории (если AI ошибся)."""
    _check_owner(db, user, owner_type, owner_id)
    if category not in KB_CATEGORIES:
        raise HTTPException(400, f"Категория должна быть одной из: {', '.join(KB_CATEGORIES)}")
    if not set_category(owner_type, owner_id, file_id, category):
        raise HTTPException(404, "Файл не найден")
    return {"id": file_id, "category": category}


@router.patch("/{file_id}/toggle")
def kb_toggle(file_id: int, owner_type: str, owner_id: int, enabled: bool,
              db: Session = Depends(get_db),
              user: User = Depends(current_user)):
    """Включить или выключить файл в RAG-поиске.
    Выключенные файлы хранятся, но не участвуют в retrieve()."""
    _check_owner(db, user, owner_type, owner_id)
    if not set_enabled(owner_type, owner_id, file_id, enabled):
        raise HTTPException(404, "Файл не найден")
    return {"id": file_id, "enabled": enabled}


@router.delete("/{file_id}")
def kb_delete(file_id: int, owner_type: str, owner_id: int,
              db: Session = Depends(get_db),
              user: User = Depends(current_user)):
    _check_owner(db, user, owner_type, owner_id)
    ok = delete_file(owner_type, owner_id, file_id)
    if not ok:
        raise HTTPException(404, "Файл не найден")
    try:
        from server.audit_log import log_action
        log_action("knowledge.delete", user_id=user.id, target_type="kb",
                   target_id=f"{owner_type}:{owner_id}:{file_id}")
    except Exception:
        pass
    return {"status": "deleted"}


@router.get("/search")
def kb_search(owner_type: str, owner_id: int, q: str, top: int = 5,
              db: Session = Depends(get_db),
              user: User = Depends(current_user)):
    """Тестовый поиск (для UI «проверь, как работает база знаний»)."""
    _check_owner(db, user, owner_type, owner_id)
    if not q or not q.strip():
        return {"results": []}
    # Лимит на длину запроса — иначе юзер может слать 100 КБ текст в
    # OpenAI embeddings за наш счёт. 1000 символов ~ 250 токенов хватает
    # для любого осмысленного запроса.
    q = q.strip()[:1000]
    if top < 1: top = 1
    if top > 20: top = 20
    results = retrieve(owner_type=owner_type, owner_id=owner_id, query=q, top=top)
    return {
        "results": results,
        "context_preview": build_context_block(results, max_chars=1500),
    }
