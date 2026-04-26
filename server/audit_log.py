"""
Аудит-лог действий: пишет ActionLog в БД.

Использование:
    from server.audit_log import log_action

    log_action(
        action="site.generate_done",
        user_id=user.id,
        target_type="site_project", target_id=str(project.id),
        details={"tier": "premium", "size_kb": 47, "duration_sec": 180},
    )

Чтобы НЕ блокировать основной запрос при недоступности БД — обёрнуто
в try/except. Лог в файл (стандартный logger) тоже идёт — на случай
полного развала записи в БД.

Принцип: логируем ВСЕ значимые события (создания, удаления, AI-вызовы,
платежи, ошибки). Тогда в новом чате можно скинуть выгрузку и AI поймёт
что происходило в проде.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)


def log_action(
    action: str,
    *,
    user_id: int | None = None,
    target_type: str | None = None,
    target_id: str | int | None = None,
    level: str = "info",
    success: bool = True,
    details: dict[str, Any] | None = None,
    ip: str | None = None,
    request_id: str | None = None,
    error: str | None = None,
) -> None:
    """Запись действия в action_logs. Никогда не raise'ит — fail-safe.

    action: dot-notation, лучше `category.event` (auth.login, site.generate_done)
    level: info | warn | error | critical
    success: для быстрого SQL «покажи все error за сутки»
    """
    try:
        from server.db import db_session
        from server.models import ActionLog
        with db_session() as db:
            row = ActionLog(
                ts=datetime.utcnow(),
                user_id=user_id,
                action=action[:100],
                target_type=(target_type or None) and str(target_type)[:50],
                target_id=(target_id is not None) and str(target_id)[:200] or None,
                level=level if level in ("info", "warn", "error", "critical") else "info",
                success=bool(success),
                details=json.dumps(details, ensure_ascii=False, default=str)[:8000] if details else None,
                ip=(ip or None) and str(ip)[:64],
                request_id=(request_id or None) and str(request_id)[:64],
                error=(error or None) and str(error)[:2000],
            )
            db.add(row)
            db.commit()
    except Exception as e:
        # Никогда не падаем — лог это вспомогательная функция.
        # В файл пишем чтобы не потерять полностью.
        log.warning(f"[audit_log] failed write {action}: {e}")
        log.info(f"[audit_log:fallback] {action} user={user_id} target={target_type}:{target_id} "
                 f"success={success} details={details} error={error}")
