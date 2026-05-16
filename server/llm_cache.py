"""LLM Cache — hash-based exact-match для экономии повторных вызовов.

Это MVP (не семантический). Семантика через embeddings — TODO когда будет
большой объём трафика. Для онбординга / типовых запросов / повторных
prompt'ов даёт 30-40% hit-rate легко.

Usage:
    from server.llm_cache import cached_generate_response
    result = cached_generate_response("claude-sonnet", messages,
                                      extra={"_purpose": "agent_builder"},
                                      ttl_sec=3600)
    # Под капотом — get_or_set: проверяет cache, если miss → вызывает
    # generate_response, сохраняет.

Не кэшируем:
- Streaming-вызовы (нет смысла, разные result каждый раз)
- Запросы со временем (содержат timestamp / "сейчас")
- Запросы с attachments (файлы могут меняться)
- temperature > 0.5 (модель сама даёт разные ответы)

Очистка expired записей — раз в сутки через scheduler.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

# Поля extra которые НЕ влияют на контент → не учитываем в hash
_EXTRA_NON_CONTENT_KEYS = {
    "_user_id", "_request_id", "_user_key", "_purpose",
    "_router_task", "_router_complexity",
}

# _purpose'ы для которых cache ОБЯЗАТЕЛЬНО per-user. Личный агент в onboarding
# с пустым профилем даёт идентичный system-prompt для двух новых юзеров →
# ответ одного «протечёт» другому. Хешируем user_id в ключ.
_PER_USER_PURPOSES = {
    "personal_agent",
    "agent_builder",
    "module",  # match for "module:smm" / "module:lawyer" / ...
}

DEFAULT_TTL_SEC = 3600  # 1 час


def _make_cache_key(model: str, messages: list, extra: dict | None) -> str:
    """SHA256 от model + canonical-JSON(messages) + JSON(filtered extra).
    Для _purpose из _PER_USER_PURPOSES — подмешиваем _user_id в namespace."""
    extra_filtered = {}
    namespace = ""
    if extra:
        # Только content-keys (max_tokens, temperature, etc.)
        extra_filtered = {k: v for k, v in extra.items()
                          if k not in _EXTRA_NON_CONTENT_KEYS}
        purpose = str(extra.get("_purpose", ""))
        # purpose может содержать "module:smm" — берём корневую часть
        purpose_root = purpose.split(":", 1)[0]
        if purpose_root in _PER_USER_PURPOSES or purpose in _PER_USER_PURPOSES:
            uid = extra.get("_user_id")
            if uid is not None:
                namespace = f"u{uid}"
    payload = {
        "m": model,
        "msg": messages or [],
        "x": extra_filtered,
        "ns": namespace,
    }
    # sort_keys для стабильности
    s = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _should_cache(model: str, messages: list, extra: dict | None) -> bool:
    """Решаем кэшировать или нет — по эвристикам безопасности."""
    if not messages:
        return False
    # Высокая temperature → разные ответы → не кэшируем
    if extra:
        t = extra.get("temperature")
        if isinstance(t, (int, float)) and t > 0.5:
            return False
        # Streaming-вызовы — не кэшируем (вернётся накопленный, потеря смысла)
        if extra.get("stream"):
            return False
    # Запросы с attachments (содержат file_url) — может меняться
    for m in messages:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list):
            for blk in c:
                if isinstance(blk, dict) and blk.get("file_url"):
                    return False
        if isinstance(c, str) and any(t in c.lower() for t in
                                       ("сейчас", "today", "now",
                                        "вчера", "сегодня", "current")):
            return False
    return True


def get(model: str, messages: list, extra: dict | None) -> dict | None:
    """Найти в кэше. Возвращает dict-результат или None."""
    if not _should_cache(model, messages, extra):
        return None
    key = _make_cache_key(model, messages, extra)
    try:
        from server.db import db_session
        from server.models import LlmCache
        now = datetime.utcnow()
        with db_session() as db:
            row = db.query(LlmCache).filter_by(cache_key=key).first()
            if not row:
                return None
            if row.expires_at < now:
                # Expired — удаляем
                db.delete(row)
                db.commit()
                return None
            # Hit — увеличиваем счётчик
            row.hit_count = (row.hit_count or 0) + 1
            row.last_hit_at = now
            response = row.response
            db.commit()
        try:
            data = json.loads(response)
            log.debug(f"[llm_cache] HIT model={model} key={key[:10]}")
            return data
        except Exception as e:
            log.warning(f"[llm_cache] bad JSON in cache key={key[:10]}: {e}")
            return None
    except Exception as e:
        # Кэш — best-effort. Никогда не ломаем основной flow.
        log.warning(f"[llm_cache] get failed: {e}")
        return None


def put(model: str, messages: list, extra: dict | None,
        response: dict, ttl_sec: int = DEFAULT_TTL_SEC) -> None:
    """Сохранить в кэш. Не бросает — best-effort."""
    if not _should_cache(model, messages, extra):
        return
    if not isinstance(response, dict):
        return
    # Не кэшируем неудачные ответы
    content = response.get("content", "")
    if not content or "ошибка" in str(content).lower()[:50]:
        return
    key = _make_cache_key(model, messages, extra)
    try:
        from server.db import db_session
        from server.models import LlmCache
        purpose = (extra or {}).get("_purpose", "")[:80] if extra else None
        now = datetime.utcnow()
        expires = now + timedelta(seconds=ttl_sec)
        with db_session() as db:
            existing = db.query(LlmCache).filter_by(cache_key=key).first()
            if existing:
                existing.response = json.dumps(response, ensure_ascii=False)
                existing.expires_at = expires
                existing.model = model
                existing.purpose = purpose
            else:
                db.add(LlmCache(
                    cache_key=key, model=model, purpose=purpose,
                    response=json.dumps(response, ensure_ascii=False),
                    expires_at=expires,
                ))
            db.commit()
        log.debug(f"[llm_cache] SET model={model} key={key[:10]} ttl={ttl_sec}s")
    except Exception as e:
        log.warning(f"[llm_cache] put failed: {e}")


def cached_generate_response(model: str, messages: list,
                             extra: dict | None = None,
                             user_api_key: str | None = None,
                             ttl_sec: int = DEFAULT_TTL_SEC) -> dict:
    """Drop-in замена generate_response с кэшем.

    Алгоритм:
      1. Если кэшируемо → get() из БД. Hit → возвращаем сразу.
      2. Miss → вызываем generate_response()
      3. Если ok → put() в кэш
    """
    cached = get(model, messages, extra)
    if cached is not None:
        return cached
    from server.ai import generate_response
    result = generate_response(model, messages, extra=extra, user_api_key=user_api_key)
    if isinstance(result, dict) and result.get("content"):
        put(model, messages, extra, result, ttl_sec=ttl_sec)
    return result


def cleanup_expired() -> int:
    """Удалить просроченные записи. Возвращает кол-во удалённых.
    Вызывается раз в сутки scheduler'ом."""
    try:
        from server.db import db_session
        from server.models import LlmCache
        now = datetime.utcnow()
        with db_session() as db:
            res = db.query(LlmCache).filter(LlmCache.expires_at < now).delete(synchronize_session=False)
            db.commit()
        if res:
            log.info(f"[llm_cache] cleanup: deleted {res} expired entries")
        return int(res or 0)
    except Exception as e:
        log.warning(f"[llm_cache] cleanup failed: {e}")
        return 0


def stats() -> dict:
    """Краткая статистика для админки/мониторинга."""
    try:
        from server.db import db_session
        from server.models import LlmCache
        from sqlalchemy import func
        with db_session() as db:
            total = db.query(func.count(LlmCache.cache_key)).scalar() or 0
            total_hits = db.query(func.coalesce(func.sum(LlmCache.hit_count), 0)).scalar() or 0
            top = (db.query(LlmCache.model, func.count(LlmCache.cache_key),
                            func.coalesce(func.sum(LlmCache.hit_count), 0))
                     .group_by(LlmCache.model)
                     .order_by(func.coalesce(func.sum(LlmCache.hit_count), 0).desc())
                     .limit(10).all())
        return {
            "total_entries": int(total),
            "total_hits": int(total_hits),
            "by_model": [
                {"model": m or "—", "entries": int(c), "hits": int(h)}
                for m, c, h in top
            ],
        }
    except Exception as e:
        return {"error": str(e)}
