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
                         platform: str, user_name: str = "") -> str | None:
    """
    Обработать входящее сообщение.
    Если у бота есть граф (workflow) — исполняет его.
    Иначе — простой режим (system_prompt → AI → ответ).
    """
    if not _check_daily_limit(bot):
        return None

    # Определяем режим: граф или простой
    workflow = _get_bot_workflow(bot)

    if workflow:
        answer = await _execute_workflow(bot, chat_id, user_text, platform, user_name, workflow)
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
                            workflow: dict) -> str:
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

    # ── Задержка ───────────────────────────────────────────────────────────
    if ntype == "delay":
        secs = min(int(cfg.get("secs", 2)), 30)  # макс 30 сек
        await asyncio.sleep(secs)
        return input_text

    # ── Выходные ноды ──────────────────────────────────────────────────────
    if ntype == "output_tg":
        ctx["final_output"] = input_text
        # Если указан конкретный chat_id в ноде — отправить туда тоже
        tg_token = cfg.get("tg_token") or (ctx["bot"].tg_token if hasattr(ctx["bot"], "tg_token") else None)
        tg_chat = cfg.get("tg_chat_id")
        if tg_token and tg_chat and tg_chat != ctx["chat_id"]:
            await send_telegram(tg_token, tg_chat, input_text)
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
                        reply_to: int = None) -> dict:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
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
