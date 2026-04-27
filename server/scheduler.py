"""
Scheduler для воркфлоу с триггером `trigger_schedule`.
Запускается при старте main.py, проверяет каждую минуту — пришло ли время.
"""
import asyncio, logging, json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from server.db import SessionLocal
from server.models import ChatBot
from server.chatbot_engine import _execute_workflow

log = logging.getLogger("scheduler")

# Запоминаем последний запуск каждой (bot_id, node_id) пары
_last_fired: dict[tuple[int, str], datetime] = {}


def _should_fire(cfg: dict, now_local: datetime, last_fired: datetime | None) -> bool:
    """Нужно ли запускать расписание СЕЙЧАС (в локальном времени бота)."""
    mode = cfg.get("mode", "daily")

    if mode == "interval":
        minutes = int(cfg.get("interval_min", 15) or 15)
        if last_fired is None: return True
        return (now_local - last_fired).total_seconds() >= minutes * 60

    if mode == "hourly":
        # Срабатывает в начале каждого часа
        if last_fired is None: return now_local.minute == 0
        return now_local.hour != last_fired.hour and now_local.minute == 0

    if mode == "custom":
        # Простой парсер cron "M H D M W"
        cron = (cfg.get("cron") or "").strip()
        parts = cron.split()
        if len(parts) != 5: return False
        m, h, d, mo, w = parts
        def match(val: int, spec: str) -> bool:
            if spec == "*": return True
            if "," in spec: return any(match(val, s) for s in spec.split(","))
            if "/" in spec:
                base, step = spec.split("/", 1)
                if base == "*": return val % int(step) == 0
                return val >= int(base) and (val - int(base)) % int(step) == 0
            if "-" in spec:
                a, b = spec.split("-", 1)
                return int(a) <= val <= int(b)
            return val == int(spec)
        fired_this_minute = (last_fired and (now_local - last_fired).total_seconds() < 60)
        if fired_this_minute: return False
        return (match(now_local.minute, m) and match(now_local.hour, h)
                and match(now_local.day, d) and match(now_local.month, mo)
                and match(now_local.isoweekday() % 7, w))

    # daily / weekly — по HH:MM
    time_str = (cfg.get("time") or "09:00").strip()
    try:
        hh, mm = [int(x) for x in time_str.split(":", 1)]
    except Exception:
        return False

    if now_local.hour != hh or now_local.minute != mm:
        return False

    # Не стреляем дважды в ту же минуту
    if last_fired and (now_local - last_fired).total_seconds() < 60:
        return False

    if mode == "daily":
        return True

    if mode == "weekly":
        wd = now_local.isoweekday()  # 1-7 (Пн=1)
        allowed = {int(x.strip()) for x in (cfg.get("weekdays") or "1,2,3,4,5").split(",") if x.strip().isdigit()}
        return wd in allowed

    return False


async def _scheduler_tick():
    """Одна проверка — бежим по всем активным ботам и их расписаниям.

    Оптимизация: pre-фильтруем в SQL по подстроке "trigger_schedule" в
    workflow_json. На 1000 ботов отсекает 95%+ нерелевантных строк
    без полной загрузки JSON в Python.
    """
    db = SessionLocal()
    try:
        bots = (
            db.query(ChatBot)
            .filter_by(status="active")
            .filter(ChatBot.workflow_json.isnot(None))
            .filter(ChatBot.workflow_json.like('%trigger_schedule%'))
            .all()
        )
    finally:
        db.close()

    for bot in bots:
        if not bot.workflow_json:
            continue
        try:
            wf = json.loads(bot.workflow_json)
        except Exception:
            continue
        nodes = wf.get("wfc_nodes", [])
        schedule_nodes = [n for n in nodes if n.get("type") == "trigger_schedule"]
        if not schedule_nodes:
            continue

        for n in schedule_nodes:
            cfg = n.get("cfg", {})
            tz_name = cfg.get("tz") or "Asia/Yekaterinburg"
            try:
                tz = ZoneInfo(tz_name)
            except Exception:
                tz = ZoneInfo("UTC")
            now_local = datetime.now(tz)
            key = (bot.id, n.get("id"))
            last = _last_fired.get(key)
            if last and last.tzinfo is None:
                last = last.replace(tzinfo=tz)
            if _should_fire(cfg, now_local, last):
                log.info(f"[Scheduler] firing bot={bot.id} node={n.get('id')} mode={cfg.get('mode')}")
                _last_fired[key] = now_local
                # Запустить граф с пустым входом
                try:
                    await _execute_workflow(
                        bot=bot,
                        chat_id=f"schedule_{n.get('id')}",
                        user_text="",
                        platform="schedule",
                        user_name="Scheduler",
                        workflow=wf,
                    )
                except Exception as e:
                    log.error(f"[Scheduler] bot={bot.id} node={n.get('id')} error: {e}")


async def scheduler_loop():
    """Главный цикл — проверка каждые 30 секунд.
    Advisory lock гарантирует что при нескольких workers tick выполнится один раз."""
    from server.worker_lock import worker_lock
    log.info("Scheduler started")
    while True:
        try:
            with worker_lock("scheduler_tick", ttl_sec=25) as acquired:
                if acquired:
                    await _scheduler_tick()
                # иначе другой worker уже выполняет — пропускаем
        except Exception as e:
            log.error(f"[Scheduler] tick error: {e}")
        await asyncio.sleep(30)


# ══ Auto-check API ключей раз в час + алерт админу при поломке ═══════════════
_last_apikey_check: datetime | None = None
_APIKEY_CHECK_INTERVAL = timedelta(hours=1)
_last_alerted_broken_ids: set[int] = set()


async def _apikey_check_tick():
    """Проверяет все API-ключи раз в час. Шлёт email админу если статус сломался."""
    global _last_apikey_check, _last_alerted_broken_ids
    now = datetime.utcnow()
    if _last_apikey_check and (now - _last_apikey_check) < _APIKEY_CHECK_INTERVAL:
        return
    _last_apikey_check = now

    from server.models import ApiKey
    from server.routes.admin import _test_key

    db = SessionLocal()
    try:
        keys = db.query(ApiKey).all()
    finally:
        db.close()

    # Проверяем в executor (OpenAI/Anthropic SDK синхронные)
    loop = asyncio.get_event_loop()
    broken = []
    for key in keys:
        try:
            status, error = await loop.run_in_executor(None, _test_key, key.provider, key.key_value)
        except Exception as e:
            status, error = "error", str(e)
        # Апдейт в БД в отдельной сессии
        db = SessionLocal()
        try:
            k = db.query(ApiKey).filter_by(id=key.id).first()
            if k:
                k.status = status
                k.last_error = error
                k.last_check = datetime.utcnow()
                db.commit()
        finally:
            db.close()
        if status == "error":
            broken.append((key, error))

    # Алерт админу если появились новые сломанные ключи
    new_broken = {k.id for k, _ in broken} - _last_alerted_broken_ids
    if new_broken:
        _alert_admin_broken_keys([b for b in broken if b[0].id in new_broken])
        _last_alerted_broken_ids = {k.id for k, _ in broken}
    else:
        # Если ключи починились — сбросим
        _last_alerted_broken_ids = {k.id for k, _ in broken}


def _alert_admin_broken_keys(broken: list):
    """Шлёт email админу со списком сломанных ключей."""
    import os
    admins = [e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()]
    if not admins:
        return
    try:
        from server.email_service import _send, _base_template
        items = "".join(f"<li><b>{k.provider}</b>: {e or 'нет ответа'}</li>" for k, e in broken)
        body = f"""
        <p style="color:rgba(199,196,215,0.8);line-height:1.6">Автоматическая проверка обнаружила {len(broken)} сломанных API-ключа:</p>
        <ul style="color:rgba(199,196,215,0.8)">{items}</ul>
        <p style="color:rgba(199,196,215,0.7);font-size:13px">Юзеры получают «Сервис временно недоступен». Обновите ключ в /admin.html → API Ключи.</p>"""
        for admin_email in admins:
            _send(admin_email, "⚠️ API-ключи сломаны — AI Студия Че",
                  _base_template("Сломаны API-ключи", body))
    except Exception as e:
        log.error(f"[apikey alert] email failed: {e}")


async def apikey_check_loop():
    """Проверка API-ключей раз в час в фоне (с advisory lock для multi-worker)."""
    from server.worker_lock import worker_lock
    log.info("API-key health-check started")
    # Первая проверка через 5 минут после старта (чтобы не тормозить холодный старт)
    await asyncio.sleep(300)
    while True:
        try:
            with worker_lock("apikey_check", ttl_sec=3500) as acquired:
                if acquired:
                    await _apikey_check_tick()
        except Exception as e:
            log.error(f"[apikey check] error: {e}")
        await asyncio.sleep(3600)


async def _cleanup_old_pdfs_tick():
    """Удаляет PDF-отчёты бизнес-решений старше 30 дней.
    Без этого /uploads/solutions/ растёт неограниченно — каждый run = новый PDF."""
    import os, time
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    folder = os.path.join(base, "uploads", "solutions")
    if not os.path.isdir(folder):
        return
    cutoff = time.time() - 30 * 86400
    removed = 0
    for name in os.listdir(folder):
        if not name.startswith("sol_") or not name.endswith(".pdf"):
            continue
        path = os.path.join(folder, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except Exception:
            pass
    if removed:
        log.info(f"[pdf-cleanup] removed {removed} PDFs older than 30 days")


async def pdf_cleanup_loop():
    """Раз в сутки чистит старые PDF (с lock — не дублируется на multi-worker)."""
    from server.worker_lock import worker_lock
    await asyncio.sleep(600)  # подождать 10 мин после старта
    while True:
        try:
            with worker_lock("pdf_cleanup", ttl_sec=3600 * 23) as acquired:
                if acquired:
                    await _cleanup_old_pdfs_tick()
        except Exception as e:
            log.error(f"[pdf cleanup] error: {e}")
        await asyncio.sleep(86400)


# ── Auto-backup chat.db (раз в сутки, hot backup + retention 14 дней) ────────

async def _db_backup_tick():
    """Делает hot-backup chat.db через sqlite3.backup() — не блокирует writes.

    Сохраняет в /backups/chat.db.YYYY-MM-DD; старше 14 дней удаляет.
    Без этого деплой = git pull + restart, и при ошибке миграции откатиться
    некуда, кроме как руками вытаскивать журналы WAL.
    """
    import os, sqlite3, datetime, glob
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = os.path.join(base, "chat.db")
    if not os.path.exists(src):
        return
    backup_dir = os.path.join(base, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    today = datetime.date.today().isoformat()
    dst = os.path.join(backup_dir, f"chat.db.{today}")
    if os.path.exists(dst):
        return  # уже сделали сегодня (например рестарт сервера)
    try:
        # SQLite-native backup API — атомарно копирует, не блокируя writers
        # надолго (использует WAL + iterdump-friendly mode).
        src_conn = sqlite3.connect(src)
        dst_conn = sqlite3.connect(dst)
        try:
            with dst_conn:
                src_conn.backup(dst_conn)
        finally:
            src_conn.close()
            dst_conn.close()
        # Проверка целостности скопированного файла. Без этого corrupted backup
        # может остаться незамеченным до момента когда он понадобится для
        # восстановления — это худший вариант.
        check_conn = sqlite3.connect(dst)
        try:
            cur = check_conn.execute("PRAGMA integrity_check")
            row = cur.fetchone()
            integrity_ok = bool(row and row[0] == "ok")
        finally:
            check_conn.close()
        if not integrity_ok:
            log.error(f"[db-backup] integrity_check FAILED for {dst} — removing")
            try: os.remove(dst)
            except Exception: pass
            return
        size_mb = os.path.getsize(dst) / 1024 / 1024
        log.info(f"[db-backup] {dst} ({size_mb:.1f} MB) integrity=ok")
    except Exception as e:
        log.error(f"[db-backup] failed: {e}")
        # Удалим частичную копию чтобы не путать
        if os.path.exists(dst):
            try: os.remove(dst)
            except Exception: pass
        return

    # Retention: удаляем backup-ы старше 14 дней
    cutoff = (datetime.date.today() - datetime.timedelta(days=14)).isoformat()
    removed = 0
    for path in glob.glob(os.path.join(backup_dir, "chat.db.*")):
        try:
            tag = os.path.basename(path).replace("chat.db.", "")
            # tag = YYYY-MM-DD
            if len(tag) == 10 and tag < cutoff:
                os.remove(path)
                removed += 1
        except Exception:
            pass
    if removed:
        log.info(f"[db-backup] retention: removed {removed} backups older than 14 days")


async def _cleanup_old_action_logs_tick():
    """Удаляет аудит-логи в три эшелона retention:
      - обычные info: 30 дней
      - auth.* / payment.* / record.* info: 365 дней (нужны для forensic
        и юридических вопросов: «когда я зарегистрировался?», «когда был платёж?»)
      - error/warn/critical: 90 дней (нужны для разбора инцидентов)
    """
    from datetime import datetime, timedelta
    from server.db import db_session
    from server.models import ActionLog
    now = datetime.utcnow()
    cutoff_info_short = now - timedelta(days=30)
    cutoff_info_long = now - timedelta(days=365)
    cutoff_err = now - timedelta(days=90)
    try:
        with db_session() as db:
            # info обычные (не auth/payment) — 30 дней
            n_info = (db.query(ActionLog)
                      .filter(ActionLog.ts < cutoff_info_short,
                              ActionLog.level == "info")
                      .filter(~ActionLog.action.like("auth.%"))
                      .filter(~ActionLog.action.like("payment.%"))
                      .filter(~ActionLog.action.like("record.%"))
                      .delete(synchronize_session=False))
            # auth/payment/record info — 1 год
            n_long = (db.query(ActionLog)
                      .filter(ActionLog.ts < cutoff_info_long,
                              ActionLog.level == "info")
                      .filter(
                          ActionLog.action.like("auth.%") |
                          ActionLog.action.like("payment.%") |
                          ActionLog.action.like("record.%"))
                      .delete(synchronize_session=False))
            # warn/error/critical — 90 дней
            n_err = (db.query(ActionLog)
                     .filter(ActionLog.ts < cutoff_err,
                             ActionLog.level != "info")
                     .delete(synchronize_session=False))
            db.commit()
            if n_info or n_long or n_err:
                log.info(f"[audit-cleanup] removed info={n_info} long={n_long} non-info={n_err}")
    except Exception as e:
        log.error(f"[audit-cleanup] failed: {e}")


async def audit_cleanup_loop():
    from server.worker_lock import worker_lock
    await asyncio.sleep(1200)  # 20 мин после старта
    while True:
        try:
            with worker_lock("audit_cleanup", ttl_sec=3600 * 23) as acquired:
                if acquired:
                    await _cleanup_old_action_logs_tick()
        except Exception as e:
            log.error(f"[audit-cleanup] tick: {e}")
        await asyncio.sleep(86400)


async def _cleanup_old_conversations_tick():
    """Удаляет тёрны диалогов старше 30 дней — иначе таблица растёт без границ.
    Каждый бот в день может писать сотни сообщений × 100k клиентов = миллионы строк."""
    from datetime import datetime, timedelta
    from server.db import db_session
    from server.models import BotConversationTurn
    cutoff = datetime.utcnow() - timedelta(days=30)
    try:
        with db_session() as db:
            n = (db.query(BotConversationTurn)
                 .filter(BotConversationTurn.created_at < cutoff)
                 .delete(synchronize_session=False))
            db.commit()
            if n:
                log.info(f"[conv-cleanup] removed {n} turns older than 30d")
    except Exception as e:
        log.error(f"[conv-cleanup] failed: {e}")


async def conv_cleanup_loop():
    """Раз в сутки чистит старые тёрны диалогов чат-ботов."""
    from server.worker_lock import worker_lock
    await asyncio.sleep(900)  # 15 мин после старта
    while True:
        try:
            with worker_lock("conv_cleanup", ttl_sec=3600 * 23) as acquired:
                if acquired:
                    await _cleanup_old_conversations_tick()
        except Exception as e:
            log.error(f"[conv-cleanup] tick error: {e}")
        await asyncio.sleep(86400)


async def _storage_billing_tick():
    """
    Раз в сутки списывает плату за хранение файлов:
      - считаем total bytes у каждого юзера (только active assets)
      - округляем вверх до 100 МБ блоков
      - умножаем на цену storage.per_100mb_month / 30 (дневная ставка)
      - списываем атомарно

    Логика просроченных оплат:
      - При успешном списании ставим last_billed_at=now на ВСЕ active asset'ы юзера
      - Если баланса нет 7+ дней (last_billed_at < now-7d) → archive (is_active=False)
      - Если archived 30+ дней (last_billed_at < now-37d) → физическое удаление файла

    Юзер видит "архивирован" в UI и может пополнить + восстановить (если ещё не удалили).
    """
    from datetime import datetime, timedelta
    from server.db import db_session
    from server.models import StoredAsset, User, Transaction
    from server.billing import deduct_strict
    from server.pricing import get_price
    from sqlalchemy import func, update as sa_update

    rate_kop_month = get_price("storage.per_100mb_month", default=5000)
    daily_rate = max(1, rate_kop_month // 30)
    chunk = 100 * 1024 * 1024
    now = datetime.utcnow()
    try:
        with db_session() as db:
            users_with_storage = (
                db.query(StoredAsset.user_id, func.sum(StoredAsset.size_bytes).label("total"))
                .filter(StoredAsset.is_active == True)
                .group_by(StoredAsset.user_id)
                .all()
            )
            charged = skipped = 0
            for row in users_with_storage:
                user_id = row[0]
                total_bytes = int(row[1] or 0)
                if total_bytes <= 0:
                    continue
                units = (total_bytes + chunk - 1) // chunk
                cost = units * daily_rate
                if deduct_strict(db, user_id, cost):
                    db.add(Transaction(
                        user_id=user_id, type="usage", tokens_delta=-cost,
                        description=f"Хранилище: {round(total_bytes/1024/1024, 1)} МБ ({cost/100:.2f} ₽/день)",
                    ))
                    # Помечаем все активные asset'ы юзера как «оплачено сегодня».
                    db.execute(
                        sa_update(StoredAsset)
                        .where(StoredAsset.user_id == user_id,
                               StoredAsset.is_active == True)
                        .values(last_billed_at=now)
                    )
                    charged += 1
                else:
                    skipped += 1
            db.commit()
            if charged or skipped:
                log.info(f"[storage-billing] charged={charged} skipped(no balance)={skipped}")

            # ── Архивация просроченных (>7 дней без оплаты) ──────────────
            # Защита от race: новые файлы (created_at > cutoff) пропускаем,
            # даже если last_billed_at старый — последнее обновление billing
            # tick'а могло их пропустить, но грейс-период есть.
            cutoff_archive = now - timedelta(days=7)
            archived = (
                db.query(StoredAsset)
                .filter(StoredAsset.is_active == True)
                .filter(StoredAsset.last_billed_at != None)
                .filter(StoredAsset.last_billed_at < cutoff_archive)
                .filter(StoredAsset.created_at < cutoff_archive)
                .all()
            )
            archived_ids: list[int] = []
            for a in archived:
                a.is_active = False
                archived_ids.append(a.id)
            if archived_ids:
                db.commit()
                log.warning(f"[storage-billing] archived {len(archived_ids)} asset(s) — просрочка оплаты >7д")
                from server.audit_log import log_action
                # Группируем по user_id для отдельных audit-записей
                by_user: dict[int, list[int]] = {}
                for a in archived:
                    by_user.setdefault(a.user_id, []).append(a.id)
                for uid, ids in by_user.items():
                    log_action("asset.archived", user_id=uid, target_type="asset",
                               level="warn", success=False,
                               details={"reason": "no_balance_7d", "asset_ids": ids[:50]})

            # ── Физическое удаление (>37 дней с last_billed_at, is_active=False) ──
            cutoff_delete = now - timedelta(days=37)
            stale = (
                db.query(StoredAsset)
                .filter(StoredAsset.is_active == False)
                .filter(StoredAsset.last_billed_at != None)
                .filter(StoredAsset.last_billed_at < cutoff_delete)
                .all()
            )
            deleted = 0
            for a in stale:
                from pathlib import Path as _P
                try:
                    p = _P(a.path.lstrip("/"))
                    if p.exists():
                        p.unlink()
                except Exception as ex:
                    log.warning(f"[storage-billing] cannot delete file {a.path}: {ex}")
                # Удаляем запись из БД (можно оставить для истории, но тогда orphan)
                db.delete(a)
                deleted += 1
            if deleted:
                db.commit()
                log.warning(f"[storage-billing] hard-deleted {deleted} asset(s) — просрочка >37д")
    except Exception as e:
        log.error(f"[storage-billing] failed: {e}")


async def storage_billing_loop():
    """Раз в сутки списывает дневную плату за хранение файлов юзеров."""
    from server.worker_lock import worker_lock
    await asyncio.sleep(1800)  # 30 мин после старта (после миграций)
    while True:
        try:
            with worker_lock("storage_billing", ttl_sec=3600 * 23) as acquired:
                if acquired:
                    await _storage_billing_tick()
        except Exception as e:
            log.error(f"[storage-billing] tick error: {e}")
        await asyncio.sleep(86400)


async def db_backup_loop():
    """Раз в 24ч hot-backup БД (с advisory lock — не дублируется)."""
    from server.worker_lock import worker_lock
    await asyncio.sleep(120)  # подождать 2 мин после старта (миграции должны успеть)
    while True:
        try:
            with worker_lock("db_backup", ttl_sec=3600 * 23) as acquired:
                if acquired:
                    await _db_backup_tick()
        except Exception as e:
            log.error(f"[db-backup] tick error: {e}")
        await asyncio.sleep(86400)


def start_scheduler():
    """Фоновые задачи: scheduler / health / cleanup PDF / backup / conv / audit."""
    asyncio.create_task(scheduler_loop())
    asyncio.create_task(apikey_check_loop())
    asyncio.create_task(pdf_cleanup_loop())
    asyncio.create_task(db_backup_loop())
    asyncio.create_task(conv_cleanup_loop())
    asyncio.create_task(audit_cleanup_loop())
    asyncio.create_task(storage_billing_loop())
