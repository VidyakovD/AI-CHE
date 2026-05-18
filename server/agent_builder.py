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
# DEPRECATED (2026-05-16): нижняя секция _build_system_prompt / apply_action /
# build_reply относится к старой архитектуре «много агентов на юзера»
# (status=draft → Builder диалог → activate). В singleton-варианте (раздел 23)
# routes/agents_modular.py использует ТОЛЬКО build_reply_personal и invoke_module
# ниже. Эти функции оставлены до полной миграции / тестов, чтобы не сломать
# импорты. Не вызываются из production-кода.


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
    """Достаём JSON из ответа модели даже если она добавила ```json``` или текст.

    Алгоритм:
      1. Снимаем ```json``` обёртку если есть.
      2. Пробуем json.loads сразу.
      3. Если не вышло — балансируем фигурные скобки с учётом строк/escape
         от первой { и берём первый сбалансированный объект.
    """
    if not raw:
        return None
    s = raw.strip()
    # Срезаем markdown-обёртку если есть
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    # Быстрый путь: модель вернула чистый JSON
    try:
        return json.loads(s)
    except Exception:
        pass
    # Балансируем скобки от первой `{`
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    end_idx = -1
    for i in range(start, len(s)):
        ch = s[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end_idx = i
                break
    if end_idx < 0:
        return None
    try:
        return json.loads(s[start:end_idx + 1])
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
        # LLM Router сам выбирает модель: agent_tools task → Claude (лидер MCP-Atlas).
        # Builder — диалог с tool-call'ами, типичная агентная задача.
        from server.llm_router import ask as router_ask
        route = router_ask(
            messages,
            task="agent_tools",
            complexity="medium",
            extra={"max_tokens": 2000, "temperature": 0.3, "_purpose": "agent_builder"},
        )
        raw = route.content
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


# ════════════════════════════════════════════════════════════════════════════
# Personal Agent — для архитектуры «один агент на юзера + модули»
# Архитектурная правка 2026-05-16: главный режим работы для модуля 23.
# ════════════════════════════════════════════════════════════════════════════


def _personal_system_prompt(*, agent_name: str, mode: str, profile: dict,
                            personality: dict, modules: list[dict]) -> str:
    """System prompt для personal-agent режима (onboarding | active)."""
    profile_summary = "пусто (ещё ничего не знаю про юзера)"
    facts = profile.get("facts") or []
    if facts or any(profile.get(k) for k in ("name", "industry", "tone", "goals")):
        lines = []
        for k in ("name", "industry", "tone", "goals"):
            if profile.get(k):
                lines.append(f"  {k}: {profile[k]}")
        for f in facts[:30]:
            if isinstance(f, dict) and f.get("value"):
                lines.append(f"  • {f.get('key', '')}: {f['value']}")
        profile_summary = "\n".join(lines) or "пусто"

    modules_summary = "ничего ещё не подключено"
    if modules:
        modules_summary = "\n".join(
            f"  • {m.get('slug')} (уровень L{m.get('level', 0)})" for m in modules
        )

    voice = personality.get("voice", "friendly")
    addon = personality.get("addon_prompt", "")
    catalog = _modules_catalog_md()

    if mode == "onboarding":
        mode_block = """
Ты сейчас в режиме ЗНАКОМСТВА. Твоя задача — узнать юзера: имя, сферу деятельности,
стиль общения, цели/боли. НЕ предлагай сразу подключать агентов — сначала узнай человека.
Задавай по 1-2 вопроса за раз, реагируй на его ответы тепло и по делу.

💡 Используй quick_replies КАЖДЫЙ РАЗ когда задаёшь вопрос с ограниченными вариантами:
   • «Какой стиль общения?» → ["Деловой", "Дружеский", "Лаконичный"]
   • «Чем занимаешься?» → ["Бизнес", "Фриланс", "Хобби", "Расскажу"] (последний — для свободного ввода)
   • «Готов работать?» → ["Да, поехали", "Ещё пара вопросов"]

🎨 ВАЖНО про твоё имя: ты по умолчанию называешься «Че», но юзер может тебя переименовать
и сменить иконку (он нажмёт на твоё имя в шапке — откроется панель настроек). Если уже узнал главное —
после set ready_for_active=true, ОБЯЗАТЕЛЬНО предложи: «Я по умолчанию Че, но ты можешь дать мне
своё имя и сменить иконку — нажми на имя сверху ✏». quick_replies: ["Назову позже", "Оставим Че"].

Если уже узнал главное (имя, сферу, стиль) — установи `ready_for_active: true` и
напиши что-то типа «Окей, я узнал главное. Теперь могу помогать и наращиваться агентами.»
"""
    else:
        mode_block = """
Ты в РАБОЧЕМ режиме. Юзер задаёт задачи или общается. Ты:
- Отвечаешь по сути, используя то что знаешь о юзере (профиль выше)
- Если задача попадает в спектр уже подключённого модуля — упоминаешь его в reply
- Если задача нужна НОВЫЙ модуль которого нет — предлагай подключить (suggest_modules)
- Если юзер сам просит что-то выучить / запомнить про него — добавляй в add_facts
- Если юзер хочет сменить стиль/тон/имя — обновляй personality
"""

    return f"""Ты — {agent_name}, личный ИИ-агент юзера на платформе AI Студия Че.
ТЫ ОДИН. Не «один из агентов», а единственный персональный ассистент этого юзера.
Юзер тебя знакомит с собой → ты обрастаешь модулями (подключаемыми навыками) →
каждый модуль учится под этого юзера.

{mode_block}

ПРОФИЛЬ ЮЗЕРА (Memory Hub — что ты уже знаешь):
{profile_summary}

ПОДКЛЮЧЁННЫЕ МОДУЛИ:
{modules_summary}

ДОСТУПНЫЕ МОДУЛИ ИЗ КАТАЛОГА (можешь предлагать подключать через suggest_modules):
{catalog}

СТИЛЬ ОБЩЕНИЯ: {voice}
{addon}

ФОРМАТ ОТВЕТА — СТРОГО JSON (никакого текста до или после):
```json
{{
  "reply": "Текст для юзера (markdown, основной вывод)",
  "profile_updates": {{
    // опц. — обновления топ-полей профиля юзера
    "name": "...", "industry": "...", "tone": "...", "goals": "..."
  }},
  "add_facts": [
    // опц. — новые факты которые надо запомнить про юзера
    {{"key": "...", "value": "..."}}
  ],
  "quick_replies": [
    // опц. — варианты ответа кнопками (UI покажет chips под сообщением).
    // ОБЯЗАТЕЛЬНО используй когда задаёшь вопрос с понятными вариантами
    // (стиль, выбор из 2-5 опций, да/нет). 2-5 коротких вариантов.
    "Деловой", "Дружеский", "Лаконичный"
  ],
  "suggest_modules": [
    // опц. — slug'и модулей которые стоит подключить (юзер увидит чипы)
    "smm", "lawyer"
  ],
  "invoke_module": {{
    // опц. — если задача КОНКРЕТНО требует выполнения работы подключённым
    // модулем, делегируй ему. Юзер увидит твой reply + отдельное сообщение
    // от модуля. Работает ТОЛЬКО для уже подключённых модулей (см. выше).
    "slug": "smm",
    "task": "Конкретная задача для модуля, развёрнуто"
  }},
  "ready_for_active": false  // true = онбординг считается законченным
}}
```

🎯 ПРАВИЛО ПРО quick_replies:
   - Если ты задаёшь вопрос с понятными ограниченными вариантами — ВСЕГДА
     добавляй quick_replies (2-5 коротких опций, ≤30 символов каждый).
   - Особенно в режиме onboarding: «Какой стиль общения?» → ["Деловой",
     "Дружеский", "Лаконичный"]. «Готов работать?» → ["Да, поехали",
     "Ещё пара уточнений"].
   - НЕ добавляй quick_replies если ответ требует свободного текста
     (имя, цель, длинное описание).

ПРАВИЛА:
1. Reply — обязательно. Остальные поля — опционально.
2. Не предлагай модулей которых НЕТ в каталоге выше.
3. invoke_module — только для подключённых (см. список выше). Для НЕподключённых — suggest_modules.
4. Не выдумывай факты — только то что юзер реально сказал.
5. Если юзер просит активировать / готов работать — ready_for_active=true.
6. Возвращай ТОЛЬКО валидный JSON. Никаких ```json``` обёрток.

🎯 КРИТИЧНО при invoke_module: ТЫ — БОСС. Не дублируй работу модуля!
   Если делегируешь — твой reply ОБЯЗАН быть короткий 1-2 предложения:
   • «Окей, поручаю это модулю X — сейчас он распишет.»
   • «Понял. Передал {{slug}} — ниже его план.»
   • «Принял задачу. Делаю через {{slug}}.»

   НЕ повторяй вопросы / план / содержание которое будет в ответе модуля.
   Модуль сам всё распишет в отдельном сообщении. Твоя роль — короткий ack."""


def build_reply_personal(*, agent_name: str, mode: str, profile: dict,
                         personality: dict, modules: list[dict],
                         history: list[dict], user_input: str,
                         user_id: int | None = None) -> dict:
    """Personal-agent шаг диалога (singleton-агент модуля 23).

    Args:
      agent_name: имя агента (для контекста)
      mode: onboarding | active
      profile: Memory Hub юзера (мутируется in-place при add_facts/profile_updates)
      personality: стиль агента (тон, voice, addon_prompt)
      modules: [{slug, level}] подключённые
      history: предыдущие сообщения (без нового user_input)
      user_input: новое сообщение юзера

    Returns: {
      "reply": str,
      "applied": list[str],
      "errors": list[str],
      "profile_changed": bool,
      "ready_for_active": bool,
      "raw": str,
    }
    """
    # ── PrivacyGuard: маскируем PII во ВСЕХ сообщениях которые улетят в LLM ──
    # Профиль может содержать имя/тел/email/ИНН — система-промпт это всё включает.
    # Один guard на весь вызов — токены консистентны, потом unmask reply.
    try:
        from server.privacy_guard import PrivacyGuard
        guard = PrivacyGuard()
    except Exception:
        guard = None  # privacy_guard модуль недоступен — деградируем gracefully

    def _mask(t: str) -> str:
        if not guard or not t:
            return t
        try:
            return guard.mask(t)
        except Exception:
            return t

    system = _personal_system_prompt(
        agent_name=agent_name, mode=mode, profile=profile,
        personality=personality, modules=modules,
    )
    # ВАЖНО: маскируем последовательно одним guard — иначе таблицы токенов не совпадут.
    if guard:
        # Сначала system (профиль), затем история, затем user_input
        system = guard.mask(system)
    masked_history = []
    for m in (history or [])[-20:]:
        if m.get("role") in ("user", "assistant"):
            masked_history.append({
                "role": m["role"],
                "content": _mask(m.get("content") or "")
            })
    masked_user_input = _mask(user_input)
    messages = [{"role": "system", "content": system}]
    messages.extend(masked_history)
    messages.append({"role": "user", "content": masked_user_input})

    try:
        from server.llm_router import ask as router_ask
        # Mode «agent_tools» — Claude (лидер MCP-Atlas) с structured JSON output
        extra_kw = {"max_tokens": 2000, "temperature": 0.4,
                    "_purpose": "personal_agent"}
        if user_id is not None:
            extra_kw["_user_id"] = int(user_id)
        route = router_ask(
            messages,
            task="agent_tools",
            complexity="medium",
            extra=extra_kw,
        )
        raw = route.content
        # Unmask токены обратно в оригинальные PII в reply модели
        if guard and raw:
            try:
                raw = guard.unmask_response(raw)
            except Exception:
                pass
    except Exception as e:
        log.exception(f"[personal-agent] LLM call failed: {e}")
        return {
            "reply": "Что-то у меня внутри сломалось 😔 Попробуй ещё раз через минуту.",
            "applied": [], "errors": [f"LLM error: {e!s:.120}"],
            "profile_changed": False, "ready_for_active": False, "raw": "",
        }

    parsed = _extract_json(raw)
    if not parsed:
        log.warning(f"[personal-agent] couldn't parse JSON, raw={raw[:200]!r}")
        return {
            "reply": raw.strip()[:2000] or "Не смог сформировать ответ — попробуй переформулировать.",
            "applied": [], "errors": ["JSON parse failed"],
            "profile_changed": False, "ready_for_active": False, "raw": raw,
        }

    reply = str(parsed.get("reply", "")).strip()[:4000]
    applied: list[str] = []
    errors: list[str] = []
    profile_changed = False

    # profile_updates — обновляем топ-поля
    updates = parsed.get("profile_updates") or {}
    if isinstance(updates, dict):
        for k in ("name", "industry", "tone", "goals"):
            if updates.get(k):
                val = str(updates[k]).strip()[:500]
                if val and val != profile.get(k):
                    profile[k] = val
                    applied.append(f"профиль: {k} = «{val[:60]}»")
                    profile_changed = True

    # add_facts — добавляем в profile.facts
    new_facts = parsed.get("add_facts") or []
    if isinstance(new_facts, list):
        facts = profile.setdefault("facts", [])
        for f in new_facts[:10]:
            if not isinstance(f, dict): continue
            k = str(f.get("key", "")).strip()[:80]
            v = str(f.get("value", "")).strip()[:500]
            if not k or not v: continue
            # Не дублируем (по ключу)
            if any(isinstance(x, dict) and x.get("key") == k for x in facts):
                continue
            facts.append({"key": k, "value": v,
                          "learned_at": datetime.utcnow().isoformat()})
            applied.append(f"запомнил: {k} = «{v[:60]}»")
            profile_changed = True

    suggest_modules = parsed.get("suggest_modules") or []
    if not isinstance(suggest_modules, list):
        suggest_modules = []

    # quick_replies — варианты ответа кнопками под сообщением (UX опросов)
    quick_replies = parsed.get("quick_replies") or []
    if not isinstance(quick_replies, list):
        quick_replies = []
    quick_replies = [str(q).strip()[:40] for q in quick_replies[:5] if str(q).strip()]

    # invoke_module — делегирование подключённому модулю
    invoke = parsed.get("invoke_module")
    invoke_request = None
    if isinstance(invoke, dict):
        slug = str(invoke.get("slug", "")).strip()
        task = str(invoke.get("task", "")).strip()[:8000]
        # Проверяем что модуль подключён
        if slug and task and any(m.get("slug") == slug for m in modules):
            invoke_request = {"slug": slug, "task": task}
            # Safety-net: при invoke_module reply должен быть короткий «босс-ack».
            # Если модель ответила длинно — обрезаем чтобы не было дубля
            # с ответом модуля (юзер просил 2026-05-16).
            if len(reply) > 280:
                log.warning(f"[personal-agent] reply слишком длинный при invoke_module ({len(reply)} симв) — обрезаю")
                # Берём первое предложение + многоточие
                first_sentence = re.split(r"(?<=[.!?])\s+", reply, maxsplit=1)[0]
                reply = first_sentence[:220].strip()
                if not reply.endswith((".", "!", "?", "…")):
                    reply += "…"

    ready = bool(parsed.get("ready_for_active"))

    return {
        "reply": reply or "Принял.",
        "applied": applied,
        "errors": errors,
        "profile_changed": profile_changed,
        "suggest_modules": [str(s)[:40] for s in suggest_modules[:5]],
        "quick_replies": quick_replies,
        "invoke_request": invoke_request,
        "ready_for_active": ready,
        "raw": raw,
    }


# datetime для timestamps в add_facts
from datetime import datetime  # noqa: E402


# ════════════════════════════════════════════════════════════════════════════
# Module Runtime — реальное выполнение задач модулями
# Личный агент решает "нужен модуль X" → invoke_module → возвращается результат
# ════════════════════════════════════════════════════════════════════════════


def invoke_module(*, slug: str, task: str, profile: dict,
                  module_memory: dict, custom_settings: dict,
                  user_id: int | None = None) -> dict:
    """Запустить модуль на задачу. Использует system_prompt из AGENT_REGISTRY +
    подмешивает Memory Hub юзера + персональную память модуля + настройки.

    Returns: {
      "ok": bool,
      "output": str,         # ответ модуля (markdown)
      "model_used": str,
      "error": str | None,
      "memory_updates": dict | None,  # что модуль выучил за этот вызов
    }
    """
    meta = AGENT_REGISTRY.get(slug)
    if not meta:
        return {"ok": False, "output": "",
                "error": f"Модуль {slug!r} не найден в каталоге",
                "model_used": "", "memory_updates": None}

    # Профиль юзера в кратком виде
    profile_lines = []
    for k in ("name", "industry", "tone", "goals"):
        if profile.get(k):
            profile_lines.append(f"  {k}: {profile[k]}")
    for f in (profile.get("facts") or [])[:15]:
        if isinstance(f, dict) and f.get("value"):
            profile_lines.append(f"  • {f.get('key','')}: {f['value']}")
    profile_text = "\n".join(profile_lines) or "  пусто"

    # Персональная память модуля (что выучено о юзере за прошлые вызовы)
    memory_lines = []
    if isinstance(module_memory, dict):
        for k in ("style", "preferences", "constraints"):
            if module_memory.get(k):
                memory_lines.append(f"  • {k}: {module_memory[k]}")
        learned = module_memory.get("learned") or []
        for L in learned[:10]:
            if isinstance(L, dict) and L.get("note"):
                memory_lines.append(f"  • {L['note']}")
    memory_text = "\n".join(memory_lines) or "  ничего ещё не выучено"

    # Кастомные настройки (channels/tokens/preferences)
    settings_text = ""
    if isinstance(custom_settings, dict) and custom_settings:
        try:
            settings_text = json.dumps(custom_settings, ensure_ascii=False, indent=2)[:500]
        except Exception:
            settings_text = ""

    base_prompt = meta.get("system_prompt") or (
        f"Ты — {meta.get('name', slug)}. {meta.get('description', '')}"
    )

    system = f"""{base_prompt}

═══ КОНТЕКСТ ЮЗЕРА (Memory Hub) ═══
{profile_text}

═══ ЧТО ТЫ ВЫУЧИЛ ПРО ЭТОГО ЮЗЕРА (твоя память) ═══
{memory_text}

═══ НАСТРОЙКИ МОДУЛЯ ═══
{settings_text or '  (нет)'}

═══ ВАЖНО ═══
- Отвечай ПО СУТИ задачи. Не объясняй что ты модуль — это знает оркестратор.
- Используй то что знаешь о юзере (его стиль, тон, отрасль).
- В конце ответа (в новой строке) можешь добавить:
  «[LEARNED: <что_заметил>]» — например «[LEARNED: юзер пишет в деловом тоне без эмодзи]»
  Это будет сохранено в твою память для следующих вызовов.
- Если задача требует данных извне (web, API) которых у тебя нет — напрямую скажи юзеру что нужно подключить (через owner-агента)."""

    # PrivacyGuard для модуля: профиль/память юзера в system + сама задача
    # могут содержать PII. Маскируем перед LLM, unmask reply.
    try:
        from server.privacy_guard import PrivacyGuard
        guard = PrivacyGuard()
        system = guard.mask(system)
        task_masked = guard.mask(task[:8000])
    except Exception:
        guard = None
        task_masked = task[:8000]
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": task_masked},
    ]

    try:
        from server.llm_router import ask as router_ask
        # Подбираем task-type под модуль — для контента/копирайтинга creative,
        # для legal/finance/analysis deep_analysis, остальные default
        task_type_map = {
            "smm": "creative_writing", "copywriter": "creative_writing",
            "scriptwriter": "creative_writing",
            "lawyer": "deep_analysis", "accountant": "deep_analysis",
            "analyst": "deep_analysis", "fin_analyst": "deep_analysis",
            "researcher": "research", "comp_intel": "research",
            "tender_parser": "research", "tender_analyst": "deep_analysis",
            "developer": "code", "seo": "creative_writing",
        }
        task_kind = task_type_map.get(slug, "default")
        extra_kw = {"max_tokens": 3000, "temperature": 0.5,
                    "_purpose": f"module:{slug}"}
        if user_id is not None:
            extra_kw["_user_id"] = int(user_id)
        route = router_ask(
            messages,
            task=task_kind,
            complexity="medium",
            extra=extra_kw,
        )
        output = route.content or ""
        if guard and output:
            try:
                output = guard.unmask_response(output)
            except Exception:
                pass
    except Exception as e:
        log.exception(f"[module:{slug}] LLM call failed: {e}")
        return {"ok": False, "output": "",
                "error": f"LLM error: {e!s:.140}",
                "model_used": "", "memory_updates": None}

    # Парсим [LEARNED: ...] маркеры (модуль выучил что-то про юзера)
    learned_notes: list[str] = []
    try:
        for match in re.finditer(r"\[LEARNED:\s*([^\]]+)\]", output):
            note = match.group(1).strip()[:300]
            if note:
                learned_notes.append(note)
        # Убираем маркеры из видимого output (показываем чистый ответ юзеру)
        output_clean = re.sub(r"\[LEARNED:\s*[^\]]+\]", "", output).strip()
    except Exception:
        output_clean = output

    memory_updates = None
    if learned_notes:
        memory_updates = {"new_notes": learned_notes}

    return {
        "ok": True,
        "output": output_clean,
        "model_used": route.model_used if hasattr(route, "model_used") else "",
        "error": None,
        "memory_updates": memory_updates,
    }


def apply_module_memory_updates(memory: dict, updates: dict) -> dict:
    """Применить memory_updates к module_memory (мутирует dict).
    Возвращает количество добавленных записей."""
    if not updates or not isinstance(memory, dict):
        return memory
    new_notes = updates.get("new_notes") or []
    if not new_notes:
        return memory
    learned = memory.setdefault("learned", [])
    for note in new_notes[:5]:  # лимит на ход
        learned.append({"note": str(note)[:300],
                        "ts": datetime.utcnow().isoformat()})
    # Держим топ-50 заметок (старые отсекаем)
    if len(learned) > 50:
        memory["learned"] = learned[-50:]
    return memory


# ════════════════════════════════════════════════════════════════════════════
# Прокачка модулей L0 → L1 → L2 → L3 → L4
# Из ТЗ раздел 5.2:
#   L0 → L1: 5-10 взаимодействий + заполненный onboarding
#   L1 → L2: ≥30 взаимодействий + подключён источник данных
#   L2 → L3: 4+ недели регулярного использования + накопленная база
#   L3 → L4: явное разрешение юзера (НЕ авто-апгрейд)
# ════════════════════════════════════════════════════════════════════════════


def increment_module_interaction(db, module) -> int:
    """Атомарный +1 для interaction_count модуля — безопасно при multi-worker.

    Заменяет RMW pattern `m.interaction_count = (m.interaction_count or 0) + 1`,
    который при 4 воркерах терял инкременты (read 29 + read 29 → write 30 + write 30).

    Использовать перед compute_module_level() — возвращает актуальное значение,
    которое уже видно остальным воркерам.

    Pending changes (memory_json, last_used_at и т.д.) — flush'аются ДО UPDATE,
    чтобы не потеряться при refresh().
    """
    from server.models import AgentModule
    # 1) Сначала зафлэшить уже сделанные изменения этого объекта в БД
    db.flush()
    # 2) Атомарный SQL UPDATE: SET interaction_count = interaction_count + 1
    db.query(AgentModule).filter(AgentModule.id == module.id).update(
        {AgentModule.interaction_count: AgentModule.interaction_count + 1},
        synchronize_session="fetch",
    )
    # 3) Re-read объекта — берёт актуальный counter из БД (с учётом параллельных +1)
    db.refresh(module)
    return int(module.interaction_count or 0)


def compute_module_level(*, current_level: int, interaction_count: int,
                         agent_status: str, learned_count: int) -> int:
    """Вычислить новый уровень модуля. НИКОГДА не понижает (только up).
    L4 — только через явный API (юзер сам разрешил автономию)."""
    new_level = current_level

    # L0 → L1: 5+ взаимодействий И юзер прошёл онбординг
    if new_level < 1 and interaction_count >= 5 and agent_status == "active":
        new_level = 1
    # L1 → L2: 30+ взаимодействий И модуль что-то выучил (learned >= 3)
    if new_level < 2 and interaction_count >= 30 and learned_count >= 3:
        new_level = 2
    # L2 → L3: 200+ взаимодействий И накопленная база (learned >= 20)
    if new_level < 3 and interaction_count >= 200 and learned_count >= 20:
        new_level = 3
    # L3 → L4 — только через явный API, авто не повышаем
    return new_level
