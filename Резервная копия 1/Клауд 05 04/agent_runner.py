"""
ReAct Agent Runner — AI Студия Че
===================================
Архитектура: Reason → Act → Observe → Repeat

Агент получает задачу, сам планирует шаги, вызывает инструменты,
анализирует результаты и итерирует до завершения.

Инструменты:
  - web_search      : поиск через Perplexity/DuckDuckGo
  - browse_url      : получить содержимое URL
  - run_llm         : вызов языковой модели (GPT/Claude)
  - generate_image  : генерация картинки через GPT Images
  - generate_video  : генерация видео через Kling
  - send_vk_post    : публикация в ВКонтакте
  - send_tg_message : отправка в Telegram
  - read_file       : читать загруженный файл
  - write_output    : сохранить результат
  - finish          : завершить с ответом

Запуск: python agent_runner.py (background process)
API:    POST /agent/run  GET /agent/{task_id}/status
"""

import os, json, uuid, asyncio, logging, re, time
from datetime import datetime
from typing import Any
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("agent")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [AGENT] %(message)s")

# ── TOOL DEFINITIONS ──────────────────────────────────────────────────────────

TOOL_SCHEMAS = [
    {
        "name": "web_search",
        "description": "Поиск актуальной информации в интернете. Используй для получения свежих данных, новостей, фактов.",
        "parameters": {
            "query": "Поисковый запрос (строка)",
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
            "model": "Модель: gpt | gpt-4o | claude | claude-sonnet | perplexity",
            "prompt": "Запрос к модели",
            "system": "Системный промпт (необязательно)"
        }
    },
    {
        "name": "generate_image",
        "description": "Сгенерировать изображение через DALL-E. Возвращает URL картинки.",
        "parameters": {
            "prompt": "Описание изображения на английском",
            "size": "Размер: 1024x1024 | 1792x1024 | 1024x1792"
        }
    },
    {
        "name": "generate_video",
        "description": "Сгенерировать видео через Kling. Возвращает task_id для проверки статуса.",
        "parameters": {
            "prompt": "Описание видео",
            "aspect_ratio": "16:9 | 9:16",
            "duration": "5 | 10"
        }
    },
    {
        "name": "send_vk_post",
        "description": "Опубликовать пост в сообществе ВКонтакте.",
        "parameters": {
            "message": "Текст поста",
            "image_url": "URL изображения (необязательно)"
        }
    },
    {
        "name": "send_tg_message",
        "description": "Отправить сообщение в Telegram канал/чат.",
        "parameters": {
            "text": "Текст сообщения (поддерживает Markdown)",
            "image_url": "URL изображения (необязательно)"
        }
    },
    {
        "name": "write_output",
        "description": "Сохранить промежуточный или финальный результат. Используй для длинных текстов.",
        "parameters": {
            "content": "Содержимое для сохранения",
            "label": "Метка/заголовок результата"
        }
    },
    {
        "name": "finish",
        "description": "Завершить задачу и вернуть итоговый ответ пользователю.",
        "parameters": {
            "answer": "Финальный ответ / результат для пользователя",
            "summary": "Краткое резюме что было сделано"
        }
    }
]

TOOL_SCHEMA_STR = "\n".join(
    f"• **{t['name']}**({', '.join(t['parameters'].keys())}): {t['description']}"
    for t in TOOL_SCHEMAS
)

# ── SYSTEM PROMPT ─────────────────────────────────────────────────────────────

AGENT_SYSTEM = f"""Ты автономный ИИ-агент AI Студии Че. Ты получаешь задачу и самостоятельно выполняешь её шаг за шагом, используя доступные инструменты.

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

# ── TASK STORE (in-memory + optional DB) ─────────────────────────────────────

tasks: dict[str, dict] = {}   # task_id → task state

def create_task(user_id, goal: str, context: dict = None) -> str:
    tid = str(uuid.uuid4())
    tasks[tid] = {
        "id": tid,
        "user_id": user_id,
        "goal": goal,
        "context": context or {},
        "status": "pending",   # pending / running / done / error
        "steps": [],
        "outputs": [],
        "result": None,
        "created_at": datetime.utcnow().isoformat(),
        "updated_at": datetime.utcnow().isoformat(),
    }
    return tid

def update_task(tid: str, **kwargs):
    if tid in tasks:
        tasks[tid].update(kwargs)
        tasks[tid]["updated_at"] = datetime.utcnow().isoformat()

def add_step(tid: str, step: dict):
    if tid in tasks:
        tasks[tid]["steps"].append({**step, "ts": datetime.utcnow().isoformat()})

# ── TOOL IMPLEMENTATIONS ──────────────────────────────────────────────────────

async def tool_web_search(params: dict, context: dict) -> str:
    query = params.get("query", "")
    num   = min(int(params.get("num_results", 5)), 10)
    log.info(f"[tool] web_search: {query}")

    # Try Perplexity first
    pplx_keys = [k.strip() for k in os.getenv("PERPLEXITY_API_KEYS","").split(",") if k.strip()]
    if pplx_keys:
        try:
            import httpx
            client = httpx.AsyncClient(timeout=15)
            resp = await client.post(
                "https://api.perplexity.ai/chat/completions",
                headers={"Authorization": f"Bearer {pplx_keys[0]}", "Content-Type": "application/json"},
                json={"model":"sonar-small-chat","messages":[
                    {"role":"system","content":"Дай краткий ответ с источниками."},
                    {"role":"user","content":query}
                ]}
            )
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
            return f"Результаты поиска по запросу '{query}':\n\n{text}"
        except Exception as e:
            log.warning(f"Perplexity failed: {e}")

    # Fallback: DuckDuckGo lite
    try:
        import httpx
        client = httpx.AsyncClient(timeout=10)
        resp = await client.get(
            f"https://lite.duckduckgo.com/lite/?q={query.replace(' ', '+')}&kl=ru-ru",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        # Extract text snippets
        text = resp.text
        snippets = re.findall(r'class="result-snippet"[^>]*>(.*?)</td>', text, re.DOTALL)
        titles   = re.findall(r'class="result-link"[^>]*>(.*?)</a>', text, re.DOTALL)
        results  = []
        for i,(t,s) in enumerate(zip(titles[:num], snippets[:num])):
            t_clean = re.sub(r'<[^>]+>','',t).strip()
            s_clean = re.sub(r'<[^>]+>','',s).strip()
            results.append(f"{i+1}. {t_clean}\n   {s_clean}")
        return f"Результаты поиска '{query}':\n" + "\n".join(results) if results else "Результатов не найдено"
    except Exception as e:
        return f"Ошибка поиска: {e}"


async def tool_browse_url(params: dict, context: dict) -> str:
    url = params.get("url", "")
    log.info(f"[tool] browse_url: {url}")
    try:
        import httpx
        client = httpx.AsyncClient(timeout=15, follow_redirects=True)
        resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        text = resp.text
        # Strip HTML tags
        clean = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL)
        clean = re.sub(r'<style[^>]*>.*?</style>', '', clean, flags=re.DOTALL)
        clean = re.sub(r'<[^>]+>', ' ', clean)
        clean = re.sub(r'\s+', ' ', clean).strip()
        return clean[:3000] + ("..." if len(clean)>3000 else "")
    except Exception as e:
        return f"Ошибка загрузки {url}: {e}"


async def tool_run_llm(params: dict, context: dict) -> str:
    model  = params.get("model", "gpt")
    prompt = params.get("prompt", "")
    system = params.get("system", "Ты полезный ассистент.")
    log.info(f"[tool] run_llm: model={model}, prompt[:80]={prompt[:80]}")

    from ai import generate_response
    messages = [{"role":"system","content":system}, {"role":"user","content":prompt}]
    try:
        result = generate_response(model, messages)
        if isinstance(result, dict):
            return result.get("content","")
        return str(result)
    except Exception as e:
        return f"Ошибка LLM: {e}"


async def tool_generate_image(params: dict, context: dict) -> str:
    prompt = params.get("prompt","")
    size   = params.get("size","1024x1024")
    log.info(f"[tool] generate_image: {prompt[:60]}")

    keys = [k.strip() for k in os.getenv("OPENAI_API_KEYS","").split(",") if k.strip()]
    if not keys:
        return "Нет OpenAI ключей для генерации изображений"
    try:
        from openai import OpenAI
        client = OpenAI(api_key=keys[0])
        resp = client.images.generate(model="dall-e-3", prompt=prompt, n=1, size=size)
        return resp.data[0].url or "URL не получен"
    except Exception as e:
        return f"Ошибка генерации: {e}"


async def tool_generate_video(params: dict, context: dict) -> str:
    keys = [k.strip() for k in os.getenv("KLING_API_KEYS","").split(",") if k.strip()]
    if not keys:
        return "[Заглушка] Kling video: нет API ключей. task_id=mock_123"
    try:
        import httpx
        client = httpx.AsyncClient(timeout=30)
        payload = {
            "model": "kling-v1",
            "prompt": params.get("prompt",""),
            "aspect_ratio": params.get("aspect_ratio","16:9"),
            "duration": int(params.get("duration",5)),
            "cfg_scale": 0.5,
        }
        resp = await client.post(
            "https://api.klingai.com/v1/videos/text2video",
            json=payload,
            headers={"Authorization": f"Bearer {keys[0]}", "Content-Type":"application/json"}
        )
        data = resp.json()
        task_id = data.get("data",{}).get("task_id","unknown")
        return f"Видео генерируется. task_id={task_id}"
    except Exception as e:
        return f"Ошибка Kling: {e}"


async def tool_send_vk_post(params: dict, context: dict) -> str:
    token    = context.get("vk_token") or os.getenv("VK_TOKEN","")
    group_id = context.get("vk_group_id") or os.getenv("VK_GROUP_ID","")
    message  = params.get("message","")
    log.info(f"[tool] send_vk_post: {message[:60]}")

    if not token or not group_id:
        return "[Заглушка] VK пост: не настроен токен. Текст: " + message[:100]
    try:
        import httpx
        gid = group_id.lstrip("-")
        client = httpx.AsyncClient(timeout=10)
        resp = await client.post(
            "https://api.vk.com/method/wall.post",
            params={
                "owner_id": f"-{gid}",
                "message": message,
                "from_group": 1,
                "access_token": token,
                "v": "5.131"
            }
        )
        data = resp.json()
        if "error" in data:
            return f"Ошибка VK: {data['error']['error_msg']}"
        post_id = data.get("response",{}).get("post_id","?")
        return f"✅ Пост опубликован в VK. ID: {post_id}"
    except Exception as e:
        return f"Ошибка VK: {e}"


async def tool_send_tg_message(params: dict, context: dict) -> str:
    token   = context.get("tg_token") or os.getenv("TG_BOT_TOKEN","")
    chat_id = context.get("tg_chat_id") or os.getenv("TG_CHAT_ID","")
    text    = params.get("text","")
    log.info(f"[tool] send_tg_message: {text[:60]}")

    if not token or not chat_id:
        return "[Заглушка] TG сообщение: не настроен токен. Текст: " + text[:100]
    try:
        import httpx
        client = httpx.AsyncClient(timeout=10)
        resp = await client.post(
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
    return params.get("answer","Задача выполнена")


TOOLS = {
    "web_search":      tool_web_search,
    "browse_url":      tool_browse_url,
    "run_llm":         tool_run_llm,
    "generate_image":  tool_generate_image,
    "generate_video":  tool_generate_video,
    "send_vk_post":    tool_send_vk_post,
    "send_tg_message": tool_send_tg_message,
    "write_output":    tool_write_output,
    "finish":          tool_finish,
}

# ── REACT LOOP ────────────────────────────────────────────────────────────────

async def run_agent(task_id: str, goal: str, context: dict, max_steps: int = 15):
    """Main ReAct loop."""
    update_task(task_id, status="running")
    log.info(f"[{task_id}] Starting: {goal[:80]}")

    # Build conversation history for the planner
    history = []
    final_answer = None
    outputs = []

    for step_num in range(1, max_steps + 1):
        log.info(f"[{task_id}] Step {step_num}/{max_steps}")

        # Build prompt for planner
        history_str = ""
        for h in history[-8:]:  # last 8 steps for context window
            history_str += f"\n### Шаг {h['step']}\n"
            history_str += f"Думаю: {h['thought']}\n"
            history_str += f"Действие: {h['action']}({json.dumps(h['params'], ensure_ascii=False)})\n"
            history_str += f"Результат: {str(h['observation'])[:500]}\n"

        planner_prompt = f"""Задача: {goal}

Контекст: {json.dumps(context, ensure_ascii=False, default=str)[:500]}

История шагов:{history_str if history_str else " (пусто — первый шаг)"}

Шаг {step_num}. Что делаем дальше? Верни JSON."""

        # Call planner LLM
        try:
            from ai import generate_response
            planner_messages = [
                {"role": "system", "content": AGENT_SYSTEM},
                {"role": "user",   "content": planner_prompt}
            ]
            raw = generate_response("gpt-4o" if os.getenv("OPENAI_API_KEYS") else "gpt",
                                    planner_messages)
            raw_text = raw.get("content","") if isinstance(raw, dict) else str(raw)
        except Exception as e:
            log.error(f"Planner error: {e}")
            update_task(task_id, status="error",
                        result=f"Ошибка планировщика на шаге {step_num}: {e}")
            return

        # Parse JSON from planner response
        try:
            # Extract JSON block
            json_match = re.search(r'\{.*\}', raw_text, re.DOTALL)
            if not json_match:
                raise ValueError("No JSON in response")
            plan = json.loads(json_match.group())
            thought = plan.get("думаю", plan.get("думаю", plan.get("thought", "")))
            action  = plan.get("действие", plan.get("action", "finish"))
            params  = plan.get("параметры", plan.get("parameters", plan.get("params", {})))
        except Exception as e:
            log.warning(f"JSON parse error: {e} — raw: {raw_text[:300]}")
            # Try to extract action from raw text
            thought = raw_text[:200]
            action  = "finish"
            params  = {"answer": raw_text[:500], "summary": "Ошибка парсинга"}

        log.info(f"[{task_id}] Thought: {thought[:80]}")
        log.info(f"[{task_id}] Action:  {action}({json.dumps(params, ensure_ascii=False)[:100]})")

        # Execute tool
        tool_fn = TOOLS.get(action)
        if not tool_fn:
            observation = f"Инструмент '{action}' не найден. Доступные: {', '.join(TOOLS.keys())}"
        else:
            try:
                observation = await tool_fn(params, context)
            except Exception as e:
                observation = f"Ошибка инструмента {action}: {e}"

        log.info(f"[{task_id}] Observe: {str(observation)[:120]}")

        # Record step
        step_record = {
            "step": step_num,
            "thought": thought,
            "action": action,
            "params": params,
            "observation": str(observation)
        }
        history.append(step_record)
        add_step(task_id, step_record)

        # Save output if write_output or finish
        if action == "write_output":
            outputs.append({"label": params.get("label",""), "content": params.get("content","")})
            update_task(task_id, outputs=outputs)

        # Check if done
        if action == "finish":
            final_answer = params.get("answer", str(observation))
            update_task(task_id,
                        status="done",
                        result=final_answer,
                        outputs=outputs,
                        steps_count=step_num)
            log.info(f"[{task_id}] DONE in {step_num} steps")
            return

    # Max steps reached
    update_task(task_id,
                status="done",
                result=f"Достигнут лимит шагов ({max_steps}). Последнее действие: {history[-1]['observation'][:300] if history else ''}",
                steps_count=max_steps)


# ── BACKGROUND RUNNER ─────────────────────────────────────────────────────────

async def agent_worker(queue: asyncio.Queue):
    """Background worker that processes tasks from queue."""
    while True:
        task_id, goal, context = await queue.get()
        try:
            await run_agent(task_id, goal, context)
        except Exception as e:
            log.error(f"Agent worker error for {task_id}: {e}")
            update_task(task_id, status="error", result=str(e))
        queue.task_done()


# Global queue — imported by main.py
agent_queue: asyncio.Queue | None = None

async def init_agent_queue():
    global agent_queue
    agent_queue = asyncio.Queue()
    asyncio.create_task(agent_worker(agent_queue))
    log.info("Agent queue initialized")

async def submit_task(task_id: str, goal: str, context: dict):
    if agent_queue is None:
        await init_agent_queue()
    await agent_queue.put((task_id, goal, context))


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
