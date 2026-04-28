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
from server.models import User, ChatBot, AgentConfig, KnowledgeFile
from server.knowledge import (
    add_file as kb_add_file,
    get_files, delete_file, retrieve, build_context_block,
    set_enabled,
    MAX_FILE_BYTES, MAX_FILES_PER_OWNER,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/knowledge", tags=["knowledge"])

KB_DIR = Path("uploads/knowledge")
KB_DIR.mkdir(parents=True, exist_ok=True)
_KB_DIR_RESOLVED = KB_DIR.resolve()

_ALLOWED_EXT = {".pdf", ".docx", ".xlsx", ".xlsm", ".csv", ".tsv",
                ".txt", ".md", ".json", ".html", ".htm"}


def _check_owner(db: Session, user: User, owner_type: str, owner_id: int):
    """Проверяет, что юзер владеет указанным агентом/ботом."""
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
    raise HTTPException(400, "owner_type должен быть 'bot' или 'agent'")


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

    # Лимит файлов
    cnt = (db.query(KnowledgeFile)
             .filter_by(owner_type=owner_type, owner_id=owner_id).count())
    if cnt >= MAX_FILES_PER_OWNER:
        raise HTTPException(409,
            f"Превышен лимит {MAX_FILES_PER_OWNER} файлов. Удалите ненужные.")

    # Сохраняем на диск
    safe_id = secrets.token_urlsafe(12)
    safe_name = "".join(c for c in fname if c.isalnum() or c in "._-")[:60]
    if not safe_name:
        safe_name = f"file{ext}"
    abs_path = KB_DIR / f"{safe_id}_{safe_name}"
    abs_path.write_bytes(contents)
    rel_path = f"/uploads/knowledge/{safe_id}_{safe_name}"

    mime = file.content_type or None

    # Запуск индексации в фоне (длительный embeddings-вызов)
    def _do_index():
        try:
            kb_add_file(
                owner_type=owner_type, owner_id=owner_id, user_id=user.id,
                name=fname[:200], path=rel_path, mime=mime,
                size=len(contents), tags=tags,
            )
        except Exception as e:
            log.error(f"[KB] index task failed: {type(e).__name__}: {e}")

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
    }


@router.get("")
def kb_list(owner_type: str, owner_id: int,
            db: Session = Depends(get_db),
            user: User = Depends(current_user)):
    _check_owner(db, user, owner_type, owner_id)
    files = get_files(owner_type, owner_id)
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
        },
    }


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
    if top < 1: top = 1
    if top > 20: top = 20
    results = retrieve(owner_type=owner_type, owner_id=owner_id, query=q, top=top)
    return {
        "results": results,
        "context_preview": build_context_block(results, max_chars=1500),
    }
