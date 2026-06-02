"""Одноразовые pairing-коды для multi-surface identity linking.

Используется:
  - web (/user/tg-link/aiche-bot/code) — генерация кода для UI
  - Internal API (/internal/v1/link/code/issue + redeem) — для внешних клиентов
  - @aiche_bot (/start LINK_<code>) — обмен кода на привязку tg_user_id
  - @aiche_max (по аналогии в будущем)

Хранилище — in-memory dict {code: (user_id, kind, expires_at)}. TTL 10 мин.
Для multi-worker (4 gunicorn-воркеров) код может «пропадать» между воркерами,
но это OK: юзер тогда просто перегенерирует. Для durable storage в будущем —
заменить на Redis или БД-таблицу.

Threadsafe через простой Lock — операции с dict короткие.
"""
from __future__ import annotations

import secrets
import threading
import time
from typing import Optional


_VALID_KINDS = {"tg_user_id", "max_user_id", "vk_user_id", "phone"}

# code -> (user_id, kind, expires_at_monotonic)
_CODES: dict[str, tuple[int, str, float]] = {}
_LOCK = threading.Lock()

DEFAULT_TTL_SEC = 600  # 10 минут


def _gc_locked() -> None:
    """Очистка expired кодов. Caller должен держать lock."""
    now = time.monotonic()
    expired = [c for c, (_, _, exp) in _CODES.items() if exp < now]
    for c in expired:
        _CODES.pop(c, None)


def issue_code(user_id: int, kind: str, ttl_sec: int = DEFAULT_TTL_SEC) -> str:
    """Сгенерировать pairing-код для линковки identifier'а к юзеру.

    Args:
        user_id: id юзера на aiche.ru (источник истины)
        kind: какой identifier ожидается при redeem
        ttl_sec: TTL кода в секундах (default 600 = 10 мин)

    Returns:
        6-значный код в виде строки "AB12CD" (4 буквы латиницы + 2 цифры
        для эстетики и удобства диктовки голосом).
    """
    if kind not in _VALID_KINDS:
        raise ValueError(f"Invalid kind {kind!r}. Valid: {sorted(_VALID_KINDS)}")
    if not user_id:
        raise ValueError("user_id required")
    # Buchstaben (без 0/O/1/I/L — лучше различимость): A-Z minus confusing
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ"
    digits = "23456789"  # без 0 и 1
    code = (
        "".join(secrets.choice(alphabet) for _ in range(4))
        + "".join(secrets.choice(digits) for _ in range(2))
    )
    with _LOCK:
        _gc_locked()
        _CODES[code] = (int(user_id), kind, time.monotonic() + ttl_sec)
    return code


def redeem_code(code: str, value: str) -> Optional[tuple[int, str]]:
    """Обменять код на привязку. Возвращает (user_id, kind) или None.

    Не пишет ничего в БД — caller сам делает UPDATE users SET <kind>=value
    WHERE id=user_id (через _set_identifier или прямой запрос).

    Args:
        code: 6-символьный код
        value: значение identifier'а (нужен только для логов/контракта)

    Returns:
        (user_id, kind) если код найден и не просрочен. Иначе None.
        Код удаляется при успешном redeem (single-use).
    """
    if not code or not value:
        return None
    with _LOCK:
        _gc_locked()
        item = _CODES.pop(code, None)  # one-time use, pop сразу
    if not item:
        return None
    user_id, kind, exp = item
    if exp < time.monotonic():
        return None
    return (user_id, kind)


def peek_code(code: str) -> Optional[tuple[int, str]]:
    """Посмотреть код БЕЗ потребления. Для тестов/админки. None если нет/expired."""
    with _LOCK:
        item = _CODES.get(code)
    if not item:
        return None
    user_id, kind, exp = item
    if exp < time.monotonic():
        return None
    return (user_id, kind)


def _reset_for_tests() -> None:
    """Очистить весь стейт. Только для unit-тестов."""
    with _LOCK:
        _CODES.clear()
