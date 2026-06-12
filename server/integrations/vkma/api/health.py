"""VKMA health endpoint — для мониторинга работы surface.

GET  /api/vkma/health        — без auth, базовый ping
GET  /api/vkma/me            — с current_vkma_user, возвращает данные юзера
                                (валидирует launch_params + создаёт юзера если новый)

Stage 2 ставит этот роутер чтобы при деплое можно было curl-проверить
что VKMA-surface поднялся. Stage 3 добавит остальные эндпоинты.
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends

from server.integrations.vkma.auth import (
    _is_production, _vk_app_id, _vk_secure_key, current_vkma_user,
)
from server.models import User
from server.routes.deps import kop_to_rub

log = logging.getLogger("vkma-api")
router = APIRouter(prefix="/api/vkma", tags=["vkma"])


@router.get("/health")
def vkma_health() -> dict:
    """Без auth. Проверяет что surface поднят + env-переменные настроены."""
    return {
        "ok": True,
        "service": "aiche-vkma",
        "version": "1",
        "config": {
            "vk_app_id_set": bool(_vk_app_id()),
            "vk_secure_key_set": bool(_vk_secure_key()),
            "token_encryption_key_set": bool((os.getenv("TOKEN_ENCRYPTION_KEY") or "").strip()),
            "production_mode": _is_production(),
        },
    }


@router.get("/me")
def vkma_me(user: User = Depends(current_vkma_user)) -> dict:
    """Получить данные текущего VK MiniApp-юзера.

    Принимает launch_params в header `X-VK-Launch-Params`. Валидирует HMAC
    (только в проде), находит/создаёт User. Возвращает базовые поля.

    Идемпотентно — повторный вызов с теми же launch_params вернёт того
    же юзера. Дубликаты не создаются (UNIQUE по vk_user_id).
    """
    return {
        "user_id": user.id,
        "name": user.name,
        "email": user.email,
        "vk_user_id": user.vk_user_id,
        "balance_kop": int(user.tokens_balance or 0),
        "balance_rub": kop_to_rub(user.tokens_balance),
        "trial_ends_at": (user.trial_ends_at.isoformat()
                          if user.trial_ends_at else None),
        "pd_consent_at": (user.pd_consent_at.isoformat()
                          if user.pd_consent_at else None),
        "is_verified": user.is_verified,
    }
