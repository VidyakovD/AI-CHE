"""
Advisory-локи для фоновых задач через SQLite.

Зачем: uvicorn --workers 2 стартует `scheduler_loop`/`imap_loop`/`apikey_check_loop`
в каждом процессе. Без координации scheduled workflows выполнялись бы дважды,
IMAP письма обрабатывались бы дважды, health-check API-ключей спамил бы.

Принцип:
  with worker_lock('scheduler_tick', ttl_sec=25):
      # только один процесс выполнит этот блок
      ...

Реализация — SQLite строка с expires_at. Кто первый вставил/продлил — тот и
держит лок. Другие процессы видят что лок жив и пропускают tick.
"""
import contextlib
import logging
import os
import sqlite3
import time

log = logging.getLogger(__name__)

_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".worker_locks.db")
_INIT = False


def _conn():
    global _INIT
    conn = sqlite3.connect(_DB, timeout=3.0, isolation_level=None)
    if not _INIT:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS locks (name TEXT PRIMARY KEY, expires_at REAL)"
        )
        _INIT = True
    return conn


@contextlib.contextmanager
def worker_lock(name: str, ttl_sec: float = 30.0):
    """
    Контекст-менеджер advisory lock.
    Блок внутри выполняется только если удалось получить лок на `name`.
    Если лок занят — context выставит переменную `acquired=False`
    (через `lock_holder.acquired`), чтобы caller мог skip работу.

    Usage:
        lh = worker_lock.__enter__(...)
        # или:
        with worker_lock('name') as acquired:
            if not acquired:
                return  # другой worker уже делает
            # ... работа ...
    """
    acquired = _try_acquire(name, ttl_sec)
    try:
        yield acquired
    finally:
        if acquired:
            _release(name)


def _try_acquire(name: str, ttl_sec: float) -> bool:
    now = time.time()
    try:
        conn = _conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT expires_at FROM locks WHERE name=?", (name,)
            ).fetchone()
            if row and row[0] > now:
                conn.execute("COMMIT")
                return False
            # Лок свободен или просрочен — забираем
            conn.execute(
                "INSERT OR REPLACE INTO locks(name, expires_at) VALUES (?, ?)",
                (name, now + ttl_sec),
            )
            conn.execute("COMMIT")
            return True
        finally:
            conn.close()
    except Exception as e:
        # Fail-CLOSED. Лучше пропустить tick (его сделает другой воркер
        # или мы сами на следующем цикле), чем выполнить задачу дважды:
        # IMAP-письмо обработается дважды → ответ отправится дважды;
        # scheduled workflow триггернётся дважды → деньги списываются 2×.
        log.warning(f"[worker_lock] {name}: acquire failed (skipping tick): {e}")
        return False


def _release(name: str):
    try:
        conn = _conn()
        try:
            conn.execute("DELETE FROM locks WHERE name=?", (name,))
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"[worker_lock] {name}: release failed: {e}")
