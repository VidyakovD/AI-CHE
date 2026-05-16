"""Agent Builder — диалоговый конструктор агентов.

Спецагент для разговора «о создании другого агента». Не путать с самим
агентом (Agent) или его runtime — это отдельный мини-LLM-цикл который
читает историю чата с агентом + текущий spec, отвечает юзеру и выполняет
действия (add_module / set_schedule / set_trigger / save / activate).

Подход — prompt-based tool calling через JSON-response (без нативного
function-calling провайдеров — работает с любым). Модель отвечает строго:

  {
    "reply": "Текст ответа юзеру (показывается в чате)",
    "actions": [
      {"type":"add_module","slug":"smm","reason":"для постов в соцсети"},
      {"type":"set_schedule","cron":"0 9 * * *"},
      ...
    ],
    "ready_to_activate": false
  }

Парсим actions, применяем к spec.json, сохраняем в БД, возвращаем reply.

Used by: server/routes/agents_modular.py POST /api/agents/{id}/messages
для агентов со status=draft.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from server.ai import generate_response
from server.agent_runner import AGENT_REGISTRY

log = logging.getLogger(__name__)


# ── Список доступных модулей для Builder ─────────────────────────────────────

# Топ-модули которые предлагать в первую очередь — гибкая подсказка для LLM.
# Не ограничение: модель может выбрать любой из AGENT_REGISTRY.
TOP_MODULES = [
    "smm", "copywriter", "scriptwriter",          # контент
    "marketer", "seo", "email_marketer",          # маркетинг
    "lawyer", "accountant", "hr_docs",            # документы
    "analyst", "fin_analyst", "researcher",       # аналитика
    "bot_tg", "bot_site",                          # автоматизация
    "tender_parser", "tender_analyst",            # тендеры
]

# Маппинг каналов на человеческие названия (для подсказки LLM)
CHANNEL_DESCRIPTIONS = {
    "web": "веб-чат на aiche.ru (по умолчанию)",
    "tg": "Telegram-бот (нужен токен бота)",
    "vk": "VK-сообщения и постинг (нужен токен)",
    "avito": "Avito Messenger (нужны креды)",
    "max": "MAX (max.ru) бот",
    "whatsapp": "WhatsApp через Wazzup24",
    "email": "email уведомления",
}

TRIGGER_DESCRIPTIONS = {
    "new_email": "новое письмо в почте (нужен Gmail OAuth)",
    "tg_mention": "упоминание бота в Telegram",
    "vk_comment": "новый комментарий в VK-группе",
    "crm_lead": "новый лид в CRM",
    "webhook": "входящий webhook (custom URL)",
    "manual": "запуск по кнопке",
}


def _modules_catalog_md() -> str:
    """Markdown-каталог модулей для подсказки LLM в system_prompt.
    Сначала топ-модули (с порядком), потом остальные алфавитно."""
    if not AGENT_REGISTRY:
        return "_каталог модулей пуст — Registry не загружен_"
    seen = set()
    lines = []
    for slug in TOP_MODULES:
        m = AGENT_REGISTRY.get(slug)
        if m and slug not in seen:
            seen.add(slug)
            lines.append(f"- **{slug}** — {m['name']}: {m['description'][:120]}")
    other = sorted(s for s in AGENT_REGISTRY.keys() if s not in seen)
    for slug in other[:25]:  # ограничим чтобы не раздувать промпт
        m = AGENT_REGISTRY[slug]
        lines.append(f"- **{slug}** — {m['name']}: {m['description'][:100]}")
    if len(other) > 25:
        lines.append(f"- _… и ещё {len(other) - 25} модулей_")
    return "\n".join(lines)


# ── System prompt для Builder ────────────────────────────────────────────────

def _build_system_prompt(spec: dict, agent_name: str, control_mode: str) -> str:
    modules_md = _modules_catalog_md()
    channels_md = "\n".join(f"- `{k}` — {v}" for k, v in CHANNEL_DESCRIPTIONS.items())
    triggers_md = "\n".join(f"- `{k}` — {v}" for k, v in TRIGGER_DESCRIPTIONS.items())
    current = json.dumps(spec, ensure_ascii=False, indent=2)
    return f"""Ты — Agent Builder, дружелюбный конструктор ИИ-агентов на платформе AI Студия Че.
Твоя задача — помочь юзеру через короткий диалог собрать спецификацию его агента: подобрать модули из каталога, настроить расписание/триггеры/цель, и в конце активировать.

ИМЯ АГЕНТА: {agent_name}
СПОСОБ УПРАВЛЕНИЯ (выбрал юзер): {control_mode}

ТЕКУЩАЯ СПЕЦИФИКАЦИЯ (что уже накоплено):
```json
{current}
```

ДОСТУПНЫЕ МОДУЛИ (выбирай ровно из этого списка по slug):
{modules_md}

ДОСТУПНЫЕ КАНАЛЫ:
{channels_md}

ДОСТУПНЫЕ ТИПЫ ТРИГГЕРОВ:
{triggers_md}

ПРАВИЛА ОБЩЕНИЯ:
1. Говори ПРОСТО, без жаргона. Юзер — предприниматель, не разработчик.
2. Задавай по 1-2 вопроса за раз, не сразу 10. Веди диалог постепенно.
3. Когда у тебя достаточно информации — предложи активировать (action `activate_agent`).
4. Не делай предположений. Если не уверен — спроси.
5. Если юзер уже всё сказал в первом сообщении — собери spec и сразу предлагай активировать.
6. Цена работы агента зависит от модулей — упомяни если она существенная (>50 ₽/день).
7. Расписание задавай в cron-формате ("0 9 * * *" = каждый день в 9:00). Объясняй cron словами в reply.

ФОРМАТ ОТВЕТА — СТРОГО JSON (никакого текста до или после):
```json
{{
  "reply": "Текст для юзера (показывается в чате, поддерживает markdown)",
  "actions": [
    // 0 или больше действий из списка ниже
  ],
  "ready_to_activate": false  // true = предлагаешь активировать
}}
```

ДОСТУПНЫЕ ДЕЙСТВИЯ (в массиве actions):
- `{{"type":"add_module","slug":"<slug>","reason":"<почему>"}}` — добавить модуль
- `{{"type":"remove_module","slug":"<slug>"}}` — убрать модуль
- `{{"type":"set_schedule","cron":"<cron>"}}` — установить расписание (null чтобы убрать)
- `{{"type":"add_trigger","trigger":{{"type":"<type>","filter":"<optional>"}}}}` — добавить триггер
- `{{"type":"clear_triggers"}}` — очистить все триггеры
- `{{"type":"set_goals","text":"<новая цель>"}}` — обновить цель агента
- `{{"type":"set_channels","channels":["web","tg"]}}` — задать каналы
- `{{"type":"set_system_prompt_addon","text":"<правила>"}}` — добавить пользовательские правила
- `{{"type":"activate_agent"}}` — активировать агента (status=active). Делай только когда юзер подтвердил готовность.

ВАЖНО: возвращай ТОЛЬКО валидный JSON. Никаких ```json``` обёрток, никакого текста до/после."""


# ── Применение действий к spec ───────────────────────────────────────────────

VALID_TRIGGER_TYPES = set(TRIGGER_DESCRIPTIONS.keys())
VALID_CHANNELS = set(CHANNEL_DESCRIPTIONS.keys())

# Допустимые cron-выражения — простая sanity-проверка (5 полей или @daily etc.)
_CRON_RE = re.compile(r"^(@(?:yearly|monthly|weekly|daily|hourly|reboot)|(\S+\s+){4}\S+)$")


def apply_action(spec: dict, action: dict) -> tuple[bool, str]:
    """Применить одно action к spec. Возвращает (success, message).
    spec мутируется на месте."""
    if not isinstance(action, dict):
        return False, "action не является объектом"
    atype = action.get("type")
    if not atype:
        return False, "у action нет поля type"

    if atype == "add_module":
        slug = (action.get("slug") or "").strip()
        if not slug or slug not in AGENT_REGISTRY:
            return False, f"неизвестный модуль: {slug!r}"
        mods = spec.setdefault("modules", [])
        if slug not in mods:
            mods.append(slug)
        return True, f"добавлен модуль {slug}"

    if atype == "remove_module":
        slug = (action.get("slug") or "").strip()
        mods = spec.setdefault("modules", [])
        if slug in mods:
            mods.remove(slug)
        return True, f"убран модуль {slug}"

    if atype == "set_schedule":
        cron = action.get("cron")
        if cron is None or cron == "":
            spec["schedule"] = None
            return True, "расписание очищено"
        cron = str(cron).strip()
        if not _CRON_RE.match(cron):
            return False, f"невалидный cron: {cron!r}"
        spec["schedule"] = cron
        return True, f"расписание: {cron}"

    if atype == "add_trigger":
        tr = action.get("trigger")
        if not isinstance(tr, dict) or tr.get("type") not in VALID_TRIGGER_TYPES:
            return False, f"невалидный триггер: {tr!r}"
        trigs = spec.setdefault("triggers", [])
        trigs.append({k: v for k, v in tr.items() if k in ("type", "filter", "params")})
        return True, f"добавлен триггер {tr.get('type')}"

    if atype == "clear_triggers":
        spec["triggers"] = []
        return True, "триггеры очищены"

    if atype == "set_goals":
        text = (action.get("text") or "").strip()[:2000]
        spec["goals"] = text
        return True, "цель обновлена"

    if atype == "set_channels":
        chans = action.get("channels") or []
        if not isinstance(chans, list):
            return False, "channels должно быть массивом"
        chans = [c for c in chans if c in VALID_CHANNELS]
        if not chans:
            chans = ["web"]
        spec["channels"] = chans
        return True, f"каналы: {', '.join(chans)}"

    if atype == "set_system_prompt_addon":
        text = (action.get("text") or "").strip()[:4000]
        spec["system_prompt_addon"] = text
        return True, "правила обновлены"

    if atype == "activate_agent":
        # Сам Agent.status выставляется в caller (route), здесь только маркер
        return True, "ACTIVATE"

    return False, f"неизвестный action.type: {atype}"


# ── Парсинг JSON-ответа модели (с recovery от обёрток) ──────────────────────

def _extract_json(raw: str) -> dict | None:
    """Достаём JSON из ответа модели даже если она добавила ```json``` или текст."""
    if not raw:
        return None
    s = raw.strip()
    # Срезаем markdown-обёртку если есть
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    # Берём содержимое от первой { до последней }
    start = s.find("{")
    end = s.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(s[start:end+1])
    except Exception as e:
        log.warning(f"[builder] JSON parse failed: {e!s:.120}")
        return None


# ── Main entry point ─────────────────────────────────────────────────────────

def build_reply(*, agent_name: str, control_mode: str, spec: dict,
                history: list[dict], user_input: str) -> dict:
    """Сделать один шаг диалога Builder.

    Args:
      agent_name: имя агента (для контекста)
      control_mode: chat|schedule|triggers|hybrid
      spec: текущий spec (мутируется на месте при применении actions)
      history: предыдущие сообщения [{role, content}], БЕЗ нового user_input
      user_input: новое сообщение юзера

    Returns: {
      "reply": str,              # текст для юзера
      "applied": list[str],      # описания применённых действий (для лога)
      "errors": list[str],       # ошибки применения
      "ready_to_activate": bool, # модель просит активацию
      "raw": str,                # сырой ответ модели (для debug)
    }
    """
    system = _build_system_prompt(spec, agent_name, control_mode)
    messages = [{"role": "system", "content": system}]
    # Берём последние 20 сообщений истории (хватит для контекста, не раздувая)
    for m in (history or [])[-20:]:
        r = m.get("role")
        if r in ("user", "assistant"):
            messages.append({"role": r, "content": m.get("content") or ""})
    messages.append({"role": "user", "content": user_input})

    try:
        result = generate_response(
            "claude-sonnet",
            messages,
            extra={"max_tokens": 2000, "temperature": 0.3, "_purpose": "agent_builder"},
        )
        raw = result.get("content", "") if isinstance(result, dict) else str(result)
    except Exception as e:
        log.exception(f"[builder] LLM call failed: {e}")
        return {
            "reply": "Что-то у меня внутри сломалось при генерации ответа 😔 "
                     "Попробуй ещё раз через минуту, или открой настройки.",
            "applied": [], "errors": [f"LLM error: {e!s:.120}"],
            "ready_to_activate": False, "raw": "",
        }

    parsed = _extract_json(raw)
    if not parsed:
        log.warning(f"[builder] couldn't parse JSON, raw={raw[:200]!r}")
        return {
            "reply": raw.strip()[:2000] or "Не смог сформировать ответ — попробуй переформулировать.",
            "applied": [], "errors": ["JSON parse failed — модель не вернула структурированный ответ"],
            "ready_to_activate": False, "raw": raw,
        }

    reply = str(parsed.get("reply", "")).strip()[:4000]
    actions = parsed.get("actions") or []
    if not isinstance(actions, list):
        actions = []
    ready = bool(parsed.get("ready_to_activate"))

    applied: list[str] = []
    errors: list[str] = []
    for act in actions[:20]:  # лимит на всякий случай
        ok, msg = apply_action(spec, act)
        if ok:
            applied.append(msg)
            if msg == "ACTIVATE":
                ready = True
        else:
            errors.append(msg)
            log.warning(f"[builder] action failed: {msg} action={act}")

    return {
        "reply": reply or "Принял, продолжаю.",
        "applied": applied,
        "errors": errors,
        "ready_to_activate": ready,
        "raw": raw,
    }
