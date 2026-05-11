"""
Общий helper для фоновых asyncio-задач.

asyncio.create_task возвращает Task, но event loop хранит только weakref —
если caller не сохраняет сильную ссылку, Python GC может собрать task
до её завершения. Это особенно опасно для:
  - fire-and-forget HTTP-вызовов (webhooks, CRM dispatch)
  - запуска orchestra runs/restage из роутера
  - параллельных вызовов через ThreadPoolExecutor wrapper'а

Использование:

    from server._async_tasks import spawn

    spawn(my_async_func(...))  # вместо asyncio.create_task(...)

Бесконечные циклы (scheduler-loops) не нуждаются в spawn() — там цикл
держит task живым своим телом. Это helper для одноразовых задач.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Coroutine, Any

log = logging.getLogger(__name__)


_pending: set[asyncio.Task] = set()


def spawn(coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task:
    """Запустить background-задачу и сохранить ссылку до завершения.

    Returns: Task — caller может await/cancel, или забыть.
    """
    task = asyncio.create_task(coro, name=name)
    _pending.add(task)
    task.add_done_callback(_on_done)
    return task


def _on_done(task: asyncio.Task) -> None:
    _pending.discard(task)
    # Если task завершился с исключением — логируем (иначе оно «съедается» asyncio).
    if not task.cancelled():
        exc = task.exception()
        if exc:
            log.error(f"[spawn] background task '{task.get_name()}' failed: {type(exc).__name__}: {exc}")


def pending_count() -> int:
    """Сколько spawn'нутых задач сейчас в полёте — для healthcheck/мониторинга."""
    return len(_pending)
