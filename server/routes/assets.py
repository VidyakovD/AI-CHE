"""
Хранилище файлов (лидмагниты, медиа) с тарификацией.

Юзер загружает PDF/картинку/видео → файл сохраняется на сервере →
получает короткий public-token-URL для использования в воркфлоу боте
(например output_tg_file → присылает PDF при подписке).

Биллинг: фикс ₽/месяц за каждые 100 МБ суммарного размера активных
файлов юзера. Списывается раз в месяц scheduler'ом (см. _storage_billing_tick).

API:
  GET    /assets                 — список своих файлов
  POST   /assets/upload          — загрузить файл (multipart)
  DELETE /assets/{id}            — удалить
  GET    /assets/usage           — текущий объём хранения и месячная стоимость
  GET    /assets/{public_token}  — публичная ссылка (без auth, для скачивания)
"""
import os
import uuid
import logging
import secrets
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from server.routes.deps import get_db, current_user, _user_dict
from server.models import User, StoredAsset, ChatBot
from server.pricing import get_price
from server.audit_log import log_action

log = logging.getLogger(__name__)

router = APIRouter(tags=["assets"])

ASSETS_DIR = Path("uploads/assets")
ASSETS_DIR.mkdir(parents=True, exist_ok=True)

# Лимит на размер одного файла (тарифицируется отдельно через storage)
_MAX_FILE_BYTES = 50 * 1024 * 1024   # 50 МБ — больше требует premium-плана
_ALLOWED_MIME = {
    # документы
    "application/pdf", "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/plain", "text/markdown",
    # картинки
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/svg+xml",
    # аудио/видео
    "audio/mpeg", "audio/mp4", "audio/ogg",
    "video/mp4", "video/webm", "video/quicktime",
}


def _user_total_bytes(db: Session, user_id: int) -> int:
    from sqlalchemy import func
    val = db.query(func.coalesce(func.sum(StoredAsset.size_bytes), 0)) \
            .filter(StoredAsset.user_id == user_id, StoredAsset.is_active == True) \
            .scalar()
    return int(val or 0)


def _bytes_to_100mb_units(b: int) -> int:
    """Округление вверх к ближайшим 100 МБ. Юзер платит за блоки, не за байты."""
    chunk = 100 * 1024 * 1024
    return (b + chunk - 1) // chunk


@router.get("/assets")
def list_assets(db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    """Все активные файлы юзера."""
    rows = (db.query(StoredAsset)
              .filter_by(user_id=user.id, is_active=True)
              .order_by(StoredAsset.created_at.desc())
              .all())
    return [{
        "id": a.id, "name": a.name, "mime_type": a.mime_type,
        "size_bytes": a.size_bytes,
        "size_mb": round(a.size_bytes / 1024 / 1024, 2),
        "purpose": a.purpose,
        "public_url": f"/assets/public/{a.public_token}" if a.public_token else None,
        "private_url": a.path,
        "bot_id": a.bot_id,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    } for a in rows]


@router.get("/assets/usage")
def assets_usage(db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    """Сколько занято + сколько стоит в месяц по текущей тарификации."""
    total = _user_total_bytes(db, user.id)
    units = _bytes_to_100mb_units(total)
    rate_kop = get_price("storage.per_100mb_month", default=5000)
    monthly_kop = units * rate_kop
    return {
        "total_bytes": total,
        "total_mb": round(total / 1024 / 1024, 2),
        "billed_units_100mb": units,
        "rate_per_100mb_rub": rate_kop / 100,
        "monthly_cost_rub": monthly_kop / 100,
    }


@router.post("/assets/upload")
async def upload_asset(file: UploadFile = File(...),
                       purpose: str = "general",
                       bot_id: int | None = None,
                       db: Session = Depends(get_db),
                       user: User = Depends(current_user)):
    """Загрузить файл. Сразу выдаёт public_token для использования в воркфлоу бота."""
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email")
    # Проверка mime
    mime = (file.content_type or "").lower()
    if mime and mime not in _ALLOWED_MIME:
        raise HTTPException(400, f"Тип файла {mime} не поддерживается")
    # Stream → temp → проверка размера
    contents = await file.read(_MAX_FILE_BYTES + 1)
    if len(contents) > _MAX_FILE_BYTES:
        raise HTTPException(413, f"Файл больше {_MAX_FILE_BYTES // 1024 // 1024} МБ")
    # Если bot_id задан — проверяем владение
    if bot_id is not None:
        bot = db.query(ChatBot).filter_by(id=bot_id, user_id=user.id).first()
        if not bot:
            raise HTTPException(404, "Бот не найден")
    # Сохраняем
    ext = Path(file.filename or "file.bin").suffix[:10] or ".bin"
    asset_id = uuid.uuid4().hex
    rel_path = f"/uploads/assets/{asset_id}{ext}"
    abs_path = ASSETS_DIR / f"{asset_id}{ext}"
    abs_path.write_bytes(contents)
    asset = StoredAsset(
        user_id=user.id,
        bot_id=bot_id,
        name=(file.filename or "file")[:200],
        path=rel_path,
        mime_type=mime or None,
        size_bytes=len(contents),
        public_token=secrets.token_urlsafe(16),
        purpose=purpose[:40] if purpose else "general",
        is_active=True,
    )
    db.add(asset); db.commit(); db.refresh(asset)
    log_action("asset.upload", user_id=user.id, target_type="asset",
               target_id=str(asset.id),
               details={"size_bytes": len(contents), "purpose": purpose, "mime": mime})
    return {
        "id": asset.id, "name": asset.name,
        "size_bytes": asset.size_bytes,
        "public_url": f"/assets/public/{asset.public_token}",
        "private_url": asset.path,
    }


@router.get("/assets/archived")
def list_archived(db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    """Архивированные файлы (is_active=False) — юзер может восстановить
    пока scheduler их физически не удалил (>37 дней с last_billed_at).
    """
    rows = (db.query(StoredAsset)
              .filter_by(user_id=user.id, is_active=False)
              .order_by(StoredAsset.created_at.desc())
              .all())
    from pathlib import Path as _P
    out = []
    for a in rows:
        # Проверяем что файл ещё физически существует
        p = _P(a.path.lstrip("/"))
        if not p.exists():
            continue
        out.append({
            "id": a.id, "name": a.name, "size_bytes": a.size_bytes,
            "size_mb": round(a.size_bytes / 1024 / 1024, 2),
            "purpose": a.purpose,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "last_billed_at": a.last_billed_at.isoformat() if a.last_billed_at else None,
        })
    return out


@router.post("/assets/{asset_id}/restore")
def restore_asset(asset_id: int,
                  db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    """Восстановить архивированный asset (если файл ещё на диске).
    Списываемся с баланса за один день storage'а сразу — иначе ночью архивирует
    обратно. Юзер должен пополнить баланс до восстановления.
    """
    from pathlib import Path as _P
    a = db.query(StoredAsset).filter_by(
        id=asset_id, user_id=user.id, is_active=False).first()
    if not a:
        raise HTTPException(404, "Архивный файл не найден")
    p = _P(a.path.lstrip("/"))
    if not p.exists():
        raise HTTPException(410, "Файл уже удалён физически")
    # Сразу списываем дневную ставку (~цена одного блока за день)
    rate_kop_month = get_price("storage.per_100mb_month", default=5000)
    daily_rate = max(1, rate_kop_month // 30)
    chunk = 100 * 1024 * 1024
    units = (a.size_bytes + chunk - 1) // chunk
    cost = units * daily_rate
    from server.billing import deduct_strict
    if not deduct_strict(db, user.id, cost):
        raise HTTPException(402, f"Нужно минимум {cost/100:.2f} ₽ для восстановления")
    a.is_active = True
    a.last_billed_at = datetime.utcnow()
    db.commit()
    log_action("asset.restored", user_id=user.id, target_type="asset",
               target_id=str(asset_id), details={"cost_kop": cost})
    return {"status": "restored", "id": asset_id, "cost_kop": cost}


@router.delete("/assets/{asset_id}")
def delete_asset(asset_id: int,
                 db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    """Удалить файл (фактически — soft-delete + физическое удаление с диска)."""
    a = db.query(StoredAsset).filter_by(id=asset_id, user_id=user.id).first()
    if not a:
        raise HTTPException(404, "Файл не найден")
    # Удаляем с диска
    try:
        abs_path = Path(a.path.lstrip("/"))
        if abs_path.exists():
            abs_path.unlink()
    except Exception as e:
        log.warning(f"[assets] failed to remove file {a.path}: {e}")
    a.is_active = False
    db.commit()
    log_action("asset.delete", user_id=user.id, target_type="asset",
               target_id=str(asset_id))
    return {"status": "deleted"}


@router.get("/assets/public/{public_token}")
def serve_public_asset(public_token: str, db: Session = Depends(get_db)):
    """
    Скачать файл по публичному токену (без auth).
    Используется в боте: output_tg_file шлёт юзеру URL https://aiche.ru/assets/public/<token>.
    """
    a = db.query(StoredAsset).filter_by(public_token=public_token,
                                          is_active=True).first()
    if not a:
        raise HTTPException(404, "Файл не найден или удалён")
    abs_path = Path(a.path.lstrip("/"))
    if not abs_path.exists():
        raise HTTPException(404, "Файл отсутствует на диске")
    return FileResponse(
        path=str(abs_path),
        media_type=a.mime_type or "application/octet-stream",
        filename=a.name,
    )
