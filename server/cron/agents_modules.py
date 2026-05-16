"""Cron-runtime для активных модулей агента (раздел 23, schedule_cron).

Юзер настраивает в UI расписание модуля (например `0 9 * * *` — каждый
день в 9:00) + задачу-шаблон в `custom_settings.cron_task` (например
"Подготовь пост на тему стройки на сегодня"). Этот loop:

  1. Каждую минуту берёт все включённые AgentModule с заполненным
     schedule_cron и custom_settings.cron_task. Только агенты status='active'.
  2. Парсит cron, сравнивает с last_cron_fired_at + текущим временем.
  3. Если пора — запускает invoke_module(slug, cron_task, ...) под user_id.
  4. Списывает agents.module_invoke (если есть баланс — иначе skip + лог).
  5. Сохраняет результат как AgentMessage role='tool' c meta.mode='cron_invoke'.
  6. Обновляет last_cron_fired_at, прокачку уровня.

Worker-lock через server.worker_lock — на multi-worker сценарий запускается
ровно одним процессом.

Cron-парсер — минимальный (5 полей "M H D M W", *,N,N-N,N/N), без префиксов
@daily/@hourly. Этого достаточно для UI с готовыми пресетами и явных строк.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta

log = logging.getLogger("scheduler")


# ── Cron parser (5 полей: minute hour dom month dow) ─────────────────────────


def _match_field(val: int, spec: str) -> bool:
    spec = spec.strip()
    if not spec or spec == "*":
        return True
    for part in spec.split(","):
        part = part.strip()
        if "/" in part:
            base, step = part.split("/", 1)
            try:
                step_i = int(step)
            except ValueError:
                continue
            if base in ("", "*"):
                if val % step_i == 0:
                    return True
            elif "-" in base:
                try:
                    a, b = base.split("-", 1)
                    a_i, b_i = int(a), int(b)
                except ValueError:
                    continue
                if a_i <= val <= b_i and (val - a_i) % step_i == 0:
                    return True
            else:
                try:
                    base_i = int(base)
                except ValueError:
                    continue
                if val >= base_i and (val - base_i) % step_i == 0:
                    return True
        elif "-" in part:
            try:
                a, b = part.split("-", 1)
                if int(a) <= val <= int(b):
                    return True
            except ValueError:
                continue
        else:
            try:
                if val == int(part):
                    return True
            except ValueError:
                continue
    return False


def cron_should_fire(cron_expr: str, now: datetime,
                     last_fired: datetime | None) -> bool:
    """Должен ли cron сработать в этой минуте.

    Args:
      cron_expr: "M H D M W" — 5 полей через пробел
      now:        текущее UTC-время (с точностью до минуты)
      last_fired: когда стреляли в прошлый раз (для дедупликации в минуте)
    """
    if not cron_expr or not isinstance(cron_expr, str):
        return False
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        return False
    m, h, d, mo, w = parts
    # Не стреляем дважды в ту же минуту
    if last_fired is not None:
        if (now - last_fired).total_seconds() < 60:
            return False
    # dow: 0 = воскресенье, 1-6 = пн-сб (UNIX cron); isoweekday: 1=пн, 7=вс
    dow = now.isoweekday() % 7   # 0=вс, 1=пн, ..., 6=сб
    return (_match_field(now.minute, m)
            and _match_field(now.hour, h)
            and _match_field(now.day, d)
            and _match_field(now.month, mo)
            and _match_field(dow, w))


# ── Tick ─────────────────────────────────────────────────────────────────────


async def _agents_modules_cron_tick():
    """Бежим по активным модулям с schedule_cron, запускаем подошедшие."""
    from server.db import db_session
    from server.models import Agent, AgentModule, AgentMessage, Transaction, User
    from server.billing import deduct_atomic
    from server.pricing import get_price
    from server.agent_builder import (
        invoke_module, apply_module_memory_updates, compute_module_level,
    )

    now = datetime.utcnow()
    # Округлим до минуты — это шаг cron
    now_min = now.replace(second=0, microsecond=0)

    try:
        with db_session() as db:
            mods = (db.query(AgentModule)
                      .filter(AgentModule.is_enabled.is_(True))
                      .filter(AgentModule.schedule_cron.isnot(None))
                      .all())
            module_cost = get_price("agents.module_invoke", default=100)
            for m in mods:
                cron = (m.schedule_cron or "").strip()
                if not cron:
                    continue
                if not cron_should_fire(cron, now_min, m.last_cron_fired_at):
                    continue
                agent = db.query(Agent).filter_by(id=m.agent_id).first()
                if not agent or agent.status != "active":
                    continue
                user = db.query(User).filter_by(id=agent.user_id).first()
                if not user:
                    continue
                # cron_task — что именно делать. Без него запуск бесполезен.
                settings = {}
                try:
                    settings = json.loads(m.custom_settings_json or "{}")
                except Exception:
                    settings = {}
                cron_task = (settings.get("cron_task") or "").strip()
                if not cron_task:
                    log.info(f"[agents.cron] module={m.id} slug={m.slug}: "
                              "schedule_cron задан, но custom_settings.cron_task пуст — skip")
                    # Помечаем как сработавшее чтобы не спамить логи
                    m.last_cron_fired_at = now_min
                    db.commit()
                    continue
                # Pre-check баланса
                if module_cost > 0 and int(user.tokens_balance or 0) < module_cost:
                    log.info(f"[agents.cron] module={m.id} user={user.id}: "
                              f"low balance ({user.tokens_balance}/{module_cost}) — skip")
                    # Помечаем чтобы не пытаться каждую минуту в течение часа
                    m.last_cron_fired_at = now_min
                    db.commit()
                    continue
                # Готовим контекст
                try:
                    profile = json.loads(agent.profile_json or "{\"facts\":[]}")
                except Exception:
                    profile = {"facts": []}
                mod_memory = {}
                try:
                    mod_memory = json.loads(m.module_memory_json or "{}")
                except Exception:
                    mod_memory = {}
                # Запускаем модуль
                log.info(f"[agents.cron] firing module={m.id} slug={m.slug} "
                          f"user={user.id} cron={cron!r}")
                try:
                    inv = invoke_module(
                        slug=m.slug, task=cron_task,
                        profile=profile,
                        module_memory=mod_memory,
                        custom_settings=settings,
                        user_id=user.id,
                    )
                except Exception as e:
                    log.exception(f"[agents.cron] invoke failed: {e}")
                    inv = {"ok": False, "output": "", "error": str(e)[:200],
                           "model_used": "", "memory_updates": None}
                # Списываем + сохраняем сообщение + прокачка
                charged_kop = 0
                level_up = False
                if inv.get("ok"):
                    if module_cost > 0:
                        charged_kop = deduct_atomic(db, user.id, module_cost)
                        if charged_kop > 0:
                            db.add(Transaction(
                                user_id=user.id, type="usage",
                                tokens_delta=-charged_kop,
                                description=f"Модуль {m.slug} (cron): {charged_kop/100:.2f} ₽",
                                model=f"agents.module:{m.slug}",
                            ))
                    if inv.get("memory_updates"):
                        apply_module_memory_updates(mod_memory, inv["memory_updates"])
                        m.module_memory_json = json.dumps(mod_memory, ensure_ascii=False)
                    m.interaction_count = (m.interaction_count or 0) + 1
                    m.last_used_at = now
                    learned_count = len(mod_memory.get("learned") or [])
                    new_lvl = compute_module_level(
                        current_level=m.level or 0,
                        interaction_count=m.interaction_count,
                        agent_status=agent.status,
                        learned_count=learned_count,
                    )
                    level_up = new_lvl > (m.level or 0)
                    if level_up:
                        m.level = new_lvl
                    content = inv["output"]
                else:
                    content = f"⚠ Cron-вызов модуля {m.slug} не отработал: " \
                              f"{inv.get('error', 'неизвестная ошибка')}"
                # Сообщение в чат — юзер увидит его при следующем заходе
                db.add(AgentMessage(
                    agent_id=agent.id, role="tool", content=content,
                    meta_json=json.dumps({
                        "mode": "cron_invoke",
                        "slug": m.slug,
                        "model_used": inv.get("model_used", ""),
                        "level": m.level,
                        "level_up": level_up,
                        "interactions": m.interaction_count,
                        "ok": bool(inv.get("ok")),
                        "cost_kop": charged_kop,
                        "cron": cron,
                    }, ensure_ascii=False),
                ))
                # Помечаем что отработали
                m.last_cron_fired_at = now_min
                # Live agent activity — UI заметит свежий tick
                agent.last_activity_at = now
                db.commit()
                try:
                    from server.audit_log import log_action
                    log_action(
                        "agent.cron_invoke", user_id=user.id,
                        target_type="agent_module", target_id=m.id,
                        details={"slug": m.slug, "ok": bool(inv.get("ok")),
                                  "cost_kop": charged_kop, "level": m.level},
                    )
                except Exception:
                    pass
    except Exception as e:
        log.error(f"[agents.cron] tick error: {type(e).__name__}: {e}")


async def agents_modules_cron_loop():
    """Раз в минуту проверяем расписания модулей и запускаем подошедшие.
    Worker-lock защищает от двойного запуска на multi-worker.
    """
    from server.worker_lock import worker_lock
    log.info("Agents-modules cron loop started")
    # Старт через 90 сек (после миграций / seed / других loop'ов)
    await asyncio.sleep(90)
    while True:
        try:
            with worker_lock("agents_modules_cron", ttl_sec=55) as acquired:
                if acquired:
                    await _agents_modules_cron_tick()
        except Exception as e:
            log.error(f"[agents.cron] loop error: {e}")
        await asyncio.sleep(60)
