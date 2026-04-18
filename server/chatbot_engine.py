"""
Chatbot Engine — ядро постоянных ботов.

Два режима:
1. Простой: system_prompt + AI модель (если нет графа)
2. Граф: исполняет воркфлоу из конструктора (nodes + edges)

RAM-контекст диалогов, отправка сообщений, webhook setup.
"""
import os, json, logging, secrets, time, asyncio
from collections import deque, defaultdict
from datetime import datetime, timedelta

import httpx

from server.ai import generate_response
from server.db import SessionLocal
from server.models import User, Transaction

log = logging.getLogger("chatbot")

# ── RAM-хранилище контекста диалогов ─────────────────────────────────────────
_conversations: dict[str, deque] = defaultdict(lambda: deque(maxlen=20))
_recent_chats: dict[int, dict[str, list]] = defaultdict(lambda: defaultdict(list))

HTTP = httpx.AsyncClient(timeout=30)


# ── Маппинг типов нод конструктора на модели AI ──────────────────────────────
NODE_MODEL_MAP = {
    "node_gpt":     "gpt-4o",
    "node_claude":  "claude",
    "node_gemini":  "gemini",
    "orchestrator": "gpt",
    "prompt":       None,  # не вызывает AI, просто передаёт system prompt дальше
    "agent_smm":    "gpt",
    "agent_copywriter": "gpt",
}


# ══════════════════════════════════════════════════════════════════════════════
#  ОСНОВНАЯ ОБРАБОТКА СООБЩЕНИЯ
# ══════════════════════════════════════════════════════════════════════════════

async def handle_message(bot, chat_id: str, user_text: str,
                         platform: str, user_name: str = "",
                         extra_ctx: dict | None = None) -> str | None:
    """
    Обработать входящее сообщение.
    Если у бота есть граф (workflow) — исполняет его.
    Иначе — простой режим (system_prompt → AI → ответ).
    extra_ctx — флаги типа is_voice/is_file/is_callback + file_id.
    """
    if not _check_daily_limit(bot):
        return None

    # Определяем режим: граф или простой
    workflow = _get_bot_workflow(bot)

    if workflow:
        answer = await _execute_workflow(bot, chat_id, user_text, platform, user_name, workflow,
                                         extra_ctx=extra_ctx or {})
    else:
        answer = await _simple_reply(bot, chat_id, user_text, platform, user_name)

    if answer:
        _save_for_summary(bot.id, chat_id, user_text, answer, user_name, platform)
        _deduct_bot_cost(bot)
        _increment_replies(bot)

    return answer


def _get_bot_workflow(bot) -> dict | None:
    """Извлечь граф воркфлоу из настроек бота (AgentConfig или ChatBot)."""
    # ChatBot может иметь связанный AgentConfig через settings JSON
    if not hasattr(bot, 'settings') or not bot.settings:
        # Для ChatBot — ищем связанный AgentConfig
        if hasattr(bot, 'workflow_json') and bot.workflow_json:
            try:
                return json.loads(bot.workflow_json)
            except Exception:
                pass
        return None
    try:
        settings = json.loads(bot.settings) if isinstance(bot.settings, str) else bot.settings
        if settings.get("wfc_nodes") and settings.get("wfc_edges"):
            return settings
    except Exception:
        pass
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  ПРОСТОЙ РЕЖИМ (без графа)
# ══════════════════════════════════════════════════════════════════════════════

async def _simple_reply(bot, chat_id, user_text, platform, user_name) -> str:
    """Простой режим: system_prompt → AI модель → ответ."""
    key = f"bot_{bot.id}:chat_{chat_id}"
    history = _conversations[key]

    messages = []
    if bot.system_prompt:
        messages.append({"role": "system", "content": bot.system_prompt})
    else:
        messages.append({"role": "system", "content": "Ты полезный AI-ассистент. Отвечай кратко и по делу."})

    for msg in history:
        messages.append(msg)
    messages.append({"role": "user", "content": user_text})

    try:
        result = generate_response(bot.model or "gpt", messages)
        answer = result.get("content", "") if isinstance(result, dict) else str(result)
    except Exception as e:
        log.error(f"[Bot {bot.id}] AI error: {e}")
        return "Произошла ошибка. Попробуйте позже."

    if not answer:
        return "Не удалось получить ответ."

    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": answer})
    return answer


# ══════════════════════════════════════════════════════════════════════════════
#  ИСПОЛНЕНИЕ ГРАФА ВОРКФЛОУ
# ══════════════════════════════════════════════════════════════════════════════

async def _execute_workflow(bot, chat_id, user_text, platform, user_name,
                            workflow: dict, extra_ctx: dict | None = None) -> str:
    """
    Исполнить граф воркфлоу.
    nodes = [{id, type, cfg: {...}}, ...]
    edges = [{id, from, to}, ...]
    """
    nodes = workflow.get("wfc_nodes", [])
    edges = workflow.get("wfc_edges", [])

    if not nodes:
        return await _simple_reply(bot, chat_id, user_text, platform, user_name)

    # Построить граф: node_id → [connected node_ids]
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        adjacency[edge["from"]].append(edge["to"])

    # Найти точку входа (триггер)
    trigger_types = {"trigger_tg", "trigger_manual", "trigger_webhook", "trigger_schedule"}
    trigger_node = None
    for n in nodes:
        if n["type"] in trigger_types:
            trigger_node = n
            break

    if not trigger_node:
        # Если нет триггера — берём первую ноду
        trigger_node = nodes[0] if nodes else None

    if not trigger_node:
        return await _simple_reply(bot, chat_id, user_text, platform, user_name)

    # Контекст выполнения
    key = f"bot_{bot.id}:chat_{chat_id}"
    history = _conversations[key]

    ctx = {
        "input_text": user_text,
        "user_name": user_name,
        "platform": platform,
        "chat_id": chat_id,
        "history": list(history),
        "results": {},      # node_id → output text
        "bot": bot,
        "final_output": "",
    }
    if extra_ctx:
        ctx.update(extra_ctx)

    # Топологическая сортировка
    order = _topo_sort(nodes, edges)
    if not order:
        log.warning(f"[Bot {bot.id}] Cycle in workflow graph, falling back to simple")
        return await _simple_reply(bot, chat_id, user_text, platform, user_name)

    # Индекс нод
    node_map = {n["id"]: n for n in nodes}

    # Исполнение по порядку
    for nid in order:
        node = node_map.get(nid)
        if not node:
            continue

        # Собираем входные данные от предыдущих нод
        inputs = []
        for edge in edges:
            if edge["to"] == nid and edge["from"] in ctx["results"]:
                inputs.append(ctx["results"][edge["from"]])

        input_text = "\n\n".join(inputs) if inputs else ctx["input_text"]

        try:
            output = await _execute_node(node, input_text, ctx)
            ctx["results"][nid] = output
        except Exception as e:
            log.error(f"[Bot {bot.id}] Node {nid} ({node['type']}) error: {e}")
            ctx["results"][nid] = f"[Ошибка: {e}]"

    # Сохраняем контекст
    history.append({"role": "user", "content": user_text})
    if ctx["final_output"]:
        history.append({"role": "assistant", "content": ctx["final_output"]})

    return ctx["final_output"] or ctx["results"].get(order[-1], "")


async def _execute_node(node: dict, input_text: str, ctx: dict) -> str:
    """Исполнить одну ноду графа."""
    ntype = node["type"]
    cfg = node.get("cfg", {})

    # ── Триггеры (входные точки, просто прокидывают текст)
    if ntype.startswith("trigger_"):
        return ctx["input_text"]

    # ── AI модели ──────────────────────────────────────────────────────────
    if ntype in ("node_gpt", "node_claude", "node_gemini"):
        model = NODE_MODEL_MAP.get(ntype, "gpt")
        system = cfg.get("system", "Ты полезный ассистент.")
        messages = [{"role": "system", "content": system}]
        # Добавляем контекст из истории
        for msg in ctx.get("history", [])[-10:]:
            messages.append(msg)
        messages.append({"role": "user", "content": input_text})
        result = generate_response(model, messages)
        answer = result.get("content", "") if isinstance(result, dict) else str(result)
        return answer

    # ── Оркестратор (выбирает агента или просто прокидывает) ───────────────
    if ntype == "orchestrator":
        return input_text

    # ── Инструкция / Prompt Builder ────────────────────────────────────────
    if ntype == "prompt":
        system_text = cfg.get("system", "")
        if system_text:
            return f"{system_text}\n\n{input_text}"
        return input_text

    # ── Условие ────────────────────────────────────────────────────────────
    if ntype == "condition":
        check_words = [w.strip().lower() for w in (cfg.get("check", "")).split(",") if w.strip()]
        text_lower = input_text.lower()
        matched = any(w in text_lower for w in check_words) if check_words else True
        return input_text if matched else ""

    # ── Switch / Router ────────────────────────────────────────────────────
    if ntype == "switch":
        field = cfg.get("field", "text")
        check_value = input_text.lower() if field == "text" else str(ctx.get(field, ""))
        # Парсим ветки: "name=keyword1,keyword2"
        branches_raw = cfg.get("branches", "")
        matched_branch = None
        for line in branches_raw.splitlines():
            if "=" not in line: continue
            name, keywords = line.split("=", 1)
            name = name.strip()
            for kw in keywords.split(","):
                kw = kw.strip().lower()
                if not kw: continue
                if kw == "*" or (kw == "__voice__" and ctx.get("is_voice")) \
                   or (kw == "__file__" and ctx.get("is_file")) \
                   or (kw == "__callback__" and ctx.get("is_callback")) \
                   or kw in check_value:
                    matched_branch = name
                    break
            if matched_branch: break
        ctx["switch_branch"] = matched_branch or "default"
        return input_text

    # ── Задержка ───────────────────────────────────────────────────────────
    if ntype == "delay":
        secs = min(int(cfg.get("secs", 2)), 30)  # макс 30 сек
        await asyncio.sleep(secs)
        return input_text

    # ── HTTP Request ───────────────────────────────────────────────────────
    if ntype == "http_request":
        import json as _json
        method = (cfg.get("method") or "GET").upper()
        url = cfg.get("url", "")
        if not url: return ""
        # Подстановка переменных {{input}} в url/body/headers
        def subst(s): return s.replace("{{input}}", input_text) if s else s
        url = subst(url)
        try:
            headers = _json.loads(subst(cfg.get("headers") or "{}"))
        except Exception:
            headers = {}
        body_raw = subst(cfg.get("body") or "")
        kwargs = {"headers": headers}
        if body_raw:
            try:
                kwargs["json"] = _json.loads(body_raw)
            except Exception:
                kwargs["content"] = body_raw
        try:
            if method == "GET":
                r = await HTTP.get(url, headers=headers)
            else:
                r = await HTTP.request(method, url, **kwargs)
            text = r.text
            # Попытка извлечь JSONPath
            extract = cfg.get("extract", "")
            if extract and extract.startswith("$"):
                try:
                    data = r.json()
                    # Простая реализация: $.key.key или $.key[0].key
                    path = extract[2:].split(".")
                    cur = data
                    for p in path:
                        if "[" in p and "]" in p:
                            name, idx = p.split("[", 1)
                            idx = int(idx.rstrip("]"))
                            cur = cur[name][idx] if name else cur[idx]
                        else:
                            cur = cur[p]
                    return str(cur)
                except Exception:
                    pass
            return text
        except Exception as e:
            log.error(f"[HTTP] {url}: {e}")
            return f"[HTTP error: {e}]"

    # ── Storage ────────────────────────────────────────────────────────────
    if ntype == "storage_get":
        key = cfg.get("key", "")
        return _storage_get(ctx["bot"].id, key)

    if ntype == "storage_set":
        key = cfg.get("key", "")
        val = (cfg.get("value", "{{input}}") or "{{input}}").replace("{{input}}", input_text)
        _storage_set(ctx["bot"].id, key, val)
        return val

    if ntype == "storage_push":
        key = cfg.get("key", "")
        val = (cfg.get("value", "{{input}}") or "{{input}}").replace("{{input}}", input_text)
        max_items = int(cfg.get("max", 100) or 100)
        _storage_push(ctx["bot"].id, key, val, max_items)
        return val

    # ── RSS ────────────────────────────────────────────────────────────────
    if ntype == "rss":
        urls = [u.strip() for u in (cfg.get("urls", "")).splitlines() if u.strip()]
        hours = int(cfg.get("hours", 48) or 48)
        limit = int(cfg.get("limit", 30) or 30)
        articles = await _fetch_rss(urls, hours, limit)
        return "\n\n".join(f"[{a['date']}] {a['title']}\n{a['link']}\n{a['summary']}" for a in articles)

    # ── Extract text (PDF/DOCX/TXT) ────────────────────────────────────────
    if ntype == "extract_text":
        path = (cfg.get("file_path", "{{input}}") or "").replace("{{input}}", input_text)
        return _extract_text_from_file(path)

    # ── Whisper STT ────────────────────────────────────────────────────────
    if ntype == "whisper":
        path = (cfg.get("file_path", "{{input}}") or "").replace("{{input}}", input_text)
        return await _whisper_transcribe(path)

    # ── TTS ────────────────────────────────────────────────────────────────
    if ntype == "tts":
        voice = cfg.get("voice", "onyx")
        audio_path = await _tts_generate(input_text, voice)
        ctx["audio_path"] = audio_path
        return audio_path

    # ── Выходные ноды ──────────────────────────────────────────────────────
    if ntype == "output_tg":
        ctx["final_output"] = input_text
        tg_token = cfg.get("tg_token") or (ctx["bot"].tg_token if hasattr(ctx["bot"], "tg_token") else None)
        tg_chat = cfg.get("tg_chat_id") or ctx.get("chat_id")
        parse_mode = cfg.get("parse_mode", "Markdown")
        if parse_mode == "None": parse_mode = None
        if tg_token and tg_chat and tg_chat != ctx["chat_id"]:
            await send_telegram(tg_token, tg_chat, input_text, parse_mode=parse_mode)
        return input_text

    if ntype == "output_tg_buttons":
        ctx["final_output"] = input_text
        tg_token = cfg.get("tg_token") or (ctx["bot"].tg_token if hasattr(ctx["bot"], "tg_token") else None)
        tg_chat = cfg.get("tg_chat_id") or ctx.get("chat_id")
        buttons = []
        for line in (cfg.get("buttons") or "").splitlines():
            line = line.strip()
            if "=" in line:
                text, data = line.split("=", 1)
                buttons.append({"text": text.strip(), "callback_data": data.strip()})
        if tg_token and tg_chat and buttons:
            await send_telegram_with_buttons(tg_token, tg_chat, input_text, buttons)
        return input_text

    if ntype == "output_tg_file":
        tg_token = cfg.get("tg_token") or (ctx["bot"].tg_token if hasattr(ctx["bot"], "tg_token") else None)
        tg_chat = cfg.get("tg_chat_id") or ctx.get("chat_id")
        path = (cfg.get("file_path") or "").replace("{{input}}", input_text)
        caption = (cfg.get("caption") or "").replace("{{input}}", input_text)
        if tg_token and tg_chat and path:
            await send_telegram_document(tg_token, tg_chat, path, caption)
        return input_text

    if ntype == "output_tg_audio":
        tg_token = cfg.get("tg_token") or (ctx["bot"].tg_token if hasattr(ctx["bot"], "tg_token") else None)
        tg_chat = cfg.get("tg_chat_id") or ctx.get("chat_id")
        path = (cfg.get("file_path") or ctx.get("audio_path") or "").replace("{{input}}", input_text)
        if tg_token and tg_chat and path:
            await send_telegram_audio(tg_token, tg_chat, path)
        return input_text

    if ntype == "output_save":
        ctx["final_output"] = input_text
        return input_text

    if ntype == "output_hook":
        url = cfg.get("url", "")
        if url:
            try:
                await HTTP.post(url, json={
                    "text": input_text,
                    "chat_id": ctx["chat_id"],
                    "platform": ctx["platform"],
                }, timeout=10)
            except Exception as e:
                log.warning(f"[Webhook output] {url}: {e}")
        ctx["final_output"] = input_text
        return input_text

    if ntype == "output_vk":
        ctx["final_output"] = input_text
        vk_token = cfg.get("vk_token") or (ctx["bot"].vk_token if hasattr(ctx["bot"], "vk_token") else None)
        vk_uid = cfg.get("vk_user_id")
        if vk_token and vk_uid:
            await send_vk(vk_token, vk_uid, input_text)
        return input_text

    # ── Агенты из библиотеки (agent_smm, agent_copywriter, etc.) ──────────
    if ntype.startswith("agent_"):
        agent_id = ntype.replace("agent_", "")
        from server.agent_runner import AGENT_REGISTRY
        agent_def = AGENT_REGISTRY.get(agent_id, {})
        system_prompt = agent_def.get("system_prompt", "Ты полезный ассистент.")
        # Добавляем настройки из cfg
        extra_cfg = "\n".join(f"• {k}: {v}" for k, v in cfg.items() if v and str(v).strip())
        if extra_cfg:
            system_prompt += f"\n\nНастройки:\n{extra_cfg}"
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": input_text},
        ]
        result = generate_response("gpt", messages)
        return result.get("content", "") if isinstance(result, dict) else str(result)

    # ── Генерация изображений ──────────────────────────────────────────────
    if ntype == "generate_image":
        result = generate_response("dalle", [
            {"role": "user", "content": input_text}
        ])
        return result.get("content", "") if isinstance(result, dict) else str(result)

    # ── Веб-поиск ──────────────────────────────────────────────────────────
    if ntype == "web_search":
        result = generate_response("perplexity", [
            {"role": "user", "content": input_text}
        ])
        return result.get("content", "") if isinstance(result, dict) else str(result)

    # ── Text filter ────────────────────────────────────────────────────────
    if ntype == "text_filter":
        return input_text

    # По умолчанию — прокидываем текст дальше
    log.warning(f"Unknown node type: {ntype}, passing through")
    return input_text


def _topo_sort(nodes: list, edges: list) -> list[str] | None:
    """Топологическая сортировка графа. Возвращает None если есть цикл."""
    node_ids = {n["id"] for n in nodes}
    in_degree = {nid: 0 for nid in node_ids}
    adj = defaultdict(list)
    for e in edges:
        if e["from"] in node_ids and e["to"] in node_ids:
            adj[e["from"]].append(e["to"])
            in_degree[e["to"]] = in_degree.get(e["to"], 0) + 1

    queue = [nid for nid in node_ids if in_degree[nid] == 0]
    result = []
    while queue:
        nid = queue.pop(0)
        result.append(nid)
        for neighbor in adj[nid]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    return result if len(result) == len(node_ids) else None


# ══════════════════════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ══════════════════════════════════════════════════════════════════════════════

def _check_daily_limit(bot) -> bool:
    now = datetime.utcnow()
    if bot.replies_reset_at and now >= bot.replies_reset_at:
        bot.replies_today = 0
        bot.replies_reset_at = now.replace(hour=0, minute=0, second=0) + timedelta(days=1)
    if not bot.replies_reset_at:
        bot.replies_reset_at = now.replace(hour=0, minute=0, second=0) + timedelta(days=1)
    return (bot.replies_today or 0) < (bot.max_replies_day or 100)


def _increment_replies(bot):
    db = SessionLocal()
    try:
        from server.models import ChatBot
        db_bot = db.query(ChatBot).filter_by(id=bot.id).first()
        if db_bot:
            db_bot.replies_today = (db_bot.replies_today or 0) + 1
            if not db_bot.replies_reset_at:
                db_bot.replies_reset_at = datetime.utcnow().replace(
                    hour=0, minute=0, second=0) + timedelta(days=1)
            db.commit()
    finally:
        db.close()


def _deduct_bot_cost(bot):
    if not bot.cost_per_reply:
        return
    db = SessionLocal()
    try:
        owner = db.query(User).filter_by(id=bot.user_id).first()
        if owner and owner.tokens_balance >= bot.cost_per_reply:
            owner.tokens_balance -= bot.cost_per_reply
            db.add(Transaction(
                user_id=owner.id, type="usage",
                tokens_delta=-bot.cost_per_reply,
                description=f"Бот «{bot.name}» — ответ",
                model=bot.model,
            ))
            db.commit()
        elif owner:
            log.warning(f"[Bot {bot.id}] Недостаточно токенов у пользователя {owner.id}")
    finally:
        db.close()


def _save_for_summary(bot_id, chat_id, user_text, answer, user_name, platform):
    _recent_chats[bot_id][chat_id].append({
        "user": user_name or chat_id,
        "text": user_text,
        "answer": answer[:200],
        "ts": datetime.utcnow().isoformat(),
        "platform": platform,
    })
    if len(_recent_chats[bot_id][chat_id]) > 50:
        _recent_chats[bot_id][chat_id] = _recent_chats[bot_id][chat_id][-50:]


# ── Пересказ диалогов ────────────────────────────────────────────────────────

async def get_summary(bot) -> dict:
    chats = _recent_chats.get(bot.id, {})
    if not chats:
        return {"summary": "Нет диалогов с момента последнего запуска.", "total_chats": 0, "total_messages": 0}

    total_msgs = sum(len(v) for v in chats.values())
    lines = []
    for chat_id, msgs in list(chats.items())[-10:]:
        user_name = msgs[-1]["user"] if msgs else chat_id
        platform = msgs[-1].get("platform", "?") if msgs else "?"
        lines.append(f"[{platform}] {user_name}: {len(msgs)} сообщ. Последнее: {msgs[-1]['text'][:60]}")

    if total_msgs > 20:
        try:
            prompt = "Дай краткий пересказ диалогов бота:\n\n" + "\n".join(lines)
            result = generate_response("gpt", [
                {"role": "system", "content": "Пиши на русском. Кратко, по делу."},
                {"role": "user", "content": prompt},
            ])
            ai_summary = result.get("content", "") if isinstance(result, dict) else str(result)
        except Exception:
            ai_summary = "\n".join(lines)
    else:
        ai_summary = "\n".join(lines) if lines else "Нет данных."

    return {"summary": ai_summary, "total_chats": len(chats), "total_messages": total_msgs}


# ══════════════════════════════════════════════════════════════════════════════
#  ОТПРАВКА СООБЩЕНИЙ В МЕССЕНДЖЕРЫ
# ══════════════════════════════════════════════════════════════════════════════

async def setup_telegram_webhook(tg_token: str, webhook_url: str) -> dict:
    try:
        r = await HTTP.post(
            f"https://api.telegram.org/bot{tg_token}/setWebhook",
            json={"url": webhook_url, "allowed_updates": ["message", "callback_query"]},
        )
        data = r.json()
        log.info(f"[TG] setWebhook → {data}")
        return data
    except Exception as e:
        log.error(f"[TG] setWebhook error: {e}")
        return {"ok": False, "description": str(e)}


async def delete_telegram_webhook(tg_token: str) -> dict:
    try:
        r = await HTTP.post(f"https://api.telegram.org/bot{tg_token}/deleteWebhook")
        return r.json()
    except Exception as e:
        return {"ok": False, "description": str(e)}


async def send_telegram(token: str, chat_id: str, text: str,
                        reply_to: int = None, parse_mode: str = "Markdown") -> dict:
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode: payload["parse_mode"] = parse_mode
    if reply_to: payload["reply_to_message_id"] = reply_to
    try:
        r = await HTTP.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)
        return r.json()
    except Exception as e:
        log.error(f"[TG] send error: {e}")
        try:
            payload.pop("parse_mode", None)
            r = await HTTP.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)
            return r.json()
        except Exception:
            return {"ok": False}


async def send_telegram_with_buttons(token: str, chat_id: str, text: str,
                                     buttons: list) -> dict:
    """Отправить сообщение с inline-кнопками. buttons = [{text, callback_data}, ...]"""
    # Располагаем кнопки по одной в ряд
    keyboard = [[b] for b in buttons]
    payload = {
        "chat_id": chat_id, "text": text,
        "reply_markup": {"inline_keyboard": keyboard},
    }
    try:
        r = await HTTP.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)
        return r.json()
    except Exception as e:
        log.error(f"[TG buttons] {e}")
        return {"ok": False}


async def send_telegram_document(token: str, chat_id: str, file_path: str,
                                 caption: str = "") -> dict:
    """Отправить документ. file_path — относительно корня проекта."""
    import os as _os
    base = _os.path.dirname(_os.path.abspath(__file__))
    abs_path = _os.path.join(base, file_path.lstrip("/"))
    if not _os.path.exists(abs_path):
        log.error(f"[TG doc] file not found: {abs_path}")
        return {"ok": False}
    try:
        with open(abs_path, "rb") as f:
            files = {"document": (_os.path.basename(abs_path), f)}
            data = {"chat_id": str(chat_id)}
            if caption: data["caption"] = caption
            r = await HTTP.post(f"https://api.telegram.org/bot{token}/sendDocument",
                                files=files, data=data)
        return r.json()
    except Exception as e:
        log.error(f"[TG doc] {e}")
        return {"ok": False}


async def send_telegram_audio(token: str, chat_id: str, file_path: str) -> dict:
    import os as _os
    base = _os.path.dirname(_os.path.abspath(__file__))
    abs_path = _os.path.join(base, file_path.lstrip("/"))
    if not _os.path.exists(abs_path):
        log.error(f"[TG audio] file not found: {abs_path}")
        return {"ok": False}
    try:
        with open(abs_path, "rb") as f:
            files = {"voice": (_os.path.basename(abs_path), f)}
            r = await HTTP.post(f"https://api.telegram.org/bot{token}/sendVoice",
                                files=files, data={"chat_id": str(chat_id)})
        return r.json()
    except Exception as e:
        log.error(f"[TG audio] {e}")
        return {"ok": False}


# ══════════════════════════════════════════════════════════════════════════════
#  STORAGE (key-value per bot)
# ══════════════════════════════════════════════════════════════════════════════

def _storage_get(bot_id: int, key: str) -> str:
    from server.db import SessionLocal
    from server.models import WorkflowStore
    db = SessionLocal()
    try:
        row = db.query(WorkflowStore).filter_by(bot_id=bot_id, key=key).first()
        return row.value if row else ""
    finally:
        db.close()


def _storage_set(bot_id: int, key: str, value: str):
    from server.db import SessionLocal
    from server.models import WorkflowStore
    db = SessionLocal()
    try:
        row = db.query(WorkflowStore).filter_by(bot_id=bot_id, key=key).first()
        if row:
            row.value = value
        else:
            db.add(WorkflowStore(bot_id=bot_id, key=key, value=value))
        db.commit()
    finally:
        db.close()


def _storage_push(bot_id: int, key: str, value: str, max_items: int = 100):
    """Пушит в массив (JSON-list), обрезает по max."""
    import json as _json
    from server.db import SessionLocal
    from server.models import WorkflowStore
    db = SessionLocal()
    try:
        row = db.query(WorkflowStore).filter_by(bot_id=bot_id, key=key).first()
        arr = []
        if row:
            try:
                arr = _json.loads(row.value) if row.value else []
                if not isinstance(arr, list): arr = []
            except Exception:
                arr = []
        arr.insert(0, value)
        arr = arr[:max_items]
        payload = _json.dumps(arr, ensure_ascii=False)
        if row:
            row.value = payload
        else:
            db.add(WorkflowStore(bot_id=bot_id, key=key, value=payload))
        db.commit()
    finally:
        db.close()


# ══════════════════════════════════════════════════════════════════════════════
#  RSS
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_rss(urls: list, hours: int = 48, limit: int = 30) -> list:
    """Забирает и парсит RSS-ленты. Возвращает отсортированные статьи."""
    import re as _re
    from email.utils import parsedate_to_datetime
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    cutoff = _dt.now(_tz.utc) - _td(hours=hours)
    results = []
    for url in urls:
        try:
            r = await HTTP.get(url, headers={"User-Agent": "Mozilla/5.0 AICHE-bot"},
                               timeout=15, follow_redirects=True)
            if r.status_code != 200: continue
            xml = r.text
            # Очень простой парсер: ищем <item>...</item> (RSS) или <entry> (Atom)
            items = _re.findall(r"<(?:item|entry)[^>]*>(.*?)</(?:item|entry)>", xml, _re.DOTALL | _re.IGNORECASE)
            for item in items[:20]:
                def extract(tag):
                    m = _re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", item, _re.DOTALL | _re.IGNORECASE)
                    if not m: return ""
                    txt = m.group(1)
                    txt = _re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", txt, flags=_re.DOTALL)
                    txt = _re.sub(r"<[^>]+>", "", txt)
                    return txt.strip()
                title = extract("title")
                link = extract("link") or _re.search(r'<link[^>]+href="([^"]+)"', item)
                if hasattr(link, "group"): link = link.group(1) if link else ""
                pubdate = extract("pubDate") or extract("published") or extract("updated")
                summary = extract("description") or extract("summary") or extract("content")
                article_date = None
                if pubdate:
                    try:
                        article_date = parsedate_to_datetime(pubdate)
                    except Exception:
                        try:
                            article_date = _dt.fromisoformat(pubdate.replace("Z", "+00:00"))
                        except Exception:
                            pass
                if article_date and article_date < cutoff:
                    continue
                if title and link:
                    results.append({
                        "title": title[:200], "link": link[:300],
                        "summary": summary[:400],
                        "date": (article_date or _dt.now(_tz.utc)).strftime("%Y-%m-%d %H:%M"),
                        "_sort": (article_date or _dt.now(_tz.utc)).timestamp(),
                    })
        except Exception as e:
            log.warning(f"[RSS] {url}: {e}")
            continue
    results.sort(key=lambda x: x["_sort"], reverse=True)
    return results[:limit]


# ══════════════════════════════════════════════════════════════════════════════
#  FILE EXTRACT (TXT / PDF / DOCX)
# ══════════════════════════════════════════════════════════════════════════════

def _extract_text_from_file(file_path: str) -> str:
    """Извлечь текст из файла. Поддерживает txt, pdf, docx."""
    import os as _os
    base = _os.path.dirname(_os.path.abspath(__file__))
    abs_path = _os.path.join(base, file_path.lstrip("/")) if not _os.path.isabs(file_path) else file_path
    if not _os.path.exists(abs_path):
        return f"[Файл не найден: {file_path}]"
    ext = _os.path.splitext(abs_path)[1].lower()
    try:
        if ext in (".txt", ".md", ".csv", ".json"):
            with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()[:20000]
        if ext == ".pdf":
            try:
                from PyPDF2 import PdfReader
                reader = PdfReader(abs_path)
                text = "\n\n".join((p.extract_text() or "") for p in reader.pages)
                return text[:20000]
            except Exception as e:
                return f"[PDF error: {e}]"
        if ext == ".docx":
            try:
                import zipfile, xml.etree.ElementTree as ET
                with zipfile.ZipFile(abs_path) as z:
                    with z.open("word/document.xml") as f:
                        tree = ET.parse(f)
                ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
                text = "\n".join(el.text or "" for el in tree.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"))
                return text[:20000]
            except Exception as e:
                return f"[DOCX error: {e}]"
        return f"[Неподдерживаемый формат: {ext}]"
    except Exception as e:
        return f"[Ошибка извлечения: {e}]"


# ══════════════════════════════════════════════════════════════════════════════
#  WHISPER / TTS (OpenAI)
# ══════════════════════════════════════════════════════════════════════════════

async def _whisper_transcribe(file_path: str) -> str:
    """Транскрибировать аудио через OpenAI Whisper."""
    import os as _os
    from server.ai import _get_api_keys
    keys = _get_api_keys("openai")
    if not keys:
        return "[Whisper: нет OpenAI ключей]"
    base = _os.path.dirname(_os.path.abspath(__file__))
    abs_path = _os.path.join(base, file_path.lstrip("/")) if not _os.path.isabs(file_path) else file_path
    if not _os.path.exists(abs_path):
        return f"[Файл не найден: {file_path}]"
    try:
        with open(abs_path, "rb") as f:
            files = {"file": (_os.path.basename(abs_path), f, "audio/mpeg")}
            data = {"model": "whisper-1"}
            r = await HTTP.post(
                "https://api.openai.com/v1/audio/transcriptions",
                files=files, data=data,
                headers={"Authorization": f"Bearer {keys[0]}"},
                timeout=120,
            )
        if r.status_code == 200:
            return r.json().get("text", "")
        return f"[Whisper error {r.status_code}: {r.text[:200]}]"
    except Exception as e:
        return f"[Whisper exception: {e}]"


async def _tts_generate(text: str, voice: str = "onyx") -> str:
    """Генерирует речь через OpenAI TTS, возвращает путь к файлу."""
    import os as _os, uuid as _uuid
    from server.ai import _get_api_keys
    keys = _get_api_keys("openai")
    if not keys:
        return ""
    try:
        r = await HTTP.post(
            "https://api.openai.com/v1/audio/speech",
            json={"model": "tts-1", "voice": voice, "input": text[:4000], "response_format": "mp3"},
            headers={"Authorization": f"Bearer {keys[0]}"},
            timeout=60,
        )
        if r.status_code != 200:
            log.error(f"[TTS] {r.status_code}: {r.text[:200]}")
            return ""
        base = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        upload_dir = _os.path.join(base, "uploads")
        _os.makedirs(upload_dir, exist_ok=True)
        fname = f"tts_{_uuid.uuid4().hex[:12]}.mp3"
        path = _os.path.join(upload_dir, fname)
        with open(path, "wb") as f:
            f.write(r.content)
        return f"/uploads/{fname}"
    except Exception as e:
        log.error(f"[TTS] {e}")
        return ""


async def send_vk(token: str, user_id: str, text: str) -> dict:
    import random
    try:
        r = await HTTP.post("https://api.vk.com/method/messages.send", data={
            "user_id": user_id, "message": text,
            "random_id": random.randint(1, 2**31),
            "access_token": token, "v": "5.131",
        })
        return r.json()
    except Exception as e:
        log.error(f"[VK] send error: {e}")
        return {"error": str(e)}


_avito_tokens: dict[int, tuple[str, float]] = {}

async def _get_avito_token(bot) -> str | None:
    if not bot.avito_client_id or not bot.avito_client_secret:
        return None
    cached = _avito_tokens.get(bot.id)
    if cached and cached[1] > time.time():
        return cached[0]
    try:
        r = await HTTP.post("https://api.avito.ru/token/", data={
            "grant_type": "client_credentials",
            "client_id": bot.avito_client_id,
            "client_secret": bot.avito_client_secret,
        })
        data = r.json()
        token = data.get("access_token")
        expires_in = data.get("expires_in", 3600)
        if token:
            _avito_tokens[bot.id] = (token, time.time() + expires_in - 60)
        return token
    except Exception as e:
        log.error(f"[Avito] token error: {e}")
        return None


async def send_avito(bot, chat_id: str, text: str) -> dict:
    token = await _get_avito_token(bot)
    if not token:
        return {"error": "No Avito token"}
    try:
        r = await HTTP.post(
            f"https://api.avito.ru/messenger/v1/accounts/{bot.avito_user_id}/chats/{chat_id}/messages",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"message": {"text": text}, "type": "text"},
        )
        return r.json()
    except Exception as e:
        log.error(f"[Avito] send error: {e}")
        return {"error": str(e)}


def generate_widget_secret() -> str:
    return secrets.token_urlsafe(16)
