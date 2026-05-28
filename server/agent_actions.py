"""Парсер action-блоков из LLM-вывода + создание PendingAgentAction.

═══ ПРОТОКОЛ ═══

Чтобы модуль предложил реальное действие (отправить почту, создать встречу
в Google Calendar, поставить кампанию на паузу), LLM должна в своём
ответе выдать блок:

    [ACTION:tool_name]
    key1: value1
    key2: value2
    body:
    многострочное
    тело
    [/ACTION]

После body: и до [/ACTION] — всё считается одним длинным текстовым полем
`body`. До body: — пары key: value (без вложенности, простой YAML-lite).

═══ ЧТО ДЕЛАЕТ process_action_blocks ═══

1. Сканирует output на [ACTION:name]...[/ACTION] блоки
2. Для каждого парсит поля → params dict
3. Создаёт PendingAgentAction(status="pending") в БД
4. Возвращает кортеж (clean_output, [pending_action_dicts])
   - clean_output — без [ACTION] блоков, с подменой на пометки
     «✉ Готов отправить (action #ID)»
   - pending_action_dicts — для отображения в UI чате

═══ ПОЧЕМУ НЕ JSON ═══

LLM плохо генерирует многострочный body внутри JSON (теряет \n,
ломает escape). YAML-lite формат с явным body: маркером — более устойчив.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

log = logging.getLogger(__name__)


# Поддерживаемые типы action'ов и их preview-форматтеры
# (preview_text — что юзер увидит в UI до подтверждения).
_PREVIEW_FORMATTERS = {}


def register_action_preview(action_type: str):
    """Декоратор для регистрации preview-форматтера action'а."""
    def _wrap(fn):
        _PREVIEW_FORMATTERS[action_type] = fn
        return fn
    return _wrap


@register_action_preview("send_email")
def _preview_send_email(params: dict) -> str:
    to = params.get("to", "?")
    subject = params.get("subject", "(без темы)")
    body_preview = (params.get("body") or "")[:120]
    if len(params.get("body") or "") > 120:
        body_preview += "..."
    return f"✉ Отправить письмо\nКому: {to}\nТема: {subject}\n\n{body_preview}"


@register_action_preview("create_google_event")
def _preview_create_google_event(params: dict) -> str:
    title = params.get("title", "(без названия)")
    start = params.get("start", "?")
    end = params.get("end") or ""
    when = f"{start} — {end}" if end else start
    where = params.get("location") or ""
    line2 = f"📍 {where}" if where else ""
    return f"📅 Создать событие в Google Calendar\n{title}\nКогда: {when}\n{line2}".strip()


@register_action_preview("yandex_direct_pause_campaign")
def _preview_yandex_direct_pause(params: dict) -> str:
    cid = params.get("campaign_id", "?")
    name = params.get("campaign_name") or ""
    return f"⏸ Поставить на паузу Я.Директ кампанию {cid}\n{name}"


@register_action_preview("yandex_direct_resume_campaign")
def _preview_yandex_direct_resume(params: dict) -> str:
    cid = params.get("campaign_id", "?")
    name = params.get("campaign_name") or ""
    return f"▶ Возобновить Я.Директ кампанию {cid}\n{name}"


@register_action_preview("yandex_direct_set_daily_budget")
def _preview_yandex_direct_set_budget(params: dict) -> str:
    cid = params.get("campaign_id", "?")
    new = params.get("new_daily_budget_rub", "?")
    return f"💰 Изменить дневной бюджет Я.Директ {cid} → {new} ₽/день"


@register_action_preview("vk_ads_pause_campaign")
def _preview_vk_ads_pause(params: dict) -> str:
    cid = params.get("campaign_id", "?")
    return f"⏸ Поставить на паузу VK Ads кампанию {cid}"


@register_action_preview("vk_ads_set_day_limit")
def _preview_vk_ads_set_day_limit(params: dict) -> str:
    cid = params.get("campaign_id", "?")
    new = params.get("day_limit_rub", "?")
    return f"💰 VK Ads дневной лимит кампании {cid} → {new} ₽"


@register_action_preview("add_finance_transaction")
def _preview_add_finance_transaction(params: dict) -> str:
    amount = params.get("amount_kop", 0)
    try:
        amount = int(amount)
    except Exception:
        amount = 0
    sign = "+" if amount > 0 else "−"
    desc = params.get("description") or "(без описания)"
    cat = params.get("category") or "other"
    date = params.get("date") or "сегодня"
    return (f"💰 Записать транзакцию\n"
            f"{sign}{abs(amount) / 100:.2f} ₽ ({cat})\n"
            f"{desc}\n{date}")


@register_action_preview("log_workout")
def _preview_log_workout(params: dict) -> str:
    exercise = params.get("exercise") or "?"
    sets = params.get("sets") or ""
    return f"🏋 Зафиксировать упражнение\n{exercise}\nПодходы: {sets}"


@register_action_preview("log_meal")
def _preview_log_meal(params: dict) -> str:
    meal_type = params.get("meal_type") or "?"
    desc = params.get("description") or "?"
    cal = params.get("calories")
    cal_str = f"\n≈ {cal} ккал" if cal else ""
    return f"🥗 Записать приём пищи\n{meal_type}: {desc}{cal_str}"


@register_action_preview("publish_to_creators")
def _preview_publish_to_creators(params: dict) -> str:
    brand = params.get("brand_name") or f"бренд #{params.get('brand_id', '?')}"
    platform = params.get("platform", "tg")
    when = params.get("schedule_at") or "сразу"
    body_preview = (params.get("body") or "")[:120]
    return (f"📢 Опубликовать пост → {brand} ({platform})\n"
            f"Когда: {when}\n\n{body_preview}")


def _format_preview(action_type: str, params: dict) -> str:
    fn = _PREVIEW_FORMATTERS.get(action_type)
    if fn:
        try:
            return fn(params)
        except Exception:
            pass
    # Fallback: первые 200 символов JSON params
    return f"⚙ Действие {action_type}: " + json.dumps(params, ensure_ascii=False)[:200]


# ── Парсер блоков ────────────────────────────────────────────────────────────

# [ACTION:tool_name]\n...\n[/ACTION]
_ACTION_BLOCK_RE = re.compile(
    r'\[ACTION:([a-z_][a-z0-9_]*)\]\s*\n(.*?)\n\s*\[/ACTION\]',
    re.IGNORECASE | re.DOTALL,
)


def _parse_action_body(body: str) -> dict:
    """Парсит тело action-блока в dict.

    Формат: ключ: значение, по одному на строку. Спец-ключ `body:` —
    всё после него и до конца блока становится одним текстовым полем.
    Если ключ повторяется — заменяется (последнее значение побеждает).
    """
    out: dict = {}
    lines = body.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        # Пропускаем пустые строки между парами
        if not stripped:
            i += 1
            continue
        # Маркер начала body-блока: всё после него и до конца — body
        m = re.match(r'^body\s*:\s*(.*)$', line, re.IGNORECASE)
        if m:
            rest = [m.group(1)] + lines[i + 1:]
            out["body"] = "\n".join(rest).strip()
            break
        # Обычная пара ключ: значение
        m = re.match(r'^([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*(.*)$', line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            out[key] = val
        i += 1
    return out


def _coerce_types(params: dict) -> dict:
    """Привести строковые значения к нужным типам (int для ID, etc).

    Простая эвристика: если ключ заканчивается на _id или это известный
    числовой ключ — int. Если выглядит как число с дробной точкой — float.
    """
    int_keys = {"mailbox_id", "campaign_id", "brand_id", "calendar_connection_id"}
    out = {}
    for k, v in params.items():
        if isinstance(v, str) and (k in int_keys or k.endswith("_id")):
            try:
                out[k] = int(v.strip())
                continue
            except Exception:
                pass
        if isinstance(v, str) and k in ("new_daily_budget_rub", "amount", "amount_rub"):
            try:
                out[k] = float(v.strip())
                continue
            except Exception:
                pass
        out[k] = v
    return out


def parse_action_blocks(output: str) -> list[dict]:
    """Извлечь все action-блоки из output без сайд-эффектов.

    Возвращает list[{action_type, params}].
    """
    found: list[dict] = []
    for m in _ACTION_BLOCK_RE.finditer(output or ""):
        action_type = m.group(1).strip().lower()
        body = m.group(2)
        params = _coerce_types(_parse_action_body(body))
        found.append({"action_type": action_type, "params": params})
    return found


def strip_action_blocks(output: str, replace_with_marker: bool = True) -> str:
    """Вырезать [ACTION:...]...[/ACTION] из output.

    Если replace_with_marker=True — оставит inline-метку «⏳ см. ниже»,
    чтобы юзер понимал что в этом месте было предложение.
    """
    def _sub(m: re.Match) -> str:
        if replace_with_marker:
            return f"\n_(предложено действие: {m.group(1)} — см. ниже)_\n"
        return ""
    return re.sub(_ACTION_BLOCK_RE, _sub, output or "").strip()


# ── Создание PendingAgentAction в БД ─────────────────────────────────────────

def create_pending_actions(
    *, user_id: int, agent_id: Optional[int], module_slug: str,
    output: str, db=None,
) -> tuple[str, list[dict]]:
    """Распарсить output, создать PendingAgentAction в БД, вернуть
    (clean_output, list of pending action dicts для UI).

    Если db не передана — создаём свою через db_session().
    """
    parsed = parse_action_blocks(output)
    if not parsed:
        return output, []

    from server.db import db_session
    from server.models import PendingAgentAction

    pending_dicts: list[dict] = []
    ctx = db_session() if db is None else None
    try:
        session = ctx.__enter__() if ctx else db
        try:
            for p in parsed:
                action_type = p["action_type"]
                params = p["params"]
                preview = _format_preview(action_type, params)
                row = PendingAgentAction(
                    user_id=user_id,
                    agent_id=agent_id,
                    module_slug=module_slug,
                    action_type=action_type,
                    params_json=json.dumps(params, ensure_ascii=False),
                    preview_text=preview[:2000],
                    status="pending",
                )
                session.add(row)
                session.flush()  # чтобы получить id
                pending_dicts.append({
                    "id": row.id,
                    "action_type": action_type,
                    "preview_text": preview,
                    "params": params,
                    "status": "pending",
                })
            if ctx is not None:
                session.commit()
        except Exception:
            if ctx is not None:
                session.rollback()
            raise
    finally:
        if ctx is not None:
            ctx.__exit__(None, None, None)

    clean = strip_action_blocks(output)
    return clean, pending_dicts


# ── Выполнение подтверждённого action'а ──────────────────────────────────────

# Реестр исполнителей: action_type → callable(params, user_id) -> dict
_EXECUTORS: dict = {}


def register_executor(action_type: str):
    def _wrap(fn):
        _EXECUTORS[action_type] = fn
        return fn
    return _wrap


def _load_module_settings(user_id: int, slug: str) -> dict:
    """Подгрузить custom_settings подключённого модуля юзера. Пустой dict если нет."""
    import json as _json
    from server.db import db_session
    from server.models import Agent, AgentModule
    with db_session() as db:
        m = (db.query(AgentModule)
               .join(Agent, Agent.id == AgentModule.agent_id)
               .filter(Agent.user_id == user_id,
                       AgentModule.slug == slug,
                       AgentModule.is_enabled.is_(True))
               .first())
        if not m or not m.custom_settings_json:
            return {}
        try:
            data = _json.loads(m.custom_settings_json)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


@register_executor("publish_to_creators")
def _execute_publish_to_creators(params: dict, user_id: int) -> dict:
    """Опубликовать пост через Креаторы — создаёт ContentItem + (опц.) сразу публикует.

    params:
      brand_id        — обязателен
      platform        — tg|vk|yt|ig
      type            — text|image|reels|youtube|poll  (default: text)
      body            — готовый текст (попадает в prepared_content_md)
      schedule_at     — ISO когда публиковать (default — сейчас → сразу publish)
      title           — опц.
    """
    from datetime import datetime as _dt
    from server.db import db_session
    from server.models import (CreatorBrand, ContentCalendar, ContentItem,
                                 CreatorChannelConnection)

    brand_id = params.get("brand_id")
    platform = (params.get("platform") or "tg").lower()
    type_ = (params.get("type") or "text").lower()
    body = (params.get("body") or "").strip()
    schedule_at_raw = params.get("schedule_at")
    title = (params.get("title") or "").strip() or None

    if not isinstance(brand_id, int) or brand_id <= 0:
        return {"ok": False, "error": "brand_id обязателен"}
    if platform not in {"tg", "vk", "yt", "ig"}:
        return {"ok": False, "error": f"Неподдерживаемая платформа: {platform}"}
    if not body:
        return {"ok": False, "error": "body — обязателен (готовый текст поста)"}

    # Парсим schedule_at, по умолчанию = сейчас (UTC)
    now = _dt.utcnow()
    if schedule_at_raw:
        try:
            sa = _dt.fromisoformat(str(schedule_at_raw).replace("Z", "+00:00"))
            if sa.tzinfo is not None:
                sa = sa.astimezone().replace(tzinfo=None)
            schedule_at = sa
        except Exception:
            schedule_at = now
    else:
        schedule_at = now

    publish_immediately = schedule_at <= now

    with db_session() as db:
        brand = db.query(CreatorBrand).filter(
            CreatorBrand.id == brand_id,
            CreatorBrand.user_id == user_id,
        ).first()
        if not brand:
            return {"ok": False, "error": f"Бренд #{brand_id} не найден"}

        # Берём/создаём календарь для бренда (схема: один бренд = один календарь)
        cal = (db.query(ContentCalendar)
                 .filter(ContentCalendar.brand_id == brand_id)
                 .first())
        if not cal:
            # Минимальный календарь — period начинается сейчас и +30 дней.
            # Создан через copywriter-bridge, не через UI.
            cal_start = _dt.utcnow()
            cal_end = cal_start.replace(hour=23, minute=59, second=59)
            from datetime import timedelta as _td
            cal_end = cal_start + _td(days=30)
            cal = ContentCalendar(brand_id=brand_id, status="active",
                                   period_start=cal_start, period_end=cal_end)
            db.add(cal); db.flush()

        item = ContentItem(
            calendar_id=cal.id,
            schedule_at=schedule_at,
            platform=platform,
            type=type_,
            brief=title,
            prepared_content_md=body,
            status="ready",  # уже готов — текст внутри
            cost_kop=0,       # это не подготовка через DALL-E, без списания
            manual_override=True,
        )
        db.add(item); db.commit(); db.refresh(item)
        item_id = item.id
        brand_name = brand.name

    # Если время = сейчас + поддерживаем auto-publish (tg/vk) — публикуем
    if publish_immediately and platform in {"tg", "vk"}:
        from server.creators_publisher import publish_item as _publish
        import asyncio as _asyncio
        try:
            loop = _asyncio.new_event_loop()
            try:
                # _publish ждёт db-сессию + объект item; перезагрузим в свежей сессии
                with db_session() as db:
                    fresh = db.query(ContentItem).get(item_id)
                    if fresh:
                        pub_result = loop.run_until_complete(_publish(db, fresh))
                    else:
                        pub_result = {"ok": False, "description": "Item lost"}
            finally:
                loop.close()
        except Exception as e:
            return {"ok": True,  # ContentItem всё же создан
                    "result": {"item_id": item_id, "brand": brand_name,
                               "platform": platform,
                               "scheduled_at": schedule_at.isoformat(),
                               "publish_error": str(e)[:200]},
                    "error": None}

        if pub_result.get("ok"):
            return {"ok": True,
                    "result": {"item_id": item_id, "brand": brand_name,
                               "platform": platform, "published": True,
                               "external_post_id": pub_result.get("external_post_id")},
                    "error": None}
        return {"ok": True,  # пост создан, но не опубликован — пусть будет в календаре
                "result": {"item_id": item_id, "brand": brand_name,
                           "platform": platform, "published": False,
                           "publish_error": pub_result.get("description")},
                "error": None}

    # Иначе — просто создан ContentItem, опубликуется по cron'у в schedule_at
    return {"ok": True,
            "result": {"item_id": item_id, "brand": brand_name,
                       "platform": platform,
                       "scheduled_at": schedule_at.isoformat(),
                       "published": False},
            "error": None}


@register_executor("yandex_direct_pause_campaign")
def _execute_yandex_direct_pause(params: dict, user_id: int) -> dict:
    """Поставить кампанию Я.Директ на паузу.

    Токен берётся из custom_settings подключённого direct_ads модуля
    (поле `oauth_token` или `access_token`).
    """
    from server.yandex_direct import pause_campaign

    cid = params.get("campaign_id")
    if not isinstance(cid, int) or cid <= 0:
        return {"ok": False, "error": "campaign_id обязателен"}

    settings = _load_module_settings(user_id, "direct_ads")
    token = (settings.get("oauth_token") or settings.get("access_token") or "").strip()
    if not token:
        return {"ok": False,
                "error": "OAuth-токен Я.Директа не настроен. "
                          "Добавь его в карточке модуля «Директолог»."}

    sandbox = bool(settings.get("sandbox"))
    result = pause_campaign(token, cid, sandbox=sandbox)
    if result["ok"]:
        return {"ok": True, "result": {"campaign_id": cid, "action": "paused"},
                "error": None}
    return {"ok": False, "error": result.get("error") or "Direct API ошибка"}


@register_executor("yandex_direct_set_daily_budget")
def _execute_yandex_direct_set_budget(params: dict, user_id: int) -> dict:
    """Изменить дневной бюджет кампании Я.Директ."""
    from server.yandex_direct import set_daily_budget

    cid = params.get("campaign_id")
    new_budget = params.get("new_daily_budget_rub") or params.get("daily_budget_rub")
    if not isinstance(cid, int) or cid <= 0:
        return {"ok": False, "error": "campaign_id обязателен"}
    try:
        new_budget = float(new_budget)
    except Exception:
        return {"ok": False, "error": "new_daily_budget_rub должен быть числом"}
    if new_budget <= 0:
        return {"ok": False, "error": "Бюджет должен быть > 0"}
    # Защита от опечатки LLM: огромный бюджет (>1 млн ₽/день) — подозрительно
    if new_budget > 1_000_000:
        return {"ok": False,
                "error": f"Бюджет {new_budget} ₽/день кажется чрезмерным — проверь и подтверди явно"}

    settings = _load_module_settings(user_id, "direct_ads")
    token = (settings.get("oauth_token") or settings.get("access_token") or "").strip()
    if not token:
        return {"ok": False,
                "error": "OAuth-токен Я.Директа не настроен"}
    sandbox = bool(settings.get("sandbox"))
    result = set_daily_budget(token, cid, new_budget, sandbox=sandbox)
    if result["ok"]:
        return {"ok": True,
                "result": {"campaign_id": cid, "new_budget_rub": new_budget},
                "error": None}
    return {"ok": False, "error": result.get("error")}


@register_executor("yandex_direct_resume_campaign")
def _execute_yandex_direct_resume(params: dict, user_id: int) -> dict:
    """Возобновить Я.Директ кампанию (снять с паузы)."""
    from server.yandex_direct import resume_campaign

    cid = params.get("campaign_id")
    if not isinstance(cid, int) or cid <= 0:
        return {"ok": False, "error": "campaign_id обязателен"}

    settings = _load_module_settings(user_id, "direct_ads")
    token = (settings.get("oauth_token") or settings.get("access_token") or "").strip()
    if not token:
        return {"ok": False,
                "error": "OAuth-токен Я.Директа не настроен в карточке модуля «Директолог»."}
    sandbox = bool(settings.get("sandbox"))
    result = resume_campaign(token, cid, sandbox=sandbox)
    if result["ok"]:
        return {"ok": True,
                "result": {"campaign_id": cid, "action": "resumed"},
                "error": None}
    return {"ok": False, "error": result.get("error")}


@register_executor("vk_ads_set_day_limit")
def _execute_vk_ads_set_day_limit(params: dict, user_id: int) -> dict:
    """Изменить дневной лимит VK Ads кампании (рубли)."""
    from server.vk_ads import set_campaign_day_limit

    cid = params.get("campaign_id")
    account_id = params.get("account_id")
    day_limit = params.get("day_limit_rub") or params.get("daily_budget_rub")
    if not isinstance(cid, int) or cid <= 0:
        return {"ok": False, "error": "campaign_id обязателен"}
    try:
        day_limit = float(day_limit)
    except Exception:
        return {"ok": False, "error": "day_limit_rub должен быть числом"}
    if day_limit < 0:
        return {"ok": False, "error": "Лимит должен быть ≥ 0"}
    # Защита от опечатки LLM: VK Ads >500k₽/день — почти наверняка ошибка
    if day_limit > 500_000:
        return {"ok": False,
                "error": f"Лимит {day_limit} ₽/день кажется чрезмерным — уточни и подтверди явно"}

    settings = _load_module_settings(user_id, "vk_ads")
    token = (settings.get("ads_token") or "").strip()
    if not token:
        return {"ok": False, "error": "VK Ads токен не настроен"}
    account_id = account_id or settings.get("account_id")
    if not account_id:
        return {"ok": False, "error": "account_id обязателен (или сохрани в настройках)"}

    result = set_campaign_day_limit(token, account_id, cid, int(day_limit))
    if result["ok"]:
        return {"ok": True,
                "result": {"campaign_id": cid, "account_id": account_id,
                           "day_limit_rub": day_limit},
                "error": None}
    return {"ok": False, "error": result.get("error")}


@register_executor("vk_ads_pause_campaign")
def _execute_vk_ads_pause(params: dict, user_id: int) -> dict:
    """Поставить VK Ads кампанию на паузу. Токен из custom_settings.ads_token."""
    from server.vk_ads import update_campaign_status

    cid = params.get("campaign_id")
    account_id = params.get("account_id")
    if not isinstance(cid, int) or cid <= 0:
        return {"ok": False, "error": "campaign_id обязателен"}

    settings = _load_module_settings(user_id, "vk_ads")
    token = (settings.get("ads_token") or "").strip()
    if not token:
        return {"ok": False,
                "error": "VK Ads токен не настроен. Получи user-токен с scope=ads "
                          "на vkhost.github.io и добавь в карточке модуля."}
    account_id = account_id or settings.get("account_id")
    if not account_id:
        return {"ok": False, "error": "account_id обязателен (или в настройках модуля)"}

    result = update_campaign_status(token, account_id, cid, status=0)
    if result["ok"]:
        return {"ok": True,
                "result": {"campaign_id": cid, "account_id": account_id,
                           "action": "paused"},
                "error": None}
    return {"ok": False, "error": result.get("error")}


@register_executor("add_finance_transaction")
def _execute_add_finance_transaction(params: dict, user_id: int) -> dict:
    """Записать ручную транзакцию в FinanceTransaction.

    params:
      amount_kop  — положительная для дохода, отрицательная для расхода (копейки)
      description — текст
      category    — slug из server.finance_csv.CATEGORIES (food/transport/...)
      date        — ISO-8601 (опц.), по умолчанию сейчас
    """
    from datetime import datetime as _dt
    from server.db import db_session
    from server.models import FinanceTransaction
    from server.finance_csv import CATEGORIES

    try:
        amount_kop = int(params.get("amount_kop") or 0)
    except Exception:
        return {"ok": False, "error": "amount_kop должно быть целым числом"}
    if amount_kop == 0:
        return {"ok": False, "error": "amount_kop=0 — нечего записывать"}
    # Тот же лимит, что и в существующем tool_add_finance_transaction
    _CAP = 2_000_000_000  # ~20 млн ₽
    if abs(amount_kop) > _CAP:
        return {"ok": False, "error": f"amount_kop={amount_kop} превышает лимит (±20 млн ₽)"}

    try:
        date_raw = params.get("date") or _dt.utcnow().isoformat()
        date = _dt.fromisoformat(str(date_raw).replace("Z", "+00:00"))
        if date.tzinfo is not None:
            date = date.astimezone().replace(tzinfo=None)
    except Exception:
        date = _dt.utcnow()

    category = params.get("category") or "other"
    if category not in CATEGORIES:
        category = "other"

    with db_session() as db:
        tx = FinanceTransaction(
            user_id=user_id,
            source="manual",
            date=date,
            amount_kop=amount_kop,
            currency=params.get("currency") or "RUB",
            description=(params.get("description") or "")[:500] or None,
            category=category,
        )
        db.add(tx); db.commit(); db.refresh(tx)
        tx_id = tx.id

    sign = "+" if amount_kop > 0 else "−"
    return {
        "ok": True,
        "result": {
            "transaction_id": tx_id,
            "amount_rub": amount_kop / 100,
            "category": CATEGORIES.get(category, category),
            "description": params.get("description"),
        },
        "error": None,
    }


@register_executor("log_workout")
def _execute_log_workout(params: dict, user_id: int) -> dict:
    """Зафиксировать одно упражнение в WorkoutLog.

    params:
      exercise — название («жим лёжа», «присед»)
      sets     — список сетов в формате «вес×повторы», через запятую:
                 «80×8, 80×8, 85×6» → JSON [{weight:80,reps:8}, ...]
      date     — ISO, по умолчанию сегодня
      notes    — опционально
    """
    import json as _json
    from datetime import datetime as _dt
    from server.db import db_session
    from server.models import WorkoutLog

    exercise = (params.get("exercise") or "").strip()
    if not exercise:
        return {"ok": False, "error": "Не указано упражнение (exercise)"}

    sets_raw = (params.get("sets") or "").strip()
    if not sets_raw:
        return {"ok": False, "error": "Не указаны подходы (sets)"}

    # Парсим «80×8, 80×8×3» → [{weight, reps}, ...]
    sets_list: list[dict] = []
    for part in re.split(r"[,;]", sets_raw):
        part = part.strip()
        if not part:
            continue
        # Формат: weight × reps [× sets_count]
        m = re.match(r"^(\d+(?:\.\d+)?)\s*[x×]\s*(\d+)(?:\s*[x×]\s*(\d+))?",
                     part, re.IGNORECASE)
        if not m:
            continue
        w = float(m.group(1)); reps = int(m.group(2))
        count = int(m.group(3) or 1)
        for _ in range(count):
            sets_list.append({"weight": w, "reps": reps})

    if not sets_list:
        return {"ok": False, "error": f"Не удалось распарсить подходы из: {sets_raw!r}"}

    try:
        date_raw = params.get("date") or _dt.utcnow().isoformat()
        wd = _dt.fromisoformat(str(date_raw).replace("Z", "+00:00"))
        if wd.tzinfo is not None:
            wd = wd.astimezone().replace(tzinfo=None)
    except Exception:
        wd = _dt.utcnow()

    with db_session() as db:
        row = WorkoutLog(
            user_id=user_id,
            workout_date=wd,
            exercise=exercise[:120],
            sets_json=_json.dumps(sets_list, ensure_ascii=False),
            notes=(params.get("notes") or None),
        )
        db.add(row); db.commit(); db.refresh(row)
        row_id = row.id

    total_volume = sum(s["weight"] * s["reps"] for s in sets_list)
    return {
        "ok": True,
        "result": {
            "id": row_id, "exercise": exercise,
            "sets_count": len(sets_list),
            "total_volume_kg": round(total_volume, 1),
        },
        "error": None,
    }


@register_executor("log_meal")
def _execute_log_meal(params: dict, user_id: int) -> dict:
    """Зафиксировать приём пищи в MealLog.

    params:
      meal_type   — breakfast|lunch|dinner|snack
      description — что съел
      calories    — int (опц.)
      protein_g/fat_g/carbs_g (опц.)
      date        — ISO, по умолчанию сейчас
    """
    from datetime import datetime as _dt
    from server.db import db_session
    from server.models import MealLog

    meal_type = (params.get("meal_type") or "").strip().lower()
    if meal_type not in {"breakfast", "lunch", "dinner", "snack"}:
        return {"ok": False,
                "error": "meal_type должен быть breakfast/lunch/dinner/snack"}
    description = (params.get("description") or "").strip()
    if not description:
        return {"ok": False, "error": "Не указано описание (description)"}

    def _int_or_none(k):
        v = params.get(k)
        if v in (None, "", "null"):
            return None
        try:
            return int(v)
        except Exception:
            try:
                return int(float(v))
            except Exception:
                return None

    try:
        date_raw = params.get("date") or _dt.utcnow().isoformat()
        md = _dt.fromisoformat(str(date_raw).replace("Z", "+00:00"))
        if md.tzinfo is not None:
            md = md.astimezone().replace(tzinfo=None)
    except Exception:
        md = _dt.utcnow()

    with db_session() as db:
        row = MealLog(
            user_id=user_id,
            meal_date=md,
            meal_type=meal_type,
            description=description[:500],
            calories=_int_or_none("calories"),
            protein_g=_int_or_none("protein_g"),
            fat_g=_int_or_none("fat_g"),
            carbs_g=_int_or_none("carbs_g"),
            notes=(params.get("notes") or None),
        )
        db.add(row); db.commit(); db.refresh(row)
        row_id = row.id
        cal = row.calories

    return {
        "ok": True,
        "result": {"id": row_id, "meal_type": meal_type,
                   "description": description, "calories": cal},
        "error": None,
    }


@register_executor("create_google_event")
def _execute_create_google_event(params: dict, user_id: int) -> dict:
    """Создать событие в Google Calendar пользователя.

    Ожидаемые params: title, start (ISO-8601 с TZ или date), опционально end,
    location, description, calendar_connection_id (если у юзера несколько).
    """
    import asyncio as _asyncio
    from server.db import db_session
    from server.models import UserCalendarConnection
    from server.calendar_sync import (
        create_google_event, google_refresh_access_token,
    )

    title = (params.get("title") or params.get("summary") or "").strip()
    start = (params.get("start") or "").strip()
    end = (params.get("end") or "").strip() or None
    location = (params.get("location") or "").strip() or None
    description = (params.get("description") or "").strip() or None
    conn_id = params.get("calendar_connection_id")

    if not title:
        return {"ok": False, "error": "Не указано название (title)"}
    if not start:
        return {"ok": False, "error": "Не указано время начала (start)"}

    # Найти подключение (если conn_id не указан — берём первое активное google)
    with db_session() as db:
        q = db.query(UserCalendarConnection).filter(
            UserCalendarConnection.user_id == user_id,
            UserCalendarConnection.provider == "google",
            UserCalendarConnection.is_active.is_(True),
        )
        if isinstance(conn_id, int) and conn_id > 0:
            q = q.filter(UserCalendarConnection.id == conn_id)
        conn = q.first()
        if not conn:
            return {"ok": False,
                    "error": "Не найдено активное Google Calendar подключение. "
                             "Подключи в карточке модуля."}
        # Снимаем чувствительные поля внутри сессии
        refresh_token = conn.refresh_token
        access_token = conn.access_token
        calendar_id = conn.calendar_id or "primary"
        conn_pk = conn.id

    # Получаем свежий access_token (TTL у Google ≈ 1 час)
    async def _run() -> dict:
        # Если есть refresh — обновляем access (он мог истечь)
        if refresh_token:
            ref = await google_refresh_access_token(refresh_token)
            if not ref or not ref.get("access_token"):
                return {"ok": False, "error": "Не удалось обновить Google access_token — переподключи Google Calendar"}
            at = ref["access_token"]
        else:
            at = access_token
        if not at:
            return {"ok": False, "error": "Нет access_token и refresh_token — переподключи"}
        try:
            result = await create_google_event(
                access_token=at, calendar_id=calendar_id,
                summary=title, start=start, end=end,
                description=description, location=location,
            )
        except PermissionError:
            return {"ok": False, "error": "Google access_token истёк — попробуй ещё раз"}
        return result

    try:
        # invoke_module запускается из sync-контекста, делаем свой loop
        loop = _asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(_run())
        finally:
            loop.close()
    except Exception as e:
        log.exception("[create_google_event] failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e!s:.140}"}

    # Сохраним обновлённый access_token если получили новый (не критично, но
    # экономит refresh при следующих cron-вызовах)
    if result.get("ok"):
        return {
            "ok": True,
            "result": {
                "event_id": result.get("event_id"),
                "html_link": result.get("html_link"),
                "calendar_connection_id": conn_pk,
                "title": title,
                "start": start,
                "end": end,
            },
            "error": None,
        }
    return {"ok": False, "error": result.get("error") or "Google API ошибка"}


@register_executor("send_email")
def _execute_send_email(params: dict, user_id: int) -> dict:
    """Реально отправить письмо через SMTP подключённого ящика.

    Ожидаемые params: mailbox_id, to, subject, body. Опционально: reply_to.
    """
    from server.db import db_session
    from server.models import UserMailbox
    from server.mail_send import send_via_smtp, derive_smtp, is_valid_email

    mailbox_id = params.get("mailbox_id")
    to = (params.get("to") or "").strip()
    subject = (params.get("subject") or "").strip()
    body = params.get("body") or ""
    reply_to = (params.get("reply_to") or "").strip() or None

    if not isinstance(mailbox_id, int) or mailbox_id <= 0:
        return {"ok": False, "error": "Не указан mailbox_id"}
    if not is_valid_email(to):
        return {"ok": False, "error": f"Невалидный адрес получателя: {to!r}"}

    with db_session() as db:
        mb = db.query(UserMailbox).filter(
            UserMailbox.id == mailbox_id,
            UserMailbox.user_id == user_id,
            UserMailbox.is_active.is_(True),
        ).first()
        if not mb:
            return {"ok": False, "error": f"Ящик #{mailbox_id} не найден или не активен"}
        smtp_host, smtp_port = derive_smtp(mb.host, mb.smtp_host, mb.smtp_port)
        smtp_user = mb.email
        smtp_password = mb.password   # EncryptedString автоматически расшифрует
        from_name = mb.label or None

    result = send_via_smtp(
        smtp_host=smtp_host, smtp_port=smtp_port,
        smtp_user=smtp_user, smtp_password=smtp_password,
        from_addr=smtp_user, from_name=from_name,
        to=to, subject=subject, body=body,
        reply_to=reply_to,
    )
    return {
        "ok": result["ok"],
        "result": {"message_id": result.get("message_id"),
                   "from": smtp_user, "to": to, "subject": subject,
                   "smtp_host": smtp_host, "smtp_port": smtp_port},
        "error": result.get("error"),
    }


def execute_action(action_type: str, params: dict, user_id: int) -> dict:
    """Запустить ранее подтверждённый action.

    Возвращает {"ok": bool, "result": ..., "error": str|None}.
    Сам не пишет в БД — caller обновляет PendingAgentAction.status.
    """
    fn = _EXECUTORS.get(action_type)
    if not fn:
        return {"ok": False, "result": None,
                "error": f"Нет исполнителя для action {action_type!r}"}
    try:
        result = fn(params, user_id)
        if isinstance(result, dict) and "ok" in result:
            return result
        return {"ok": True, "result": result, "error": None}
    except Exception as e:
        log.exception("[agent_actions] executor %s failed", action_type)
        return {"ok": False, "result": None,
                "error": f"{type(e).__name__}: {e!s:.200}"}


# ── Системный prompt для LLM с описанием протокола ──────────────────────────

def get_action_protocol_prompt(allowed_actions: list[str]) -> str:
    """Кусок system_prompt'а который добавляется к модулям с действиями.

    Объясняет LLM как оформить предложение действия. Юзер должен ОБЯЗАТЕЛЬНО
    подтвердить — модуль НЕ выполняет автоматически.

    allowed_actions — список action_type, доступных этому модулю.
    """
    if not allowed_actions:
        return ""

    examples = {
        "send_email": (
            "[ACTION:send_email]\n"
            "mailbox_id: 5\n"
            "to: ivan@example.com\n"
            "subject: Re: ваш запрос\n"
            "body:\n"
            "Здравствуйте, Иван!\n\n"
            "По вашему вопросу — да, готовы помочь.\n"
            "[/ACTION]"
        ),
        "create_google_event": (
            "[ACTION:create_google_event]\n"
            "calendar_connection_id: 3\n"
            "title: Встреча с Иваном — обсуждение проекта\n"
            "start: 2026-06-05T14:00:00+03:00\n"
            "end: 2026-06-05T15:00:00+03:00\n"
            "location: Zoom\n"
            "description: Обсудим планы на квартал\n"
            "[/ACTION]"
        ),
        "add_finance_transaction": (
            "[ACTION:add_finance_transaction]\n"
            "amount_kop: -20000      # 200 ₽ расход (отрицательное число)\n"
            "category: cafe\n"
            "description: кофе в Старбакс\n"
            "date: 2026-05-28T15:30:00+03:00\n"
            "[/ACTION]"
        ),
        "log_workout": (
            "[ACTION:log_workout]\n"
            "exercise: жим лёжа\n"
            "sets: 80×8, 80×8, 85×6\n"
            "date: 2026-05-28T19:00:00+03:00\n"
            "notes: всё чисто, без срывов\n"
            "[/ACTION]"
        ),
        "log_meal": (
            "[ACTION:log_meal]\n"
            "meal_type: breakfast\n"
            "description: овсянка с бананом + кофе\n"
            "calories: 320\n"
            "protein_g: 10\n"
            "fat_g: 5\n"
            "carbs_g: 55\n"
            "[/ACTION]"
        ),
        "yandex_direct_pause_campaign": (
            "[ACTION:yandex_direct_pause_campaign]\n"
            "campaign_id: 12345678\n"
            "campaign_name: Аукционные товары\n"
            "[/ACTION]"
        ),
        "yandex_direct_resume_campaign": (
            "[ACTION:yandex_direct_resume_campaign]\n"
            "campaign_id: 12345678\n"
            "campaign_name: Аукционные товары\n"
            "[/ACTION]"
        ),
        "vk_ads_set_day_limit": (
            "[ACTION:vk_ads_set_day_limit]\n"
            "campaign_id: 4242424\n"
            "account_id: 12345     # опц. (или из настроек модуля)\n"
            "day_limit_rub: 1500\n"
            "[/ACTION]"
        ),
        "publish_to_creators": (
            "[ACTION:publish_to_creators]\n"
            "brand_id: 7\n"
            "platform: tg\n"
            "type: text\n"
            "schedule_at: 2026-06-10T10:00:00Z\n"
            "body:\n"
            "Главное за неделю — выход новой версии...\n"
            "[/ACTION]"
        ),
    }

    blocks = ["\n═══ ПРОТОКОЛ ДЕЙСТВИЙ (важно) ═══",
              "Когда нужно реально что-то сделать (отправить, создать, изменить),",
              "оформи это блоком [ACTION:тип] ... [/ACTION]. Юзер увидит preview",
              "и подтвердит кнопкой. Без подтверждения действие НЕ выполняется.",
              "",
              "Доступные тебе действия:"]
    for at in allowed_actions:
        ex = examples.get(at)
        if ex:
            blocks.append(f"\n  {at}:\n" + "\n".join("    " + l for l in ex.splitlines()))
        else:
            blocks.append(f"\n  • {at}")
    blocks.append("")
    blocks.append("Правила:")
    blocks.append("- Один блок = одно действие. МОЖНО 2-3 блока в одном ответе —")
    blocks.append("  каждый станет отдельной карточкой подтверждения у юзера.")
    blocks.append("  Например: ответил на письмо ([ACTION:send_email]) + поставил")
    blocks.append("  встречу в календарь ([ACTION:create_google_event]) в одном ответе.")
    blocks.append("- Поля key: value по одному на строку. body: — последнее (многострочное).")
    blocks.append("- Не пиши блок если юзер ещё уточняет — сначала собери все детали.")
    blocks.append("- ID полей (mailbox_id, calendar_connection_id, brand_id и т.п.) бери")
    blocks.append("  из «КОНТЕКСТА ЮЗЕРА» — там перечислены подключённые ресурсы с их id.")
    blocks.append("- Не дублируй очевидное в основном тексте — preview карточки сам всё покажет.")
    return "\n".join(blocks)
