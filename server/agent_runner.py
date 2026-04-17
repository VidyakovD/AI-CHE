"""
ReAct Agent Runner — AI Студия Che
===================================
Архитектура: Orchestrator → Registry → ReAct Loop

Оркестратор — центральный компонент:
  classify()        — определяет, какому агенту передать задачу
  compress_history() — MicroCompact / AutoCompact управление контекстом
  run_parallel()    — параллельный запуск независимых подзадач

Registry — расширяемый реестр агентов:
  register_agent()  — добавить агент одной строкой, без изменений ядра
  unregister_agent()
  list_agents()

Queue — приоритетная очередь задач (PRIORITY_HIGH / NORMAL / LOW)

Инструменты:
  web_search, browse_url, run_llm, generate_image, generate_video,
  send_vk_post, send_tg_message, write_output, finish

API: POST /agent/run  GET /agent/{task_id}/status  WS /agent/{task_id}/ws
"""

import os, json, uuid, asyncio, logging, re, time
from datetime import datetime
from typing import Any
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("agent")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [AGENT] %(message)s")

# ── PRIORITY CONSTANTS ────────────────────────────────────────────────────────

PRIORITY_HIGH   = 1
PRIORITY_NORMAL = 2
PRIORITY_LOW    = 3


class PriorityTask:
    """Wrapper for priority queue ordering."""
    __slots__ = ("priority", "task_id", "goal", "context", "orch_config")

    def __init__(self, priority: int, task_id: str, goal: str,
                 context: dict, orch_config: dict | None = None):
        self.priority    = priority
        self.task_id     = task_id
        self.goal        = goal
        self.context     = context
        self.orch_config = orch_config or {}

    def __lt__(self, other):  return self.priority < other.priority
    def __eq__(self, other):  return self.priority == other.priority


# ── AGENT REGISTRY ────────────────────────────────────────────────────────────
# To add a new agent:
#   1. Write async handler(goal, context, max_steps) -> str  (or None for ReAct)
#   2. Call register_agent(...)   — no other changes needed

AGENT_REGISTRY: dict[str, dict] = {}


def register_agent(
    agent_id: str,
    name: str,
    description: str,
    keywords: list[str],
    system_prompt: str | None = None,
    allowed_tools: list[str] | None = None,
    handler=None,
) -> None:
    """Register a new agent type. Idempotent — safe to call on every import.

    Args:
        agent_id:      Unique identifier used for routing ("smm", "lawyer", …)
        name:          Human-readable name
        description:   Short description used by LLM classifier
        keywords:      Keyword list for fast (non-LLM) routing
        system_prompt: Specialized system prompt — "pre-training" for this agent.
                       If None, falls back to the generic AGENT_SYSTEM.
        allowed_tools: Whitelist of tool names this agent may use.
                       If None, all tools are available.
        handler:       Optional custom async(task_id, goal, context, max_steps)->None.
                       If None, the standard ReAct loop is used with system_prompt.
    """
    AGENT_REGISTRY[agent_id] = {
        "id":            agent_id,
        "name":          name,
        "description":   description,
        "keywords":      [k.lower() for k in keywords],
        "system_prompt": system_prompt,
        "allowed_tools": allowed_tools,
        "handler":       handler,
    }
    log.info(f"[Registry] Registered: {agent_id} — {name}")


def unregister_agent(agent_id: str) -> None:
    AGENT_REGISTRY.pop(agent_id, None)


def list_agents() -> list[dict]:
    return [
        {"id": v["id"], "name": v["name"],
         "description": v["description"], "keywords": v["keywords"]}
        for v in AGENT_REGISTRY.values()
    ]


# ── ORCHESTRATOR ──────────────────────────────────────────────────────────────

class Orchestrator:
    """Central routing component — classifies, compresses context, runs parallel tasks."""

    COMPRESSION_NONE  = "none"
    COMPRESSION_AUTO  = "auto"   # AutoCompact: soft, keep last 6 steps
    COMPRESSION_MICRO = "micro"  # MicroCompact: aggressive, keep last 3 steps

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        self.compression      = cfg.get("compression",      self.COMPRESSION_AUTO)
        self.max_parallel     = int(cfg.get("max_parallel", 3))
        self.classifier_model = cfg.get("classifier_model", "gpt")
        self.priority_mode    = cfg.get("priority",         "fifo")   # "fifo" | "smart"

    # ── Classification ────────────────────────────────────────────────────────

    async def classify(self, goal: str) -> str:
        """Return the agent_id best suited for this goal."""
        if not AGENT_REGISTRY:
            return "react"

        # 1. Fast keyword match
        goal_lower = goal.lower()
        for aid, a in AGENT_REGISTRY.items():
            if any(kw in goal_lower for kw in a["keywords"]):
                log.info(f"[Orchestrator] keyword match → {aid}")
                return aid

        # 2. LLM classification fallback
        try:
            agents_desc = "\n".join(
                f"- {aid}: {a['description']}"
                for aid, a in AGENT_REGISTRY.items()
            )
            prompt = (
                f"Запрос: {goal}\n\n"
                f"Доступные агенты:\n{agents_desc}\n- react: универсальный\n\n"
                'Верни JSON: {"agent": "id_агента"}'
            )
            from server.ai import generate_response
            r    = generate_response(self.classifier_model, [{"role": "user", "content": prompt}])
            text = r.get("content", "") if isinstance(r, dict) else str(r)
            m    = re.search(r'"agent"\s*:\s*"([\w_-]+)"', text)
            if m:
                aid = m.group(1)
                if aid in AGENT_REGISTRY or aid == "react":
                    log.info(f"[Orchestrator] LLM classified → {aid}")
                    return aid
        except Exception as e:
            log.warning(f"[Orchestrator] classify error: {e}")

        return "react"

    # ── Context compression ───────────────────────────────────────────────────

    def compress_history(self, history: list[dict]) -> list[dict]:
        """Compress conversation history to manage context window."""
        strategy = self.compression
        n = len(history)

        if strategy == self.COMPRESSION_NONE or n <= 4:
            return history

        if strategy == self.COMPRESSION_MICRO and n > 3:
            summary = f"[MicroCompact: {n - 3} шагов свёрнуто]"
            compact = {"step": 0, "thought": summary, "action": "compact",
                       "params": {}, "observation": summary, "ts": datetime.utcnow().isoformat()}
            return [compact] + history[-3:]

        # AUTO: keep last 6 steps, summarise older
        if n > 6:
            older    = history[:-6]
            snippets = "; ".join(
                str(h.get("observation", ""))[:80]
                for h in older if h.get("observation")
            )
            summary = f"[AutoCompact ({len(older)} шагов): {snippets[:300]}]"
            compact = {"step": 0, "thought": summary, "action": "compact",
                       "params": {}, "observation": summary, "ts": datetime.utcnow().isoformat()}
            return [compact] + history[-6:]

        return history

    # ── Parallel execution ────────────────────────────────────────────────────

    async def run_parallel(
        self,
        subtasks: list[tuple[str, dict]],
        max_steps: int = 8,
    ) -> list[str]:
        """Run up to max_parallel subtasks concurrently. Returns list of results."""
        batch = subtasks[: self.max_parallel]
        coros, tids = [], []
        for goal, ctx in batch:
            tid = create_task(user_id=ctx.get("user_id"), goal=goal, context=ctx)
            tids.append(tid)
            coros.append(run_agent(tid, goal, ctx, max_steps=max_steps))

        results = await asyncio.gather(*coros, return_exceptions=True)
        out = []
        for tid, r in zip(tids, results):
            if isinstance(r, Exception):
                out.append(f"Ошибка: {r}")
            else:
                out.append(tasks.get(tid, {}).get("result") or "")
        return out


# Singleton default orchestrator — overridden per-task via orch_config
default_orchestrator = Orchestrator()


# ── TOOL DEFINITIONS ──────────────────────────────────────────────────────────

TOOL_SCHEMAS = [
    {
        "name": "web_search",
        "description": "Поиск актуальной информации в интернете. Используй для получения свежих данных, новостей, фактов.",
        "parameters": {
            "query":       "Поисковый запрос (строка)",
            "num_results": "Количество результатов, 1-10 (по умолчанию 5)"
        }
    },
    {
        "name": "browse_url",
        "description": "Получить содержимое веб-страницы по URL. Используй после web_search для углублённого изучения.",
        "parameters": {
            "url": "Полный URL страницы"
        }
    },
    {
        "name": "run_llm",
        "description": "Вызвать языковую модель для анализа, суммаризации, перевода, написания текста.",
        "parameters": {
            "model":  "Модель: gpt | gpt-4o | claude | claude-sonnet | perplexity",
            "prompt": "Запрос к модели",
            "system": "Системный промпт (необязательно)"
        }
    },
    {
        "name": "generate_image",
        "description": "Сгенерировать изображение через DALL-E. Возвращает URL картинки.",
        "parameters": {
            "prompt": "Описание изображения на английском",
            "size":   "Размер: 1024x1024 | 1792x1024 | 1024x1792"
        }
    },
    {
        "name": "generate_video",
        "description": "Сгенерировать видео через Kling. Возвращает task_id для проверки статуса.",
        "parameters": {
            "prompt":       "Описание видео",
            "aspect_ratio": "16:9 | 9:16",
            "duration":     "5 | 10"
        }
    },
    {
        "name": "send_vk_post",
        "description": "Опубликовать пост в сообществе ВКонтакте.",
        "parameters": {
            "message":   "Текст поста",
            "image_url": "URL изображения (необязательно)"
        }
    },
    {
        "name": "send_tg_message",
        "description": "Отправить сообщение в Telegram канал/чат.",
        "parameters": {
            "text":      "Текст сообщения (поддерживает Markdown)",
            "image_url": "URL изображения (необязательно)"
        }
    },
    {
        "name": "write_output",
        "description": "Сохранить промежуточный или финальный результат. Используй для длинных текстов.",
        "parameters": {
            "content": "Содержимое для сохранения",
            "label":   "Метка/заголовок результата"
        }
    },
    {
        "name": "finish",
        "description": "Завершить задачу и вернуть итоговый ответ пользователю.",
        "parameters": {
            "answer":  "Финальный ответ / результат для пользователя",
            "summary": "Краткое резюме что было сделано"
        }
    }
]

TOOL_SCHEMA_STR = "\n".join(
    f"• **{t['name']}**({', '.join(t['parameters'].keys())}): {t['description']}"
    for t in TOOL_SCHEMAS
)

# ── SYSTEM PROMPT ─────────────────────────────────────────────────────────────

AGENT_SYSTEM = f"""Ты автономный ИИ-агент AI Студии Che. Ты получаешь задачу и самостоятельно выполняешь её шаг за шагом, используя доступные инструменты.

## Цикл работы (ReAct):
1. **ДУМАЮ**: Анализирую задачу и планирую следующий шаг
2. **ДЕЙСТВУЮ**: Вызываю инструмент
3. **НАБЛЮДАЮ**: Анализирую результат
4. Повторяю до финального ответа

## Доступные инструменты:
{TOOL_SCHEMA_STR}

## Формат ответа:
Всегда отвечай строго в JSON:
```json
{{
  "думаю": "Моё рассуждение о текущем шаге",
  "действие": "название_инструмента",
  "параметры": {{
    "ключ": "значение"
  }}
}}
```

## Правила:
- Разбивай сложные задачи на простые шаги
- Проверяй результаты перед следующим шагом
- При поиске информации — сначала ищи, потом анализируй
- Для публикаций — сначала создай контент, потом публикуй
- Максимум 15 шагов на задачу
- Всегда заканчивай инструментом `finish`
"""

# ── TASK STORE ────────────────────────────────────────────────────────────────

tasks: dict[str, dict] = {}
task_subscribers: dict[str, list] = {}


def create_task(user_id, goal: str, context: dict = None) -> str:
    tid = str(uuid.uuid4())
    tasks[tid] = {
        "id":         tid,
        "user_id":    user_id,
        "goal":       goal,
        "context":    context or {},
        "status":     "pending",
        "steps":      [],
        "outputs":    [],
        "result":     None,
        "created_at": datetime.utcnow().isoformat(),
        "updated_at": datetime.utcnow().isoformat(),
    }
    return tid


def update_task(tid: str, **kwargs):
    if tid in tasks:
        tasks[tid].update(kwargs)
        tasks[tid]["updated_at"] = datetime.utcnow().isoformat()
        _notify_task(tid)


def add_step(tid: str, step: dict):
    if tid in tasks:
        tasks[tid]["steps"].append({**step, "ts": datetime.utcnow().isoformat()})
        _notify_task(tid)


def subscribe_task(tid: str, ws) -> None:
    task_subscribers.setdefault(tid, []).append(ws)


def _notify_task(tid: str) -> None:
    t   = tasks.get(tid)
    if not t:
        return
    msg = json.dumps({"type": "update", "task": t}, ensure_ascii=False)
    for ws in list(task_subscribers.get(tid, [])):
        try:
            asyncio.create_task(ws.send_text(msg))
        except Exception:
            pass


# ── TOOL IMPLEMENTATIONS ──────────────────────────────────────────────────────

async def tool_web_search(params: dict, context: dict) -> str:
    query = params.get("query", "")
    num   = min(int(params.get("num_results", 5)), 10)
    log.info(f"[tool] web_search: {query}")

    pplx_keys = [k.strip() for k in os.getenv("PERPLEXITY_API_KEYS","").split(",") if k.strip()]
    if pplx_keys:
        try:
            import httpx
            resp = await httpx.AsyncClient(timeout=15).post(
                "https://api.perplexity.ai/chat/completions",
                headers={"Authorization": f"Bearer {pplx_keys[0]}", "Content-Type": "application/json"},
                json={"model":"sonar-small-chat","messages":[
                    {"role":"system","content":"Дай краткий ответ с источниками."},
                    {"role":"user","content":query}
                ]}
            )
            text = resp.json()["choices"][0]["message"]["content"]
            return f"Результаты поиска по запросу '{query}':\n\n{text}"
        except Exception as e:
            log.warning(f"Perplexity failed: {e}")

    try:
        import httpx
        resp = await httpx.AsyncClient(timeout=10).get(
            f"https://lite.duckduckgo.com/lite/?q={query.replace(' ', '+')}&kl=ru-ru",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        text     = resp.text
        snippets = re.findall(r'class="result-snippet"[^>]*>(.*?)</td>', text, re.DOTALL)
        titles   = re.findall(r'class="result-link"[^>]*>(.*?)</a>',    text, re.DOTALL)
        results  = []
        for i, (t, s) in enumerate(zip(titles[:num], snippets[:num])):
            results.append(f"{i+1}. {re.sub(r'<[^>]+>','',t).strip()}\n   {re.sub(r'<[^>]+>','',s).strip()}")
        return f"Результаты поиска '{query}':\n" + "\n".join(results) if results else "Результатов не найдено"
    except Exception as e:
        return f"Ошибка поиска: {e}"


async def tool_browse_url(params: dict, context: dict) -> str:
    url = params.get("url", "")
    log.info(f"[tool] browse_url: {url}")
    try:
        import httpx
        resp  = await httpx.AsyncClient(timeout=15, follow_redirects=True).get(
            url, headers={"User-Agent": "Mozilla/5.0"}
        )
        clean = re.sub(r'<script[^>]*>.*?</script>', '', resp.text, flags=re.DOTALL)
        clean = re.sub(r'<style[^>]*>.*?</style>',   '', clean,     flags=re.DOTALL)
        clean = re.sub(r'<[^>]+>', ' ', clean)
        clean = re.sub(r'\s+',    ' ', clean).strip()
        return clean[:3000] + ("..." if len(clean) > 3000 else "")
    except Exception as e:
        return f"Ошибка загрузки {url}: {e}"


async def tool_run_llm(params: dict, context: dict) -> str:
    model  = params.get("model", "gpt")
    prompt = params.get("prompt", "")
    system = params.get("system", "Ты полезный ассистент.")
    log.info(f"[tool] run_llm: model={model}, prompt[:80]={prompt[:80]}")
    from server.ai import generate_response
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    user_api_key = context.get("user_api_key")
    try:
        result = generate_response(model, messages, user_api_key=user_api_key)
        return result.get("content", "") if isinstance(result, dict) else str(result)
    except Exception as e:
        return f"Ошибка LLM: {e}"


async def tool_generate_image(params: dict, context: dict) -> str:
    prompt = params.get("prompt", "")
    size   = params.get("size", "1024x1024")
    log.info(f"[tool] generate_image: {prompt[:60]}")
    keys = [k.strip() for k in os.getenv("OPENAI_API_KEYS","").split(",") if k.strip()]
    if not keys:
        return "Нет OpenAI ключей для генерации изображений"
    try:
        from openai import OpenAI
        resp = OpenAI(api_key=keys[0]).images.generate(
            model="dall-e-3", prompt=prompt, n=1, size=size
        )
        return resp.data[0].url or "URL не получен"
    except Exception as e:
        return f"Ошибка генерации: {e}"


async def tool_generate_video(params: dict, context: dict) -> str:
    keys = [k.strip() for k in os.getenv("KLING_API_KEYS","").split(",") if k.strip()]
    if not keys:
        return "[Заглушка] Kling video: нет API ключей. task_id=mock_123"
    try:
        import httpx
        payload = {
            "model": "kling-v1",
            "prompt": params.get("prompt", ""),
            "aspect_ratio": params.get("aspect_ratio", "16:9"),
            "duration": int(params.get("duration", 5)),
            "cfg_scale": 0.5,
        }
        resp    = await httpx.AsyncClient(timeout=30).post(
            "https://api.klingai.com/v1/videos/text2video",
            json=payload,
            headers={"Authorization": f"Bearer {keys[0]}", "Content-Type": "application/json"}
        )
        task_id = resp.json().get("data", {}).get("task_id", "unknown")
        return f"Видео генерируется. task_id={task_id}"
    except Exception as e:
        return f"Ошибка Kling: {e}"


async def tool_send_vk_post(params: dict, context: dict) -> str:
    token    = context.get("vk_token")    or os.getenv("VK_TOKEN", "")
    group_id = context.get("vk_group_id") or os.getenv("VK_GROUP_ID", "")
    message  = params.get("message", "")
    log.info(f"[tool] send_vk_post: {message[:60]}")
    if not token or not group_id:
        return "[Заглушка] VK пост: не настроен токен. Текст: " + message[:100]
    try:
        import httpx
        resp = await httpx.AsyncClient(timeout=10).post(
            "https://api.vk.com/method/wall.post",
            params={"owner_id": f"-{group_id.lstrip('-')}", "message": message,
                    "from_group": 1, "access_token": token, "v": "5.131"}
        )
        data = resp.json()
        if "error" in data:
            return f"Ошибка VK: {data['error']['error_msg']}"
        return f"✅ Пост опубликован в VK. ID: {data.get('response',{}).get('post_id','?')}"
    except Exception as e:
        return f"Ошибка VK: {e}"


async def tool_send_tg_message(params: dict, context: dict) -> str:
    token   = context.get("tg_token")   or os.getenv("TG_BOT_TOKEN", "")
    chat_id = context.get("tg_chat_id") or os.getenv("TG_CHAT_ID", "")
    text    = params.get("text", "")
    log.info(f"[tool] send_tg_message: {text[:60]}")
    if not token or not chat_id:
        return "[Заглушка] TG сообщение: не настроен токен. Текст: " + text[:100]
    try:
        import httpx
        resp = await httpx.AsyncClient(timeout=10).post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        )
        data = resp.json()
        if not data.get("ok"):
            return f"Ошибка Telegram: {data.get('description','')}"
        return f"✅ Сообщение отправлено в Telegram. msg_id={data['result']['message_id']}"
    except Exception as e:
        return f"Ошибка Telegram: {e}"


async def tool_write_output(params: dict, context: dict) -> str:
    return f"✅ Сохранено: {params.get('label','результат')} ({len(params.get('content',''))} символов)"


async def tool_finish(params: dict, context: dict) -> str:
    return params.get("answer", "Задача выполнена")


TOOLS = {
    "web_search":     tool_web_search,
    "browse_url":     tool_browse_url,
    "run_llm":        tool_run_llm,
    "generate_image": tool_generate_image,
    "generate_video": tool_generate_video,
    "send_vk_post":   tool_send_vk_post,
    "send_tg_message":tool_send_tg_message,
    "write_output":   tool_write_output,
    "finish":         tool_finish,
}

# ── REACT LOOP ────────────────────────────────────────────────────────────────

async def run_agent(
    task_id: str,
    goal: str,
    context: dict,
    max_steps: int = 15,
    orchestrator: Orchestrator | None = None,
    system_override: str | None = None,
    tools_whitelist: list[str] | None = None,
):
    """Main ReAct loop. Uses orchestrator for compression if provided.

    system_override: replaces AGENT_SYSTEM (agent "pre-training")
    tools_whitelist: only these tools are available to this agent run
    """
    orch         = orchestrator or default_orchestrator
    active_system = system_override or AGENT_SYSTEM
    active_tools  = {k: v for k, v in TOOLS.items()
                     if tools_whitelist is None or k in tools_whitelist}
    update_task(task_id, status="running")
    log.info(f"[{task_id}] Starting: {goal[:80]}")

    history      = []
    outputs      = []
    final_answer = None

    for step_num in range(1, max_steps + 1):
        log.info(f"[{task_id}] Step {step_num}/{max_steps}")

        # ── Compress history ──────────────────────────────────────────────
        compressed = orch.compress_history(history)

        history_str = ""
        for h in compressed:
            history_str += f"\n### Шаг {h['step']}\n"
            history_str += f"Думаю: {h['thought']}\n"
            history_str += f"Действие: {h['action']}({json.dumps(h['params'], ensure_ascii=False)})\n"
            history_str += f"Результат: {str(h['observation'])[:500]}\n"

        planner_prompt = (
            f"Задача: {goal}\n\n"
            f"Контекст: {json.dumps(context, ensure_ascii=False, default=str)[:500]}\n\n"
            f"История шагов:{history_str if history_str else ' (пусто — первый шаг)'}\n\n"
            f"Шаг {step_num}. Что делаем дальше? Верни JSON."
        )

        # ── Call planner ──────────────────────────────────────────────────
        try:
            from server.ai import generate_response
            planner_messages = [
                {"role": "system", "content": active_system},
                {"role": "user",   "content": planner_prompt}
            ]
            user_api_key = context.get("user_api_key")
            api_provider = context.get("api_provider", "")
            if user_api_key and api_provider == "anthropic":
                planner_model = "claude-sonnet-4-6"
            elif user_api_key and api_provider == "gemini":
                planner_model = "gemini-1.5-pro"
            elif user_api_key and api_provider == "grok":
                planner_model = "grok-2"
            elif user_api_key and api_provider == "openai":
                planner_model = "gpt-4o"
            else:
                planner_model = "gpt-4o" if os.getenv("OPENAI_API_KEYS") else "gpt"
            raw      = generate_response(
                planner_model,
                planner_messages,
                user_api_key=user_api_key,
            )
            raw_text = raw.get("content", "") if isinstance(raw, dict) else str(raw)
        except Exception as e:
            log.error(f"Planner error: {e}")
            update_task(task_id, status="error",
                        result=f"Ошибка планировщика на шаге {step_num}: {e}")
            return

        # ── Parse JSON (robust: try direct parse, then balanced brace extraction) ─
        def _extract_json(text: str) -> dict:
            text = text.strip()
            if text.startswith("```"):
                text = re.sub(r'^```(?:json)?\s*', '', text)
                text = re.sub(r'\s*```\s*$', '', text)
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
            # Balanced brace extraction
            start = text.find('{')
            if start == -1:
                raise ValueError("No JSON object found in response")
            depth, end = 0, start
            for i in range(start, len(text)):
                if text[i] == '{': depth += 1
                elif text[i] == '}': depth -= 1
                if depth == 0:
                    end = i + 1
                    break
            return json.loads(text[start:end])

        try:
            plan = _extract_json(raw_text)
            thought = plan.get("думаю",    plan.get("thought", ""))
            action  = plan.get("действие", plan.get("action",  "finish"))
            params  = plan.get("параметры",plan.get("parameters", plan.get("params", {})))
        except Exception as e:
            log.warning(f"JSON parse error: {e} — raw: {raw_text[:300]}")
            thought = raw_text[:200]
            action  = "finish"
            params  = {"answer": raw_text[:500], "summary": "Ошибка парсинга"}

        log.info(f"[{task_id}] Thought: {thought[:80]}")
        log.info(f"[{task_id}] Action:  {action}({json.dumps(params, ensure_ascii=False)[:100]})")

        # ── Execute tool ──────────────────────────────────────────────────
        tool_fn = active_tools.get(action) or TOOLS.get(action)
        if not tool_fn:
            observation = f"Инструмент '{action}' не найден. Доступные: {', '.join(TOOLS.keys())}"
        else:
            try:
                observation = await tool_fn(params, context)
            except Exception as e:
                observation = f"Ошибка инструмента {action}: {e}"

        log.info(f"[{task_id}] Observe: {str(observation)[:120]}")

        # ── Record step ───────────────────────────────────────────────────
        step_record = {
            "step": step_num, "thought": thought, "action": action,
            "params": params, "observation": str(observation)
        }
        history.append(step_record)
        add_step(task_id, step_record)

        if action == "write_output":
            outputs.append({"label": params.get("label",""), "content": params.get("content","")})
            update_task(task_id, outputs=outputs)

        if action == "finish":
            final_answer = params.get("answer", str(observation))
            update_task(task_id, status="done", result=final_answer,
                        outputs=outputs, steps_count=step_num)
            log.info(f"[{task_id}] DONE in {step_num} steps")
            return

    # Max steps reached
    update_task(
        task_id, status="done",
        result=f"Достигнут лимит шагов ({max_steps}). "
               f"Последнее: {history[-1]['observation'][:300] if history else ''}",
        steps_count=max_steps,
    )


# ── BACKGROUND RUNNER ─────────────────────────────────────────────────────────

async def agent_worker(queue: asyncio.PriorityQueue):
    """Background worker — processes tasks from priority queue."""
    while True:
        pt: PriorityTask = await queue.get()
        try:
            # Build per-task orchestrator if config provided
            orch = Orchestrator(pt.orch_config) if pt.orch_config else default_orchestrator

            # Classify and route
            agent_id  = await orch.classify(pt.goal)
            agent_def = AGENT_REGISTRY.get(agent_id, {})
            handler   = agent_def.get("handler")

            if handler:
                log.info(f"[Worker] Custom handler: {pt.task_id} → {agent_id}")
                await handler(pt.task_id, pt.goal, pt.context, 12)
            else:
                log.info(f"[Worker] ReAct loop: {pt.task_id} → {agent_id or 'default'}")
                # Inject user's business config into system prompt
                base_prompt = agent_def.get("system_prompt")
                block_config = pt.context.get("block_configs", {}).get(agent_id, {})
                if base_prompt and block_config:
                    cfg_lines = "\n".join(
                        f"• {k}: {v}" for k, v in block_config.items() if v and str(v).strip()
                    )
                    if cfg_lines:
                        base_prompt = (
                            base_prompt
                            + f"\n\n=== НАСТРОЙКИ БИЗНЕСА ПОЛЬЗОВАТЕЛЯ ===\n{cfg_lines}"
                            + "\n\nЭти настройки приоритетны. Используй их при выполнении всех шагов."
                        )
                await run_agent(
                    pt.task_id, pt.goal, pt.context,
                    orchestrator=orch,
                    system_override=base_prompt,
                    tools_whitelist=agent_def.get("allowed_tools"),
                )
        except Exception as e:
            log.error(f"Agent worker error for {pt.task_id}: {e}")
            update_task(pt.task_id, status="error", result=str(e))
        queue.task_done()


# Global priority queue
agent_queue: asyncio.PriorityQueue | None = None


async def init_agent_queue():
    global agent_queue
    agent_queue = asyncio.PriorityQueue()
    asyncio.create_task(agent_worker(agent_queue))
    log.info("Agent queue initialized")


async def submit_task(
    task_id: str,
    goal: str,
    context: dict,
    priority: int = PRIORITY_NORMAL,
    orch_config: dict | None = None,
):
    """Submit a task to the priority queue."""
    global agent_queue
    if agent_queue is None:
        await init_agent_queue()
    pt = PriorityTask(priority, task_id, goal, context, orch_config)
    await agent_queue.put(pt)


# ── STANDALONE MODE ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    async def demo():
        goal = " ".join(sys.argv[1:]) or "Найди последние новости об ИИ за сегодня и напиши краткую сводку на русском"
        tid = create_task(user_id=0, goal=goal)
        print(f"Task ID: {tid}")
        print(f"Goal: {goal}\n")
        await run_agent(tid, goal, {}, max_steps=8)
        t = tasks[tid]
        print(f"\n{'='*60}")
        print(f"Status: {t['status']}")
        print(f"Steps:  {len(t['steps'])}")
        print(f"\nResult:\n{t['result']}")

    asyncio.run(demo())
