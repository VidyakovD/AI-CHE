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
    """Главный цикл — проверка каждые 30 секунд."""
    log.info("Scheduler started")
    while True:
        try:
            await _scheduler_tick()
        except Exception as e:
            log.error(f"[Scheduler] tick error: {e}")
        await asyncio.sleep(30)


def start_scheduler():
    """Запустить scheduler в фоне."""
    asyncio.create_task(scheduler_loop())
