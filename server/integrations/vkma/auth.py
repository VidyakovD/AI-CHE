"""VKMA auth — VK Mini App launch_params HMAC validation + identity resolution.

Это **отдельный путь** auth, параллельно с aiche-стандартным
`server.routes.deps.current_user` (cookie/JWT для web). Используется в
эндпоинтах под `/api/vkma/*`.

Flow:
  1. Frontend (React Mini App в VK) получает launch_params от VK Bridge
  2. Frontend шлёт их в header `X-VK-Launch-Params: vk_user_id=X&vk_app_id=Y&...&sign=Z`
  3. Backend валидирует HMAC-SHA256 от sorted vk_* params с VK_APP_SECURE_KEY
  4. Если valid → ищет/создаёт User по vk_user_id (тот же flow что
     `/internal/v1/identify` через `_find_or_create_user`)
  5. Возвращает User (через FastAPI Depends)

Опционально (Stage 3+):
  - После первого identify выдавать стандартный aiche JWT, чтобы
    последующие запросы шли через обычный `current_user`. Это уменьшит
    overhead парсинга launch_params на каждый запрос.

Env:
  VK_APP_ID            — id приложения VK (для проверки vk_app_id из params)
  VK_APP_SECURE_KEY    — secret key для HMAC. БЕЗ него auth = 503 в проде.
  TOKEN_ENCRYPTION_KEY — base64 от 32 байт, для AES-256-GCM шифрования VK
                          community access_token'ов в vkma_communities.

В dev-режиме (APP_ENV != "production") HMAC не валидируется — это
позволяет тестировать Mini App в обычном браузере без VK-окружения.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
from typing import Optional
from urllib.parse import parse_qsl, urlencode

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from server.models import User
from server.routes.deps import get_db

log = logging.getLogger("vkma-auth")


def _is_production() -> bool:
    return (os.getenv("APP_ENV") or "").lower() == "production"


def _vk_secure_key() -> Optional[str]:
    k = (os.getenv("VK_APP_SECURE_KEY") or "").strip()
    return k or None


def _vk_app_id() -> Optional[str]:
    a = (os.getenv("VK_APP_ID") or "").strip()
    return a or None


# ── HMAC validation ──────────────────────────────────────────────────────


def validate_vk_launch_params(query_string: str) -> Optional[dict[str, str]]:
    """Проверить подпись VK Mini App launch_params.

    https://dev.vk.com/mini-apps/auth

    Args:
        query_string: сырой query string из launch URL — формат
            "vk_user_id=1&vk_app_id=123&...&sign=<base64url>"

    Returns:
        Распарсенные params dict если подпись валидна. None — иначе.

    В dev (APP_ENV != "production") — пропускает без HMAC, парсит как есть.
    Это нужно для тестирования Mini App вне VK-окружения.
    """
    if not query_string:
        return None

    # В dev пропускаем без HMAC
    if not _is_production():
        return dict(parse_qsl(query_string))

    secure_key = _vk_secure_key()
    if not secure_key:
        # Прод без secure_key — fail closed
        log.error("[vkma-auth] VK_APP_SECURE_KEY не задан — отбиваю всё")
        return None

    params = dict(parse_qsl(query_string))
    sign = params.pop("sign", None)
    if not sign:
        return None

    vk_params = {k: v for k, v in params.items() if k.startswith("vk_")}
    if not vk_params:
        return None

    ordered = urlencode(sorted(vk_params.items()))
    expected = (
        base64.urlsafe_b64encode(
            hmac.new(
                secure_key.encode(),
                ordered.encode(),
                hashlib.sha256,
            ).digest()
        )
        .decode()
        .rstrip("=")
    )

    if not hmac.compare_digest(expected, sign):
        return None

    # Опционально: проверка vk_app_id (защита от использования launch_params
    # из чужого приложения если secure_key совпал — параноидально, но дёшево).
    expected_app_id = _vk_app_id()
    if expected_app_id and params.get("vk_app_id") not in (None, expected_app_id):
        log.warning(f"[vkma-auth] vk_app_id mismatch: "
                    f"got {params.get('vk_app_id')!r}, expected {expected_app_id!r}")
        return None

    return params


# ── AES-256-GCM для VK community access_token ────────────────────────────


def _get_aes_key() -> bytes:
    """TOKEN_ENCRYPTION_KEY → 32 raw bytes для AES-256-GCM.

    Ключ должен быть base64-encoded от ровно 32 байт. Сгенерить:
        openssl rand -base64 32
    """
    key = (os.getenv("TOKEN_ENCRYPTION_KEY") or "").strip()
    if not key:
        raise ValueError("TOKEN_ENCRYPTION_KEY не задан")
    try:
        decoded = base64.b64decode(key)
    except Exception as exc:
        raise ValueError("TOKEN_ENCRYPTION_KEY должен быть base64") from exc
    if len(decoded) != 32:
        raise ValueError(f"TOKEN_ENCRYPTION_KEY должен декодироваться в 32 байта (сейчас {len(decoded)})")
    return decoded


def encrypt_vk_token(plaintext: str) -> str:
    """Зашифровать VK community access_token (для vkma_communities)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    aes = AESGCM(_get_aes_key())
    nonce = os.urandom(12)
    ct = aes.encrypt(nonce, plaintext.encode(), None)
    return base64.b64encode(nonce + ct).decode()


def decrypt_vk_token(ciphertext_b64: str) -> str:
    """Расшифровать VK community access_token."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    aes = AESGCM(_get_aes_key())
    raw = base64.b64decode(ciphertext_b64)
    nonce, ct = raw[:12], raw[12:]
    return aes.decrypt(nonce, ct, None).decode()


# ── FastAPI dependency ───────────────────────────────────────────────────


def current_vkma_user(
    request: Request,
    x_vk_launch_params: Optional[str] = Header(None, alias="X-VK-Launch-Params"),
    db: Session = Depends(get_db),
) -> User:
    """FastAPI dependency: получить User из VK launch_params.

    Используется в роутах /api/vkma/*. **Параллельно** с aiche-стандартным
    `current_user` — не заменяет его, /api/* остальные продолжают по cookie/JWT.

    Auto-create нового User при первом вызове (как `/internal/v1/identify`).
    Trial credits начисляются автоматически (multi_surface.trial_credits_kop).
    """
    # Источник launch_params: header > query string
    params_str = x_vk_launch_params or request.url.query
    if not params_str:
        raise HTTPException(401, "Missing VK launch_params (header X-VK-Launch-Params or query)")

    params = validate_vk_launch_params(params_str)
    if params is None:
        raise HTTPException(401, "Invalid VK launch_params signature")

    vk_user_id = params.get("vk_user_id")
    if not vk_user_id:
        raise HTTPException(400, "VK launch_params missing vk_user_id")

    # Identify/create — переиспользуем существующую логику из Internal API
    # чтобы trial-кредиты + единая политика auto-create работали единообразно.
    from server.routes.internal_api import _find_user_by_identifier, _set_identifier
    from datetime import datetime, timedelta
    from server.pricing import get_price
    import secrets

    user = _find_user_by_identifier(db, "vk_user_id", str(vk_user_id))
    if user:
        if getattr(user, "is_banned", False):
            raise HTTPException(403, "Аккаунт заблокирован")
        return user

    # Auto-create. Тот же flow что в /internal/v1/identify для kind="vk_user_id".
    trial_kop = max(0, int(get_price("multi_surface.trial_credits_kop", default=50_000)))
    trial_days = max(0, int(get_price("multi_surface.trial_days", default=14)))
    trial_ends = (datetime.utcnow() + timedelta(days=trial_days)
                  if trial_days > 0 else None)

    display_name = (
        (params.get("vk_user_id") and f"VK {params['vk_user_id']}") or "VK user"
    )
    synthetic_email = f"vk-{vk_user_id}-{secrets.token_hex(4)}@aiche.local"

    new_user = User(
        email=synthetic_email,
        password_hash="!",
        name=display_name,
        is_verified=False,
        is_active=True,
        agreed_to_terms=True,
        tokens_balance=trial_kop,
        trial_ends_at=trial_ends,
        referral_code=secrets.token_hex(4).upper(),
    )
    _set_identifier(new_user, "vk_user_id", str(vk_user_id))
    db.add(new_user)
    db.flush()
    if trial_kop > 0:
        from server.models import Transaction
        db.add(Transaction(
            user_id=new_user.id, type="bonus",
            tokens_delta=trial_kop,
            description=f"[vkma] trial credits on first VK MiniApp visit",
        ))
    db.commit()
    db.refresh(new_user)
    log.info(f"[vkma-auth] auto-created user_id={new_user.id} for vk_user_id={vk_user_id}")
    return new_user
