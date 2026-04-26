"""
AI-помощник: по тексту задачи собирает граф воркфлоу для chatbot/agent constructor.

Используется в endpoint /chatbots/ai-build-workflow и /agents/ai-build-workflow.
Возвращает структуру { wfc_nodes: [...], wfc_edges: [...], name, explanation }
совместимую с конструктором в views/agents.html.
"""
import json
import logging
import re
from server.ai import generate_response

log = logging.getLogger("workflow_builder")


# Краткое описание всех нод для LLM (key → описание + важные поля).
# Обновляется вместе с WFC_DEFS в agents.html — должны совпадать ID.
NODE_CATALOG = """
ТРИГГЕРЫ (точка входа, ВСЕГДА один):
- trigger_tg          — входящее в Telegram. Поля: tg_token, tg_chat_id (опц).
- trigger_vk          — входящее в VK. Поля: vk_token, vk_group_id.
- trigger_avito       — входящее в Авито Messenger.
- trigger_max         — входящее в MAX (https://max.ru). Поля: max_token (Bot Token из @MasterBot).
- trigger_webhook     — внешний POST. Поля: path (e.g. "/webhook/agent").
- trigger_imap        — новое email письмо. Поля: cred_id (id IMAP-учётки), filter_from.
- trigger_schedule    — cron. Поля: mode (daily/weekly/hourly/interval/custom), time, weekdays, interval_min, cron, tz.
- trigger_manual      — кнопка «▶ Запуск».

AI / ИНСТРУКЦИЯ:
- node_gpt            — GPT-4o. Поля: system (промпт), temp.
- node_claude         — Claude Sonnet (лучший для длинных текстов/анализа). Поля: system, temp.
- node_gemini         — Gemini Pro (мультимодальность). Поля: system.
- node_grok           — Grok 3 (меньше ограничений). Поля: system, temp.
- prompt              — только текст инструкции (передаётся как system следующему AI).
- orchestrator        — LLM-классификатор: смотрит на input и выбирает ОДНУ из нескольких веток.
                        Поля: model (gpt-4o-mini), strategy. Используй когда нужно ветвление по смыслу.

ЛОГИКА:
- condition           — ветвление по словам. Поля: check ("слово1, слово2").
- switch              — мульти-ветка. Поля: field, branches (формат "name=kw1,kw2\\n*=keyword").
- delay               — пауза. Поля: secs.
- http_request        — внешний API. Поля: method, url, headers (JSON), body (JSON), extract (JSONPath).
- role_switch         — разные системные промпты по chat_id/user_id. Поля: field, default, roles ("name=prompt\\n").
- code_python         — кастомный Python (по умолчанию выключен).

ХРАНИЛИЩЕ (между запусками):
- storage_get         — read. Поля: key.
- storage_set         — write. Поля: key, value.
- storage_push        — append в массив. Поля: key, value, max.

ИНСТРУМЕНТЫ:
- grok_search         — Grok с web/X-поиском. Поля: prompt, enable_web, enable_x.
- rss                 — читает RSS. Поля: urls (по одному), hours, limit.
- extract_text        — извлечь текст из PDF/DOCX. Поля: file_path.
- whisper             — STT (голос → текст). Поля: file_path.
- tts                 — TTS (текст → аудио). Поля: voice (alloy/echo/fable/onyx/nova/shimmer).
- yd_list / yd_upload — Я.Диск.

БАЗА ЗНАНИЙ (RAG):
- kb_add              — добавить файл в БЗ.
- kb_search_file      — поиск по именам файлов БЗ.
- kb_search           — семантический поиск по содержимому. Поля: query, top.
- kb_rag              — готовый RAG (search+answer). Поля: query, model, top.

OUTPUT (отправка ответа):
- output_tg           — TG текст. Поля: tg_token (пусто=из триггера), tg_chat_id, parse_mode.
- output_tg_buttons   — TG inline-кнопки. Поля: buttons ("Да=yes\\nНет=no").
- output_tg_file      — TG файл. Поля: file_path, caption.
- output_tg_audio     — TG голосовое. Поля: file_path.
- output_vk           — VK ответ.
- output_max          — MAX ответ. Поля: max_token (пусто=из триггера), max_user_id (пусто=отвечает тому кто написал).
- output_max_buttons  — MAX inline-кнопки. Поля: buttons («Да=yes\\nНет=no»). Юзер тыкнул кнопку → message_callback с payload как новый input.
- request_contact     — попросить юзера поделиться телефоном через reply-keyboard. Поля: prompt (текст), button (текст кнопки). После нажатия в ctx появятся customer_phone и customer_name. Использовать в шаблонах booking/lead.
- request_location    — попросить геолокацию. Поля: prompt, button. После — customer_lat/customer_lng в ctx.
- output_photo        — отправить картинку. Поля: photo_url (URL или /uploads/...), caption.
- edit_message        — заменить текст ранее отправленного сообщения (TG only). Поля: text (с {{input}}). UX чище — не плодим спам новых сообщений.
- chat_action_typing  — показать «бот печатает…» перед длинным AI-вызовом.
- save_record         — сохранить заявку/бронирование/заказ в bot_records. Поля: record_type (lead/booking/order/quiz/ticket), notify_owner (bool — отправить владельцу в TG). Берёт customer_name/phone/email из ctx + payload из ctx переменных.
- bot_constructor     — мета-нода для бота-конструктора. Когда юзер пишет «/build» (или «готово/создавай») — собирает дочерний ChatBot из истории диалога через workflow_builder и возвращает инструкцию по подключению. Иначе пропускает input дальше как есть. Использовать ОДИН раз в графе перед AI-нодой: trigger_tg → bot_constructor → node_claude → output_tg.
- output_save         — сохранить в историю (видна в ЛК).
- output_hook         — POST на внешний URL. Поля: url.
"""


SYSTEM_PROMPT = (
    "Ты — конструктор воркфлоу для AI-платформы «Студия Че». "
    "Твоя задача — по описанию задачи пользователя собрать граф из готовых нод.\n\n"
    "ПРАВИЛА:\n"
    "1) Ровно ОДИН триггер в графе (первая нода).\n"
    "2) Каждая нода имеет {id, type, x, y, props}. id — короткие \"n1\", \"n2\", ...\n"
    "3) Координаты: триггер слева (x≈80), далее вправо +260 на каждый шаг, y центрируй на 200.\n"
    "4) Ветви разводи по y (200, 380, 60). Слияние ветвей — обратно к одному y.\n"
    "5) edges = [{id, from, to}] — обычно последовательно n1→n2→n3...\n"
    "6) Если задача про чат-бота в TG — обязательно завершай output_tg в конце AI-ветки.\n"
    "7) В props пиши только реально нужные поля (см. каталог); пустые/токены — оставь пустыми.\n"
    "8) Когда юзер хочет «по-разному отвечать на разные темы» — используй orchestrator + 2-3 AI-ветки.\n"
    "9) Системные промпты в node_claude/node_gpt пиши на русском, конкретно по задаче.\n"
    "10) Минимум кода: 2-5 нод хватает для большинства задач.\n\n"
    "Отвечай СТРОГО валидным JSON по схеме:\n"
    '{\n'
    '  "name": "Короткое имя воркфлоу (3-6 слов)",\n'
    '  "explanation": "1-2 предложения: что делает воркфлоу и почему именно эти ноды",\n'
    '  "wfc_nodes": [{"id":"n1","type":"trigger_tg","x":80,"y":200,"props":{}}, ...],\n'
    '  "wfc_edges": [{"id":"e1","from":"n1","to":"n2"}, ...]\n'
    "}\n\n"
    f"КАТАЛОГ НОД:\n{NODE_CATALOG}"
)


def _extract_json(text: str) -> dict:
    """Robust JSON extractor — игнорирует ```code-fences``` и текст вокруг."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r'^```(?:json)?\s*', '', t)
        t = re.sub(r'\s*```\s*$', '', t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    start = t.find('{')
    if start == -1:
        raise ValueError("В ответе модели нет JSON-объекта")
    depth, end = 0, start
    for i in range(start, len(t)):
        if t[i] == '{':
            depth += 1
        elif t[i] == '}':
            depth -= 1
        if depth == 0:
            end = i + 1
            break
    return json.loads(t[start:end])


_VALID_TYPES = {
    "trigger_tg", "trigger_vk", "trigger_avito", "trigger_max", "trigger_webhook",
    "trigger_imap", "trigger_schedule", "trigger_manual",
    "node_gpt", "node_claude", "node_gemini", "node_grok",
    "prompt", "orchestrator",
    "condition", "switch", "delay", "http_request", "role_switch", "code_python",
    "storage_get", "storage_set", "storage_push",
    "grok_search", "rss", "extract_text", "whisper", "tts",
    "yd_list", "yd_upload",
    "kb_add", "kb_search_file", "kb_search", "kb_rag",
    "output_tg", "output_tg_buttons", "output_tg_file", "output_tg_audio",
    "output_vk", "output_max", "output_max_buttons", "output_save", "output_hook",
    # Богатые UX-ноды для шаблонов бизнес-ботов
    "request_contact", "request_location", "output_photo", "edit_message",
    "chat_action_typing",
    # Универсальная нода-сохранение в bot_records (booking/lead/order/quiz)
    "save_record",
    # Мета-нода: «бот, который создаёт ботов»
    "bot_constructor",
}


def _validate(g: dict) -> dict:
    """Проверяет и нормализует граф. Поднимает ValueError при критичных проблемах."""
    nodes = g.get("wfc_nodes") or g.get("nodes") or []
    edges = g.get("wfc_edges") or g.get("edges") or []
    if not nodes:
        raise ValueError("LLM вернул пустой граф")
    seen_ids = set()
    norm_nodes = []
    for i, n in enumerate(nodes, 1):
        nid = str(n.get("id") or f"n{i}")
        if nid in seen_ids:
            nid = f"n{i}_{nid}"
        seen_ids.add(nid)
        ntype = n.get("type", "")
        if ntype not in _VALID_TYPES:
            log.warning(f"Unknown node type '{ntype}', dropping")
            continue
        norm_nodes.append({
            "id": nid,
            "type": ntype,
            "x": int(n.get("x", 80 + (i - 1) * 260)),
            "y": int(n.get("y", 200)),
            "props": n.get("props") or {},
        })
    if not norm_nodes:
        raise ValueError("После валидации не осталось ни одной валидной ноды")
    valid_ids = {n["id"] for n in norm_nodes}
    norm_edges = []
    for j, e in enumerate(edges, 1):
        f, t = e.get("from"), e.get("to")
        if f in valid_ids and t in valid_ids:
            norm_edges.append({"id": str(e.get("id") or f"e{j}"), "from": f, "to": t})
    return {
        "name": (g.get("name") or "AI-воркфлоу")[:60],
        "explanation": (g.get("explanation") or "").strip()[:500],
        "wfc_nodes": norm_nodes,
        "wfc_edges": norm_edges,
    }


# ── Готовые workflow-блоки (templates кусочков) ─────────────────────────────
# Snippets которые AI «склеивает» вместо генерации с нуля. Меньше токенов на
# Claude → дешевле + надёжнее (готовые проверенные паттерны).
# Передаём в SYSTEM_PROMPT — LLM видит примеры структуры.

WORKFLOW_BLOCKS = {
    "lead_capture": """
LEAD-CAPTURE BLOCK — квалификация → запрос контакта → сохранение заявки:
nodes:
  trigger_tg → node_claude (квалификация) → request_contact (попросить телефон)
  → save_record (record_type=lead, notify_owner=true) → output_tg (благодарность)
Вариация: после save_record можно добавить output_tg_buttons для CTA «записаться» / «узнать цены».
""",
    "booking_flow": """
BOOKING BLOCK — выбор услуги → дата → телефон → бронь:
nodes:
  trigger_tg → output_tg_buttons (меню услуг)
  → orchestrator (выбор услуги по callback) → 3 ветки:
       services: node_claude (показать слоты) → output_tg
       prices: output_tg (прайс)
       book: request_contact → save_record (record_type=booking) → output_tg (подтверждение)
""",
    "faq_rag": """
FAQ-RAG BLOCK — поиск в базе знаний с фоллбэком на оператора:
nodes:
  trigger_tg → kb_rag (query={{input}}, top=3, model=gpt-4o-mini)
  → condition (check="не нашёл, не уверен, обратитесь")
       да-ветка: save_record (record_type=ticket) → output_tg «передал оператору»
       нет-ветка: output_tg (готовый ответ из RAG)
""",
    "sales_warmup": """
SALES BLOCK — прогрев с ссылкой на оплату:
nodes:
  trigger_tg → node_claude (выявление потребности) → output_tg_buttons
  (с кнопками «купить»/«узнать больше»/«нет, спасибо»)
  → orchestrator → 3 ветки c разными ответами и save_record(type=lead)
""",
    "quiz_funnel": """
QUIZ BLOCK — серия вопросов → сегментация → персональная рекомендация:
nodes:
  trigger_tg → output_tg_buttons (вопрос 1) → orchestrator (по callback)
  → output_tg_buttons (вопрос 2) → orchestrator → output_tg_buttons (вопрос 3)
  → node_claude (рекомендация по ответам в storage) → save_record (record_type=quiz)
  → output_tg (персональный текст)
""",
    "broadcast": """
BROADCAST BLOCK — лид-магнит при подписке + рассылки:
nodes:
  trigger_tg (только /start) → output_tg_file (PDF лид-магнит)
  → save_record (record_type=subscriber)
  → trigger_schedule (раз в неделю по recipients из БД) → node_claude → output_tg
""",
}


def _select_relevant_blocks(task_lower: str) -> list[str]:
    """Простая эвристика: какие блоки релевантны задаче."""
    keywords = {
        "lead_capture": ["лид", "заявк", "контакт", "телефон", "заинтересова"],
        "booking_flow": ["запис", "брон", "услуг", "салон", "мастер", "слот"],
        "faq_rag": ["вопрос", "faq", "ответ", "база зна", "знани", "поддержк"],
        "sales_warmup": ["продаж", "купить", "оплат", "прогрев", "товар"],
        "quiz_funnel": ["квиз", "тест", "опрос", "анкет", "выбор"],
        "broadcast": ["рассылк", "подпис", "лид-магнит", "новостн"],
    }
    relevant = []
    for block, kws in keywords.items():
        if any(kw in task_lower for kw in kws):
            relevant.append(block)
    return relevant or list(keywords.keys())[:3]  # fallback — топ-3


def _enhance_task_with_gpt(task: str, user_api_key: str | None = None) -> tuple[str, dict]:
    """
    Двухэтапный пайплайн: GPT-4o-mini структурирует сырое описание клиента
    в детальное ТЗ с указанием платформы, нужных полей формы, веток ответов.
    Это позволяет Claude генерить более качественный граф.

    Аналогично _enhance_spec_with_gpt в server/routes/sites.py.
    Стоит ~0.3 ₽, занимает 3-5 сек, заметно поднимает качество workflow.

    Returns: (enhanced_task, usage_dict)
    """
    relevant_blocks = _select_relevant_blocks(task.lower())
    blocks_hint = "\n".join(WORKFLOW_BLOCKS[b] for b in relevant_blocks)
    enhance_prompt = f"""Ты — продакт-менеджер AI-чат-ботов. К тебе пришёл клиент с
сырым описанием бота. Твоя задача — превратить его в структурированное ТЗ
для разработчика-LLM, который соберёт workflow-граф из готовых нод.

Используй markdown-формат с разделами:

## Платформа
TG / VK / MAX / Avito / Widget / несколько сразу

## Цель бота
В одну фразу — что бот делает для клиента бизнеса

## Триггер
Что запускает диалог — приветственное сообщение, команда /start, входящее в чат?

## Сценарий разговора
Пошагово что бот спрашивает и как реагирует:
- шаг 1: ...
- шаг 2: ...

## Поля формы (если собираем заявки)
- имя (текст)
- телефон (request_contact)
- ...

## Ветки ответов
Если разные ответы для разных тем — перечисли темы и реакции

## Уведомления
Куда уведомлять владельца о новой заявке?

## Дополнительные фичи
- Inline-кнопки? (output_tg_buttons / output_max_buttons)
- База знаний RAG? (kb_rag)
- Расписание рассылок? (trigger_schedule)
- AI-ответы? Какая модель — gpt-4o-mini для FAQ или claude для сложных?

ПОЛЕЗНЫЕ ГОТОВЫЕ ПАТТЕРНЫ для этой задачи:
{blocks_hint}

ИСХОДНОЕ ОПИСАНИЕ КЛИЕНТА:
{task}

Выдай детальное ТЗ. Только markdown-текст, без преамбул."""
    try:
        raw = generate_response(
            "gpt-4o-mini",
            [{"role": "user", "content": enhance_prompt}],
            extra={"max_tokens": 2000},
            user_api_key=user_api_key,
        )
        enhanced = raw.get("content", "") if isinstance(raw, dict) else str(raw)
        usage = {
            "input_tokens": raw.get("input_tokens", 0) if isinstance(raw, dict) else 0,
            "output_tokens": raw.get("output_tokens", 0) if isinstance(raw, dict) else 0,
        }
        if enhanced and len(enhanced.strip()) > 200:
            return enhanced.strip(), usage
    except Exception as e:
        log.warning(f"workflow_builder enhance failed (non-fatal): {e}")
    return task, {"input_tokens": 0, "output_tokens": 0}


def build_from_task(task: str, user_api_key: str | None = None,
                    use_enhance: bool = True) -> dict:
    """
    Собирает воркфлоу по тексту задачи.

    use_enhance: если True (default) — сначала GPT-4o-mini структурирует ТЗ,
    потом Claude генерит граф. Себестоимость +0.3 ₽ но качество заметно выше.
    Можно отключить для дешёвых тарифов.

    Возвращает {name, explanation, wfc_nodes, wfc_edges, usage}.
    Бросает ValueError при невалидном LLM-ответе.
    """
    task = (task or "").strip()
    if not task:
        raise ValueError("Пустая задача")
    if len(task) > 4000:
        task = task[:4000]

    enhance_usage = {"input_tokens": 0, "output_tokens": 0}
    final_task = task
    if use_enhance:
        final_task, enhance_usage = _enhance_task_with_gpt(task, user_api_key)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": (
            f"ТЗ от клиента (после структурирования):\n{final_task}\n\n"
            "Собери воркфлоу. Никакого текста кроме JSON."
        )},
    ]
    # Пробуем модели в порядке доступности. Прокси awstore часто падает —
    # тогда переключаемся на gpt/gemini/grok если у пользователя есть рабочие ключи.
    last_err: Exception | None = None
    text = ""
    usage = {"input_tokens": 0, "output_tokens": 0}
    for model in ("claude", "gpt-4o", "gpt-4o-mini", "gemini", "grok"):
        try:
            raw = generate_response(model, messages, user_api_key=user_api_key)
        except Exception as e:
            last_err = e
            log.warning(f"workflow_builder: {model} failed: {e}")
            continue
        text = raw.get("content", "") if isinstance(raw, dict) else str(raw)
        usage = {
            "input_tokens": raw.get("input_tokens", 0) if isinstance(raw, dict) else 0,
            "output_tokens": raw.get("output_tokens", 0) if isinstance(raw, dict) else 0,
        }
        # Распознаём fallback-заглушку из server/ai.py
        if "Сервис временно недоступен" in text or len(text.strip()) < 5:
            log.warning(f"workflow_builder: {model} returned fallback stub")
            text = ""
            continue
        break

    if not text:
        raise ValueError(
            "AI-провайдеры сейчас недоступны (прокси/ключи). "
            "Проверьте API-ключи в админке или попробуйте позже."
        )
    try:
        parsed = _extract_json(text)
    except Exception as e:
        log.error(f"workflow_builder JSON parse failed: {e}; raw={text[:500]}")
        raise ValueError(f"LLM вернул не-JSON: {e}")
    result = _validate(parsed)
    # Сводный usage: enhance (GPT-mini) + основная генерация (Claude/GPT/etc).
    # Caller (routes/chatbots.py) считает по нему стоимость для списания.
    result["usage"] = {
        "input_tokens": int(usage.get("input_tokens", 0)) + int(enhance_usage.get("input_tokens", 0)),
        "output_tokens": int(usage.get("output_tokens", 0)) + int(enhance_usage.get("output_tokens", 0)),
        "enhance_input": int(enhance_usage.get("input_tokens", 0)),
        "enhance_output": int(enhance_usage.get("output_tokens", 0)),
    }
    return result
