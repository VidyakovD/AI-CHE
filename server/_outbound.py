"""Общий dispatcher для outbound HTTP-запросов (webhooks + CRM).

Абстрагирует общий поток:
  1. Re-валидируем URL на dispatch-time (DNS-резолв / private CIDR).
  2. POST с timeout, без redirects.
  3. Обновляем БД: last_status / last_called_at / total_calls / fail_count.
  4. Auto-disable после N последовательных fail'ов.

Раньше webhooks.py и crm.py имели ~150 строк копипаста. Теперь они только
формируют body+headers и вызывают `dispatch_outbound(...)`.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Type

import httpx
from sqlalchemy import update

from server.db import db_session

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SEC = 10
DEFAULT_MAX_FAIL = 10


@dataclass
class OutboundResult:
    """Итог одной отправки: статус, ошибка, флаг доставки."""
    status: int | None = None
    error: str | None = None
    delivered: bool = False


def _post_http(url: str, body: bytes | None, json_body: dict | None,
                headers: dict[str, str], timeout_sec: int) -> OutboundResult:
    """Чистый HTTP POST без DB-сайд-эффектов. Возвращает OutboundResult.

    Один из {body, json_body} должен быть задан; body имеет приоритет
    (нужно для webhooks где HMAC считается по тем же байтам что отправляются).
    """
    out = OutboundResult()
    try:
        with httpx.Client(timeout=timeout_sec, follow_redirects=False) as client:
            if body is not None:
                r = client.post(url, content=body, headers=headers)
            else:
                r = client.post(url, json=json_body, headers=headers)
            out.status = r.status_code
            out.delivered = 200 <= r.status_code < 300
            if not out.delivered:
                # Не сохраняем тело юзеровского endpoint'а в last_error —
                # туда могут попасть секреты (creds в DB-traceback'е,
                # API-токены в payload). См. комментарий в P2 batch.
                body_len = len(r.text or "")
                out.error = f"HTTP {r.status_code} (body {body_len} bytes)"
    except httpx.TimeoutException:
        out.error = "Timeout"
    except httpx.HTTPError as e:
        out.error = f"{type(e).__name__}: {str(e)[:200]}"
    except Exception as e:
        out.error = f"{type(e).__name__}: {str(e)[:200]}"
    return out


def _persist_status(model_cls: Type[Any], row_id: int, result: OutboundResult,
                     max_fail: int) -> None:
    """Atomic UPDATE статуса + auto-disable по достижении max_fail.

    Требует от model_cls наличия колонок:
      last_status, last_called_at, last_error,
      total_calls, fail_count, is_active.
    """
    now = datetime.utcnow()
    with db_session() as db:
        if result.delivered:
            db.execute(
                update(model_cls)
                .where(model_cls.id == row_id)
                .values(
                    last_status=result.status,
                    last_called_at=now,
                    last_error=None,
                    total_calls=model_cls.total_calls + 1,
                    fail_count=0,
                )
            )
        else:
            db.execute(
                update(model_cls)
                .where(model_cls.id == row_id)
                .values(
                    last_status=result.status,
                    last_called_at=now,
                    last_error=(result.error or "")[:200] or None,
                    total_calls=model_cls.total_calls + 1,
                    fail_count=model_cls.fail_count + 1,
                )
            )
            db.execute(
                update(model_cls)
                .where(model_cls.id == row_id)
                .where(model_cls.fail_count >= max_fail)
                .values(is_active=False)
            )
        db.commit()


def _disable_with_error(model_cls: Type[Any], row_id: int, error_msg: str) -> None:
    """Pre-flight reject: устанавливает last_error и is_active=False.

    Используется когда URL не прошёл DNS-валидацию (private/loopback).
    Лучше отрубить интеграцию явно, чем тихо терять каждый event.
    """
    with db_session() as db:
        db.execute(
            update(model_cls)
            .where(model_cls.id == row_id)
            .values(last_error=error_msg[:200], is_active=False)
        )
        db.commit()


def dispatch_outbound(
    *,
    model_cls: Type[Any],
    row_id: int,
    url: str,
    body: bytes | None = None,
    json_body: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    max_fail: int = DEFAULT_MAX_FAIL,
    log_prefix: str = "outbound",
) -> OutboundResult:
    """Высокоуровневая отправка: валидация URL → POST → обновление БД.

    Возвращает OutboundResult. Не бросает исключения — ловит всё.

    Пример (webhooks.py):
        result = dispatch_outbound(
            model_cls=ApiWebhook, row_id=webhook_id,
            url=w.url, body=signed_body,
            headers={"X-Aiche-Signature": signature, ...},
            log_prefix=f"webhook {webhook_id}",
        )
    """
    if body is None and json_body is None:
        raise ValueError("dispatch_outbound: нужен body или json_body")
    if body is not None and json_body is not None:
        raise ValueError("dispatch_outbound: body и json_body взаимоисключающие")

    # ── Pre-flight: defense-in-depth re-валидация URL ─────────────────────
    # URL валидируется при создании, но если когда-нибудь добавим PUT-эндпоинт
    # или БД-row будет изменён иначе — мы не должны POST'ить на 127.0.0.1.
    # DNS rebinding ловится здесь же (resolve каждый раз).
    from server.proposal_builder import validate_outbound_url
    try:
        validate_outbound_url(url, allow_http=True)
    except ValueError as e:
        msg = f"URL rejected: {e}"
        log.warning(f"[{log_prefix}] {msg}")
        _disable_with_error(model_cls, row_id, msg)
        return OutboundResult(status=None, error=msg, delivered=False)

    # ── HTTP POST ────────────────────────────────────────────────────────
    result = _post_http(
        url=url, body=body, json_body=json_body,
        headers=headers or {}, timeout_sec=timeout_sec,
    )

    # ── Сохраняем статус ─────────────────────────────────────────────────
    try:
        _persist_status(model_cls, row_id, result, max_fail)
    except Exception as e:
        log.warning(f"[{log_prefix}] persist_status failed: {e}")
    return result


# ── Хелперы, общие для webhooks.py и crm.py ───────────────────────────────

def serialize_payload(payload: dict) -> bytes:
    """JSON → bytes детерминированно (sort_keys=True для верификации HMAC).

    Без sort_keys одна и та же payload может быть сериализована в разном
    порядке ключей на разных Python-версиях, и подпись на стороне приёмника
    не сойдётся.
    """
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def hmac_sign_sha256(secret: str, body: bytes, *, prefix: str = "sha256=") -> str:
    """HMAC-SHA256 подпись body. Формат: `<prefix><hex>` (по умолчанию `sha256=`)."""
    h = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"{prefix}{h}"


def result_to_dict(result: OutboundResult) -> dict:
    """OutboundResult → {status, error, delivered} для UI/test endpoint'ов."""
    return {
        "status": result.status if result.status is not None else "error",
        "error": result.error,
        "delivered": result.delivered,
    }
