# Модуль 10 — AI-Agents & Workflows

> **Что это:** AI-агенты для бизнес-задач (отвечают по почте, мониторят VK, парсят рынок, постят посты). Workflow-конструктор (50+ блоков) + 25+ специализированных ролей. Не путать с чат-ботами ([05-chatbots.md](05-chatbots.md)) — агенты долгоживущие, чат-боты для общения с клиентами.

## TL;DR

- **Runner:** [server/agent_runner.py](server/agent_runner.py) — оркестратор, async execution, WS stream.
- **Builder:** [server/workflow_builder.py](server/workflow_builder.py) — AI генерит граф из задачи юзера.
- **Routes:** [server/routes/agent.py](server/routes/agent.py) — run / status / ws / config.
- **UI:** [views/agents.html](views/agents.html) (3445 строк) + [views/workflow.html](views/workflow.html) + [views/workflows.html](views/workflows.html).
- **Tools:** web_search (sonar) / `perplexity_research` (3 пресета) / browse_url (SSRF-safe) / run_llm (с prompt-injection защитой) / generate_image / generate_video / send_vk_post / send_tg_message.

## Архитектура

```
User → /agent/run (POST) → return task_id
     → /agent/{task_id}/ws (WebSocket) — стрим прогресса
     → /agent/{task_id}/status — текущее состояние

Workflow JSON: { nodes: [...], edges: [...] }
  trigger_* → процессинг → отправка → output
```

## Tools (внутри агента)

| Tool | Что | Биллинг |
|---|---|---|
| `tool_web_search` | Perplexity sonar — быстрый поиск | real × margin |
| `tool_perplexity_research` | 3 пресета: quick/standard/deep | **real × margin × 5** (тут особая накрутка) |
| `tool_browse_url` | Скачать страницу, SSRF-safe (DNS rebinding + CIDR-блок + scheme whitelist) | бесплатно |
| `tool_run_llm` | LLM-вызов **с prompt-injection защитой** (`<user_data>` обёртка + system-guard) | real × margin |
| `tool_generate_image` | DALL-E 3 / GPT-image | real × margin |
| `tool_generate_video` | Veo 2/3 / Kling | real × margin |
| `tool_send_vk_post` | Постинг во VK | — |
| `tool_send_tg_message` | Сообщение в TG | — |

## Ролевые промпты

[server/agents/](server/agents/) — 25+ специализированных ролей:
- Маркетолог
- SMM-менеджер
- Юрист
- Финансовый аналитик
- Менеджер по продажам
- Контент-маркетолог
- Reception/секретарь
- ... и т.д.

Каждая роль — system_prompt + предустановленные tools + дефолтная модель.

## Модели

| Таблица | Поля |
|---|---|
| `agent_configs` | user_id, role, system_prompt_override, tools_enabled, schedule_json |
| `workflow_stores` | сохранённые workflows для повторного использования |

## Endpoints

| Метод | Endpoint | Что |
|---|---|---|
| POST | `/agent/run` | Запуск task → return task_id |
| GET | `/agent/{task_id}/status` | Статус |
| WebSocket | `/agent/{task_id}/ws` | Live-stream progress |
| GET/POST/PUT/DELETE | `/agent/config` | CRUD конфигов |
| POST | `/agent/ai-build-workflow` | AI генерит граф из описания задачи |

## AI-builder workflow (анти-паттерны)

`workflow_builder.py` SYSTEM_PROMPT содержит:
- **Анти-паттерны** (`6f590f0`) — что нельзя делать (бесконечные циклы, рекурсия без exit)
- **Автоочистка orphan-нод** (`6f590f0`)
- **Один триггер на граф** (`0dd0642`) — если мульти-канал → отдельные боты, не один с мульти-триггером

## Безопасность

- ✅ **/ws и /stream IDOR-защита** — только owner task'а
- ✅ **SSRF в tool_browse_url** — DNS + CIDR + scheme whitelist
- ✅ **Prompt-injection в tool_run_llm** — `<user_data>` теги + system-guard (`04ded59`)
- ✅ **code_python sandbox** — restricted builtins

## Гочча

- **`/agent/run` не возвращает результат сразу** — async-task. Frontend подключается к /ws.
- **Не путать с chatbot** — агенты долгоживущие, chatbots реагируют на webhooks.
- **`asyncio.create_task` сохранять в `_pending_tasks`** — иначе GC убьёт.

## Тесты

- `tests/test_smoke_builders.py::TestAgentRunner` — smoke
- `tests/test_security_hardening.py` — prompt-injection

## Зависимости

- [03-ai-core](03-ai-core.md) — все llm-вызовы
- [11-knowledge-rag](11-knowledge-rag.md) — rag_search tool
- [02-billing](02-billing-payments.md) — списания (margin × 5 для perplexity_research)
