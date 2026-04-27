"""Управление собственными API-ключами пользователя."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from server.routes.deps import get_db, current_user
from server.models import UserApiKey

router = APIRouter(prefix="/user/api-keys", tags=["user-api-keys"])

ALLOWED_PROVIDERS = {"openai", "anthropic", "gemini", "grok"}


class ApiKeyCreate(BaseModel):
    provider: str
    api_key: str
    label: str | None = None


@router.get("")
def list_keys(user=Depends(current_user), db: Session = Depends(get_db)):
    keys = db.query(UserApiKey).filter_by(user_id=user.id).all()
    return [
        {
            "id": k.id,
            "provider": k.provider,
            "label": k.label,
            "key_preview": k.api_key[:8] + "..." + k.api_key[-4:] if len(k.api_key) > 12 else "***",
            "created_at": k.created_at,
        }
        for k in keys
    ]


@router.post("")
def add_key(body: ApiKeyCreate, user=Depends(current_user), db: Session = Depends(get_db)):
    if body.provider not in ALLOWED_PROVIDERS:
        raise HTTPException(400, f"Провайдер должен быть одним из: {', '.join(ALLOWED_PROVIDERS)}")
    key = (body.api_key or "").strip()
    if len(key) < 10:
        raise HTTPException(400, "API-ключ слишком короткий (минимум 10 символов)")
    existing = db.query(UserApiKey).filter_by(user_id=user.id, provider=body.provider).first()
    if existing:
        existing.api_key = key
        existing.label = body.label
    else:
        db.add(UserApiKey(user_id=user.id, provider=body.provider,
                          api_key=key, label=body.label))
    db.commit()
    # Сбросить кэш — следующий AI-вызов сразу подхватит новый ключ
    try:
        from server.chatbot_engine import invalidate_user_keys_cache
        invalidate_user_keys_cache(user.id)
    except Exception:
        pass
    from server.audit_log import log_action
    log_action("user.apikey_added", user_id=user.id, target_type="apikey",
               target_id=body.provider, details={"provider": body.provider})
    return {"status": "ok"}


@router.delete("/{key_id}")
def delete_key(key_id: int, user=Depends(current_user), db: Session = Depends(get_db)):
    k = db.query(UserApiKey).filter_by(id=key_id, user_id=user.id).first()
    if not k:
        raise HTTPException(404, "Ключ не найден")
    provider = k.provider
    db.delete(k)
    db.commit()
    try:
        from server.chatbot_engine import invalidate_user_keys_cache
        invalidate_user_keys_cache(user.id)
    except Exception:
        pass
    from server.audit_log import log_action
    log_action("user.apikey_removed", user_id=user.id, target_type="apikey",
               target_id=provider, details={"provider": provider})
    return {"status": "ok"}
