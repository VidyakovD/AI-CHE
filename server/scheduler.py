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
    """Одна проверка — бежим по всем активным ботам и их расписаниям."""
    db = SessionLocal()
    try:
        bots = db.query(ChatBot).filter_by(status="active").all()
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


def start_scheduler():
    """Запустить scheduler и health-check API-ключей в фоне."""
    asyncio.create_task(scheduler_loop())
    asyncio.create_task(apikey_check_loop())
