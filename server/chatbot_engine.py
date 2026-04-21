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


# ── SSRF защита для http_request node ─────────────────────────────────────────
# Блокирует обращения к localhost / link-local / приватным сетям / cloud metadata.
# Без этого владелец бота мог бы через http_request читать http://localhost:8000/admin,
# http://169.254.169.254/ (AWS/Яндекс Cloud metadata), сканировать внутреннюю сеть и т.д.
_SSRF_BLOCKED_HOSTS = {"localhost", "0.0.0.0", "metadata.google.internal"}


def _ssrf_validate(url: str) -> str | None:
    """Возвращает текст ошибки если URL ведёт в запрещённую сеть, иначе None."""
    import ipaddress
    import socket
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
    except Exception:
        return "invalid URL"
    if parsed.scheme not in ("http", "https"):
        return "только http/https"
    host = (parsed.hostname or "").lower()
    if not host:
        return "нет хоста"
    if host in _SSRF_BLOCKED_HOSTS:
        return "internal host blocked"
    # Попытка резолва в IP (resolv первой A-записи). Если DNS-rebinding — не защищает
    # на 100%, но отсекает прямое указание приватных IP.
    try:
        infos = socket.getaddrinfo(host, None)
        for info in infos:
            ip_str = info[4][0]
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                continue
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
                return f"private/reserved IP: {ip_str}"
    except socket.gaierror:
        return "DNS-резолв не удался"
    return None


# ── Маппинг типов нод конструктора на модели AI ──────────────────────────────
NODE_MODEL_MAP = {
    "node_gpt":     "gpt-4o",
    "node_claude":  "claude",
    "node_gemini":  "gemini",
    "node_grok":    "grok",
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
    Списание — по РЕАЛЬНЫМ токенам модели (из ctx['_usage']).
    """
    if not _check_daily_limit(bot):
        return None

    # Проверим что у владельца есть хоть какой-то баланс (минимум 1 CH)
    if not _owner_has_balance(bot, minimum=1):
        log.warning(f"[Bot {bot.id}] У владельца {bot.user_id} закончились токены")
        return None

    workflow = _get_bot_workflow(bot)
    # Инициализируем usage в ctx чтобы провайдеры могли его заполнить
    usage_acc = {"input": 0, "output": 0, "cached": 0, "model": bot.model or "gpt"}

    if workflow:
        answer = await _execute_workflow(bot, chat_id, user_text, platform, user_name, workflow,
                                         extra_ctx={**(extra_ctx or {}), "_usage": usage_acc})
    else:
        answer = await _simple_reply(bot, chat_id, user_text, platform, user_name, usage_acc)

    if answer:
        _save_for_summary(bot.id, chat_id, user_text, answer, user_name, platform)
        _deduct_bot_usage(bot, usage_acc)
        _increment_replies(bot)

    return answer


def _owner_has_balance(bot, minimum: int = 1) -> bool:
    from server.db import db_session
    with db_session() as db:
        owner = db.query(User).filter_by(id=bot.user_id).first()
        return bool(owner and (owner.tokens_balance or 0) >= minimum)


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

async def _simple_reply(bot, chat_id, user_text, platform, user_name,
                        usage_acc: dict | None = None) -> str:
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
        if usage_acc and isinstance(result, dict):
            usage_acc["input"] += result.get("input_tokens", 0) or 0
            usage_acc["output"] += result.get("output_tokens", 0) or 0
            usage_acc["cached"] += result.get("cached_tokens", 0) or 0
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
    # Прокидываем для оркестратора и switch
    ctx["_edges"] = edges
    ctx["_nodes_map"] = node_map
    # Набор "отключённых" нод (оркестратор выбрал другую ветку)
    skipped_by_routing: set[str] = set()

    # Исполнение по порядку
    for nid in order:
        if nid in skipped_by_routing:
            continue

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

            # После оркестратора — отключаем все ветки кроме выбранной
            if node.get("type") == "orchestrator":
                chosen = ctx.get("orchestrator_choice")
                my_id = node.get("id")
                all_downstream = [e["to"] for e in edges if e["from"] == my_id]
                for branch_id in all_downstream:
                    if branch_id != chosen:
                        # Рекурсивно отключаем всё ниже этой ветки
                        _collect_downstream(branch_id, edges, skipped_by_routing)
                        skipped_by_routing.add(branch_id)

            # После switch — отключаем ветки чьё имя не совпало с ctx["switch_branch"]
            if node.get("type") == "switch":
                active_branch = ctx.get("switch_branch", "default")
                my_id = node.get("id")
                for edge in edges:
                    if edge["from"] != my_id:
                        continue
                    branch_node = node_map.get(edge["to"])
                    if not branch_node:
                        continue
                    # Имя ветки хранится в cfg.branch_name дочерней ноды (опц.)
                    branch_name = (branch_node.get("cfg", {}) or {}).get("branch_name", "")
                    if branch_name and branch_name != active_branch:
                        _collect_downstream(edge["to"], edges, skipped_by_routing)
                        skipped_by_routing.add(edge["to"])
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
    if ntype in ("node_gpt", "node_claude", "node_gemini", "node_grok"):
        model = NODE_MODEL_MAP.get(ntype, "gpt")
        system = cfg.get("system", "Ты полезный ассистент.")
        if ctx.get("active_role_prompt"):
            system = ctx["active_role_prompt"] + "\n\n" + system
        messages = [{"role": "system", "content": system}]
        for msg in ctx.get("history", [])[-10:]:
            messages.append(msg)
        messages.append({"role": "user", "content": input_text})
        result = generate_response(model, messages)
        answer = result.get("content", "") if isinstance(result, dict) else str(result)
        # Копим usage для финального списания
        usage_acc = ctx.get("_usage")
        if usage_acc and isinstance(result, dict):
            usage_acc["input"] += result.get("input_tokens", 0) or 0
            usage_acc["output"] += result.get("output_tokens", 0) or 0
            usage_acc["cached"] += result.get("cached_tokens", 0) or 0
            usage_acc["model"] = model
        # Пост-обработка RAG-маркеров в ответе
        answer = await _resolve_rag_markers(answer, ctx, model)
        return answer

    # ── Оркестратор: LLM-классификатор выбирает куда направить ─────────────
    if ntype == "orchestrator":
        # Найдём все downstream ноды (куда можно направить)
        edges = ctx.get("_edges", [])
        nodes_map = ctx.get("_nodes_map", {})
        my_id = node.get("id")
        downstream_ids = [e["to"] for e in edges if e["from"] == my_id]
        downstream_nodes = [nodes_map.get(nid) for nid in downstream_ids if nodes_map.get(nid)]

        # Если всего одна нода дальше — просто прокидываем (классификация не нужна)
        if len(downstream_nodes) <= 1:
            return input_text

        # Строим описание вариантов для классификатора
        options = []
        for n in downstream_nodes:
            n_type = n.get("type", "")
            n_cfg = n.get("cfg", {})
            # Первые 100 символов из system prompt или label
            hint = n_cfg.get("system") or n_cfg.get("check") or n_cfg.get("label") or n_type
            options.append({
                "id": n.get("id"),
                "type": n_type,
                "hint": (hint or "")[:150],
            })

        options_text = "\n".join(
            f'{i+1}. id="{o["id"]}" тип={o["type"]}: {o["hint"]}'
            for i, o in enumerate(options)
        )

        model = cfg.get("model", "gpt-4o-mini")
        # Маппинг на наши алиасы
        model_alias = {"gpt-4o-mini": "gpt", "gpt-4o": "gpt-4o",
                       "claude-sonnet-4-6": "claude"}.get(model, "gpt")

        classifier_prompt = (
            "Ты классификатор сообщений для маршрутизации. Определи какой вариант лучше подходит "
            "для обработки входящего запроса. Варианты:\n\n"
            f"{options_text}\n\n"
            f"ЗАПРОС ПОЛЬЗОВАТЕЛЯ: {input_text[:500]}\n\n"
            'Ответь СТРОГО в JSON-формате: {"chosen_id": "id_выбранной_ноды", "reason": "краткое объяснение"}. '
            'Выбирай только ОДИН вариант (id из списка выше). Никакого текста кроме JSON.'
        )
        try:
            result = generate_response(model_alias, [
                {"role": "system", "content": "Ты классификатор. Отвечаешь только JSON."},
                {"role": "user", "content": classifier_prompt},
            ])
            raw = result.get("content", "") if isinstance(result, dict) else str(result)
            # Копим usage
            usage_acc = ctx.get("_usage")
            if usage_acc and isinstance(result, dict):
                usage_acc["input"] += result.get("input_tokens", 0) or 0
                usage_acc["output"] += result.get("output_tokens", 0) or 0
            # Парсим JSON
            import re as _re, json as _json
            m = _re.search(r'\{[^}]+\}', raw)
            if m:
                data = _json.loads(m.group())
                chosen = data.get("chosen_id")
                reason = data.get("reason", "")
                log.info(f"[Orchestrator] выбрал {chosen}: {reason[:80]}")
                ctx["orchestrator_choice"] = chosen
            else:
                # Fallback: первая нода
                ctx["orchestrator_choice"] = downstream_nodes[0].get("id")
        except Exception as e:
            log.error(f"[Orchestrator] error: {e}")
            ctx["orchestrator_choice"] = downstream_nodes[0].get("id")
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
        # SSRF защита: блокируем internal/metadata/private IPs
        err = _ssrf_validate(url)
        if err:
            log.warning(f"[HTTP] SSRF blocked: {url} ({err})")
            return f"[HTTP blocked: {err}]"
        try:
            headers = _json.loads(subst(cfg.get("headers") or "{}"))
        except Exception:
            headers = {}
        body_raw = subst(cfg.get("body") or "")
        kwargs = {"headers": headers, "timeout": 15.0}
        if body_raw:
            try:
                kwargs["json"] = _json.loads(body_raw)
            except Exception:
                kwargs["content"] = body_raw
        try:
            if method == "GET":
                r = await HTTP.get(url, headers=headers, timeout=15.0)
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

    # ── База знаний ────────────────────────────────────────────────────────
    if ntype == "kb_add":
        from server.knowledge import add_file
        import os as _os
        base = _os.path.dirname(_os.path.abspath(__file__))
        # path может быть из cfg или ctx (если файл только что прилетел)
        path = (cfg.get("path") or ctx.get("file_path") or "").replace("{{input}}", input_text)
        name = (cfg.get("name") or ctx.get("file_name") or _os.path.basename(path) or "file").replace("{{input}}", input_text)
        abs_path = _os.path.join(base, path.lstrip("/")) if not _os.path.isabs(path) else path
        content = _extract_text_from_file(path)
        import json as _j
        result = add_file(bot_id=ctx["bot"].id, name=name, path=path,
                          size=_os.path.getsize(abs_path) if _os.path.exists(abs_path) else 0,
                          content_text=content)
        return _j.dumps(result, ensure_ascii=False)

    if ntype == "kb_search_file":
        from server.knowledge import search_file
        query = (cfg.get("query", "{{input}}") or "{{input}}").replace("{{input}}", input_text)
        top = int(cfg.get("top", 5) or 5)
        results = search_file(ctx["bot"].id, query, top)
        if not results: return "[Файл не найден]"
        lines = [f"{r['name']} — {r['description']}" for r in results]
        ctx["found_files"] = results  # для последующего output_tg_file
        return "\n".join(lines)

    if ntype == "kb_search":
        from server.knowledge import search_kb
        query = (cfg.get("query", "{{input}}") or "{{input}}").replace("{{input}}", input_text)
        top = int(cfg.get("top", 5) or 5)
        results = search_kb(ctx["bot"].id, query, top)
        if not results: return "[В базе знаний ничего не найдено]"
        return "\n\n".join(
            f"### {r['name']}\n{r['summary']}\n\nФакты: {r['facts']}"
            for r in results
        )

    if ntype == "kb_rag":
        from server.knowledge import search_kb
        query = (cfg.get("query", "{{input}}") or "{{input}}").replace("{{input}}", input_text)
        top = int(cfg.get("top", 5) or 5)
        model = cfg.get("model", "claude")
        results = search_kb(ctx["bot"].id, query, top)
        if not results:
            return "В базе знаний нет релевантной информации по запросу."
        context_text = "\n\n".join(
            f"[Файл: {r['name']}]\nОписание: {r['description']}\nРезюме: {r['summary']}\nФакты: {r['facts']}"
            for r in results
        )
        prompt = (
            "Ответь на вопрос пользователя используя ТОЛЬКО контекст ниже. "
            "Если ответа нет в контексте — скажи об этом прямо.\n\n"
            f"КОНТЕКСТ:\n{context_text}\n\n"
            f"ВОПРОС: {query}"
        )
        result = generate_response(model, [
            {"role": "system", "content": "Ты ассистент, отвечающий строго по предоставленному контексту."},
            {"role": "user", "content": prompt},
        ])
        return result.get("content", "") if isinstance(result, dict) else str(result)

    # ── Роль / Секция ──────────────────────────────────────────────────────
    if ntype == "role_switch":
        field = cfg.get("field", "chat_id")
        default_role = cfg.get("default", "chat")
        # Карта roles: "name=prompt" (prompt может быть многострочным, но парсим по строкам с =)
        roles_map = {}
        current_role = None
        for line in (cfg.get("roles") or "").splitlines():
            if "=" in line and line.split("=")[0].strip().replace("_","").replace("-","").isalnum():
                name, prompt = line.split("=", 1)
                current_role = name.strip()
                roles_map[current_role] = prompt.strip()
            elif current_role:
                roles_map[current_role] += "\n" + line

        # Определяем активную роль
        if field == "text_first_word":
            words = input_text.strip().split()
            key = words[0].lower() if words else default_role
        else:
            # Роль хранится per-user в storage
            sk = f"role_{field}_{ctx.get(field, ctx.get('chat_id', 'default'))}"
            key = _storage_get(ctx["bot"].id, sk) or default_role

        active_prompt = roles_map.get(key, roles_map.get(default_role, ""))
        ctx["active_role"] = key
        ctx["active_role_prompt"] = active_prompt
        return input_text

    # ── Code (Python sandbox) ──────────────────────────────────────────────
    if ntype == "code_python":
        code = cfg.get("code", "output = input_text")
        return _run_python_sandbox(code, input_text, ctx)

    # ── Yandex.Disk ────────────────────────────────────────────────────────
    if ntype == "yd_list":
        from server.yandex_disk import yd_list_recent
        token = cfg.get("token") or None
        limit = int(cfg.get("limit", 30) or 30)
        items = await yd_list_recent(token, limit)
        return "\n".join(f"{i.get('path','?')} [{i.get('modified','?')}]" for i in items)

    if ntype == "yd_upload":
        from server.yandex_disk import yd_upload
        import os as _os
        token = cfg.get("token") or None
        remote = (cfg.get("remote") or "").replace("{{input}}", input_text)
        local = (cfg.get("local") or input_text).replace("{{input}}", input_text)
        base = _os.path.dirname(_os.path.abspath(__file__))
        local_abs = _os.path.join(base, local.lstrip("/")) if not _os.path.isabs(local) else local
        r = await yd_upload(token, remote, local_abs)
        import json as _j
        return _j.dumps(r, ensure_ascii=False)

    # ── Grok Search (Web + X) ─────────────────────────────────────────────
    if ntype == "grok_search":
        from server.ai import grok_search_response
        prompt = (cfg.get("prompt") or input_text).replace("{{input}}", input_text)
        enable_web = (cfg.get("enable_web", "да") == "да")
        enable_x = (cfg.get("enable_x", "да") == "да")
        model = cfg.get("model", "grok-4-fast-reasoning")
        # grok_search_response синхронная, оборачиваем в thread
        import asyncio as _asyncio
        loop = _asyncio.get_event_loop()
        result = await loop.run_in_executor(None,
            lambda: grok_search_response(prompt, enable_web, enable_x, model))
        return result.get("content", "") if isinstance(result, dict) else str(result)

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
        # Если path пуст и есть found_files в ctx — отправить их
        if not path and ctx.get("found_files"):
            for f in ctx["found_files"][:3]:
                if tg_token and tg_chat and f.get("path"):
                    await send_telegram_document(tg_token, tg_chat, f["path"], f.get("name", ""))
        elif tg_token and tg_chat and path:
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


def _collect_downstream(start_id: str, edges: list, out: set):
    """Собрать все ноды ниже start_id по графу (BFS)."""
    stack = [start_id]
    while stack:
        cur = stack.pop()
        for e in edges:
            if e["from"] == cur and e["to"] not in out:
                out.add(e["to"])
                stack.append(e["to"])


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
    from server.db import db_session
    from server.models import ChatBot
    with db_session() as db:
        db_bot = db.query(ChatBot).filter_by(id=bot.id).first()
        if db_bot:
            db_bot.replies_today = (db_bot.replies_today or 0) + 1
            if not db_bot.replies_reset_at:
                db_bot.replies_reset_at = datetime.utcnow().replace(
                    hour=0, minute=0, second=0) + timedelta(days=1)
            db.commit()


def _deduct_bot_usage(bot, usage: dict):
    """Списать стоимость ответа бота по реальным токенам модели (атомарно)."""
    from server.models import ModelPricing, UsageLog
    from server.db import db_session
    from server.billing import deduct_atomic
    input_tokens = usage.get("input", 0)
    output_tokens = usage.get("output", 0)
    cached_tokens = usage.get("cached", 0)
    model = usage.get("model", bot.model or "gpt")

    with db_session() as db:
        # Расчёт по per-token цене (как в chat.py calculate_cost)
        pricing = db.query(ModelPricing).filter_by(model_id=model).first()
        if pricing and (pricing.ch_per_1k_input > 0 or pricing.ch_per_1k_output > 0):
            cost = (input_tokens / 1000.0) * pricing.ch_per_1k_input + \
                   (output_tokens / 1000.0) * pricing.ch_per_1k_output
            cost = max(int(round(cost)), pricing.min_ch_per_req or 1)
        elif pricing and pricing.cost_per_req:
            cost = pricing.cost_per_req
        else:
            cost = 1  # fallback

        charged = deduct_atomic(db, bot.user_id, cost)

        desc = f"Бот «{bot.name}» [{model}]: {input_tokens}→{output_tokens} ток."
        if charged < cost:
            desc += f" (списано {charged}/{cost})"

        db.add(Transaction(
            user_id=bot.user_id, type="usage",
            tokens_delta=-charged,
            description=desc, model=model,
        ))
        db.add(UsageLog(
            user_id=bot.user_id, model=model,
            input_tokens=input_tokens, output_tokens=output_tokens,
            cached_tokens=cached_tokens, ch_charged=charged,
        ))
        db.commit()


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
    from server.security import tg_webhook_secret
    secret = tg_webhook_secret(tg_token)
    payload = {"url": webhook_url, "allowed_updates": ["message", "callback_query"]}
    if secret:
        payload["secret_token"] = secret
    try:
        r = await HTTP.post(
            f"https://api.telegram.org/bot{tg_token}/setWebhook",
            json=payload,
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
    from server.db import db_session
    from server.models import WorkflowStore
    with db_session() as db:
        row = db.query(WorkflowStore).filter_by(bot_id=bot_id, key=key).first()
        return row.value if row else ""


def _storage_set(bot_id: int, key: str, value: str):
    from server.db import db_session
    from server.models import WorkflowStore
    with db_session() as db:
        row = db.query(WorkflowStore).filter_by(bot_id=bot_id, key=key).first()
        if row:
            row.value = value
        else:
            db.add(WorkflowStore(bot_id=bot_id, key=key, value=value))
        db.commit()


def _storage_push(bot_id: int, key: str, value: str, max_items: int = 100):
    """Пушит в массив (JSON-list), обрезает по max."""
    import json as _json
    from server.db import db_session
    from server.models import WorkflowStore
    with db_session() as db:
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
                import zipfile
                # XXE-защита: используем defusedxml если установлен, иначе строим
                # безопасный parser вручную (без external entities)
                try:
                    from defusedxml.ElementTree import parse as _safe_parse  # type: ignore
                except ImportError:
                    import xml.etree.ElementTree as ET
                    # Парсер с отключенными внешними сущностями
                    def _safe_parse(f):
                        parser = ET.XMLParser()
                        # Явно блокируем DOCTYPE — если встретим, бросим ошибку.
                        # xml.etree не expands external entities по умолчанию, но DTD
                        # всё равно парсится; отключаем на всякий случай.
                        try:
                            parser.parser.DefaultHandler = lambda data: None
                            parser.parser.EntityDeclHandler = lambda *a, **k: (_ for _ in ()).throw(ValueError("entities disabled"))
                        except Exception:
                            pass
                        return ET.parse(f, parser=parser)
                with zipfile.ZipFile(abs_path) as z:
                    with z.open("word/document.xml") as f:
                        tree = _safe_parse(f)
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


# ══════════════════════════════════════════════════════════════════════════════
#  RAG MARKERS — автообработка [KB_SEARCH]/[FILE_SEARCH]/[EMAIL_CONTEXT] в AI-ответе
# ══════════════════════════════════════════════════════════════════════════════

async def _resolve_rag_markers(answer: str, ctx: dict, model: str) -> str:
    """
    Если в ответе AI содержится маркер [KB_SEARCH: query] / [FILE_SEARCH: query] /
    [EMAIL_CONTEXT: query] — делаем поиск и заменяем маркер на результат.
    Для KB — дополнительно просим AI переформулировать ответ с учётом контекста.
    """
    import re as _re
    if not answer: return answer

    bot_id = ctx["bot"].id if ctx.get("bot") else None
    if not bot_id: return answer

    # [FILE_SEARCH: ...] — найти файл и отметить для отправки
    file_match = _re.search(r"\[FILE_SEARCH:\s*(.+?)\]", answer)
    if file_match:
        from server.knowledge import search_file
        q = file_match.group(1).strip()
        results = search_file(bot_id, q, top=1)
        if results:
            ctx["found_files"] = results  # для отправки через output_tg_file
            replacement = f"Отправляю файл: {results[0]['name']}"
            answer = answer.replace(file_match.group(0), replacement)
        else:
            answer = answer.replace(file_match.group(0), f"Файл не найден по запросу «{q}».")

    # [KB_SEARCH: ...] — найти контекст и вызвать AI повторно
    kb_match = _re.search(r"\[KB_SEARCH:\s*(.+?)\]", answer)
    if kb_match:
        from server.knowledge import search_kb
        q = kb_match.group(1).strip()
        results = search_kb(bot_id, q, top=5)
        if results:
            context_text = "\n\n".join(
                f"[{r['name']}] {r['summary']}\nФакты: {r['facts']}" for r in results
            )
            prompt = (
                f"Используя ТОЛЬКО контекст ниже, ответь на исходный вопрос пользователя.\n\n"
                f"КОНТЕКСТ:\n{context_text}\n\n"
                f"ВОПРОС: {q}\n\n"
                f"Предыдущая заготовка ответа (замени её):\n{answer}"
            )
            try:
                result = generate_response(model, [
                    {"role": "system", "content": "Ты отвечаешь строго по контексту. Без маркеров."},
                    {"role": "user", "content": prompt},
                ])
                answer = result.get("content", "") if isinstance(result, dict) else str(result)
            except Exception as e:
                log.error(f"[KB_SEARCH resolve] {e}")
                answer = answer.replace(kb_match.group(0), context_text[:500])
        else:
            answer = answer.replace(kb_match.group(0), "[в базе знаний не найдено]")

    # [EMAIL_CONTEXT: ...] — найти письма через storage.emails
    em_match = _re.search(r"\[EMAIL_CONTEXT:\s*(.+?)\]", answer)
    if em_match:
        import json as _j
        q = em_match.group(1).strip()
        emails_raw = _storage_get(bot_id, "emails") or "[]"
        try:
            emails = _j.loads(emails_raw) if isinstance(emails_raw, str) else []
        except Exception:
            emails = []
        ql = q.lower()
        matched = [e for e in emails if isinstance(e, dict) and
                   (ql in (e.get("from","")).lower() or ql in (e.get("subject","")).lower()
                    or ql in (e.get("body","")).lower())][:5]
        if matched:
            ctx_text = "\n\n".join(
                f"From: {m.get('from','')}\nSubject: {m.get('subject','')}\n{(m.get('body','') or '')[:500]}"
                for m in matched
            )
            try:
                result = generate_response(model, [
                    {"role": "system", "content": "Отвечай по контексту писем."},
                    {"role": "user", "content": f"Контекст писем:\n{ctx_text}\n\nВопрос: {q}"},
                ])
                ai_resp = result.get("content", "") if isinstance(result, dict) else str(result)
                answer = answer.replace(em_match.group(0), ai_resp)
            except Exception:
                answer = answer.replace(em_match.group(0), ctx_text[:500])
        else:
            answer = answer.replace(em_match.group(0), "[писем не найдено]")

    return answer


# ══════════════════════════════════════════════════════════════════════════════
#  PYTHON SANDBOX (ограниченный)
# ══════════════════════════════════════════════════════════════════════════════

_PY_SANDBOX_FORBIDDEN_NAMES = {
    "eval", "exec", "compile", "open", "__import__",
    "globals", "locals", "vars", "getattr", "setattr", "delattr",
    "input", "breakpoint", "exit", "quit", "help",
    "memoryview", "bytearray", "bytes",
}


def _ast_validate_python(code: str) -> str | None:
    """
    Возвращает текст ошибки, если код содержит запрещённые конструкции.
    Не идеален (не гарантирует безопасность), но отсекает большинство атак.
    """
    import ast
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as e:
        return f"Синтаксическая ошибка: {e}"

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return "Импорты запрещены в sandbox"
        if isinstance(node, ast.Attribute):
            # Запрещаем dunder-доступ (.__class__, .__bases__, ...)
            if node.attr.startswith("_"):
                return f"Доступ к скрытым атрибутам ({node.attr}) запрещён"
        if isinstance(node, ast.Name) and node.id in _PY_SANDBOX_FORBIDDEN_NAMES:
            return f"Использование {node.id} запрещено"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _PY_SANDBOX_FORBIDDEN_NAMES:
                return f"Вызов {node.func.id}() запрещён"
    return None


def _run_python_sandbox(code: str, input_text: str, ctx: dict) -> str:
    """
    Выполняет пользовательский Python код. По умолчанию ВЫКЛЮЧЕН
    (ENABLE_PYTHON_SANDBOX=true чтобы включить).

    Даже с AST-валидацией exec() не безопасен — это RCE-вектор.
    Включать только в доверенной среде.
    """
    import json as _j
    import os as _os
    if _os.getenv("ENABLE_PYTHON_SANDBOX", "false").lower() not in ("1", "true", "yes"):
        return "[Python sandbox выключен. Установите ENABLE_PYTHON_SANDBOX=true]"

    err = _ast_validate_python(code)
    if err:
        return f"[Python sandbox: {err}]"

    log.warning(f"[Python sandbox] executing user code (len={len(code)})")
    safe_globals = {
        "__builtins__": {
            "len": len, "range": range, "str": str, "int": int, "float": float,
            "bool": bool, "list": list, "dict": dict, "tuple": tuple, "set": set,
            "sum": sum, "min": min, "max": max, "abs": abs, "round": round,
            "sorted": sorted, "reversed": reversed, "enumerate": enumerate, "zip": zip,
            "map": map, "filter": filter, "print": lambda *a, **k: None,
            "True": True, "False": False, "None": None,
            "isinstance": isinstance, "any": any, "all": all,
        },
        "json": _j,
        "re": __import__("re"),
        "datetime": __import__("datetime"),
    }
    ctx_copy = {k: v for k, v in ctx.items() if k not in ("bot", "history")}
    safe_locals = {
        "input_text": input_text,
        "ctx": ctx_copy,
        "output": "",
    }
    try:
        exec(code, safe_globals, safe_locals)
        out = safe_locals.get("output", "")
        if not isinstance(out, str):
            try: out = _j.dumps(out, ensure_ascii=False)
            except Exception: out = str(out)
        return out[:10000]
    except Exception as e:
        log.error(f"[Python sandbox] error: {e}")
        return f"[Ошибка Python: {e}]"
