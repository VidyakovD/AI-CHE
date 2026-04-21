# 03. Вкладка ИИ Агенты — архитектура (самая путаная часть проекта)

## 3 разных «агента» — НЕ путать

| # | Сущность | Где живёт | Что такое |
|---|---|---|---|
| 1 | `AGENT_BLOCKS` | views/agents.html:664 (JS-массив) | Витрина готовых карточек в Библиотеке. Только UI-метаданные (имя, иконка, configFields). |
| 2 | `AGENT_REGISTRY` | server/agent_runner.py:62 + server/agents/registry.py | Реальные агенты на бэкенде: system_prompt, keywords, allowed_tools. 22 штуки. |
| 3 | `AgentConfig` | server/models.py:409 (SQL `agent_configs`) | Сохранённый настроенный агент юзера. Поля: enabled_blocks, channels, settings (включая wfc_nodes) |

**Связь:** `AGENT_BLOCKS[i].id === AGENT_REGISTRY[i].id` (строковый ключ `smm`, `lawyer`…).

## Структура UI-вкладок

```
┌─────────────────────────────────────────────────────────┐
│ [Библиотека] [Мои агенты] [Конструктор]                │ ← 3 таба
└─────────────────────────────────────────────────────────┘
```

**«Запустить» вкладки больше нет** — убрал. «Тест» теперь = модалка из «Мои агенты».

### Библиотека
- Карточки из `AGENT_BLOCKS`
- Клик по карточке → wizard (4 шага: Канал → Оркестратор → Блоки → Review)
- В wizard на шаге 4: **2 кнопки** — «💾 Сохранить черновик» и **«🤖 Сохранить и запустить в Telegram»**

### Мои агенты
- Список `AgentConfig` юзера через `GET /agent/config`
- На карточке 4 кнопки: **▶ Тест** (модалка с разовым запуском) / **🤖 В Telegram** / **✏️ Редактировать** / **🗑 Удалить**
- Клик по тексту карточки ничего не делает (иначе был perceived bug)
- Подсказка под названием: «👇 Нажмите 🤖 В Telegram чтобы бот начал отвечать» / «⚠️ Агент пустой» / etc.
- Если бот уже задеплоен в TG — рядом с именем кликабельный **@username**

### Конструктор
- Полноэкранный canvas (position:fixed, z-index:40)
- Drag-n-drop из sidebar на canvas
- Блоки: `wfc.nodes = [{id, type, x, y, w, cfg}]`, `wfc.edges = [{from, to}]`
- 35+ типов блоков в `WFC_DEFS` (строка 1791 в agents.html)
- Top-bar кнопки: Undo/Redo / Шаблоны / Import / Export / Очистить / **▶ Тест** / **🤖 Запустить в Telegram**
- Ctrl+Z/Y, Ctrl+D (дублировать), Del (удалить)
- Auto-resize textarea внутри блоков, custom resize-handle в углу (оранжевая лесенка)

## Что происходит при «▶ Тест» (модалка)

1. Открывается `#testAgentModal`
2. Юзер вводит goal → POST `/agent/run` с `agent_config_id`
3. Получаем `task_id`
4. Polling `/agent/{task_id}/status` каждые 1.5 сек
5. Результат + cost CH в модалке

## Что происходит при «🤖 В Telegram» (deploy)

Функция `deployAgentAsBot(agentId)` в agents.html. Логика:

```
Читаем AgentConfig:
  ├─ есть wfc_nodes в settings
  │    → используем граф как есть, подставляем tg_token если пуст
  ├─ есть enabled_blocks (из wizard)
  │    → синтезируем граф:
  │       trigger_tg → orchestrator → [agent_X for X in blocks] → output_tg
  └─ совсем пусто
       → prompt «Создать простого GPT-бота?»
       → минимальный граф trigger_tg → node_gpt → output_tg

POST /chatbots с workflow_json + tg_token
  ← Backend auto-deploy webhook (в _auto_setup_channels)
  ← Backend вызывает TG getMe → возвращает setup.telegram.username

Показываем красивую success-модалку с @username + ссылкой t.me
  ├─ если @username есть → showDeploySuccess(name, username, url)
  └─ иначе alert
```

## Исполнение воркфлоу (chatbot_engine.py)

### Entry point: `handle_message(bot, chat_id, user_text, platform, user_name)`

Вызывается из `routes/webhook.py` при входящем сообщении (`/webhook/tg/{id}`).

```python
async def handle_message(bot, chat_id, user_text, platform, user_name):
    if not _check_daily_limit(bot):           # replies_today < max_replies_day
        return None
    if not _owner_has_balance(bot, minimum=1): # баланс >= 1 CH
        return None
    workflow = _get_bot_workflow(bot)          # из bot.workflow_json
    usage_acc = {"input": 0, "output": 0, ...}
    if workflow:
        answer = await _execute_workflow(bot, chat_id, user_text, ..., workflow)
    else:
        answer = await _simple_reply(bot, ...)  # простой system_prompt + model
    if answer:
        _save_for_summary(...)
        _deduct_bot_usage(bot, usage_acc)      # списание по real токенам
        _increment_replies(bot)
    return answer  # webhook.py: if answer → send_telegram(answer) else send fallback
```

### `_execute_workflow`

1. Topo-sort графа
2. Проход по порядку: для каждой ноды → `_execute_node(node, input_text, ctx)`
3. `ctx["results"][nid] = output`
4. **Важно!** После orchestrator-ноды проверяем `ctx["orchestrator_choice"]` и добавляем неактивные ветки в `skipped_by_routing`
5. Финальный ответ: `ctx["final_output"] or ctx["results"].get(order[-1], "")`

### `_execute_node` — длинный match-statement по ntype

Обрабатывает 35+ типов нод. Самые важные:

- **trigger_*** — просто `return input_text`, в `ctx` могут быть доп-поля (is_voice, file_id)
- **node_gpt/claude/gemini/grok** — вызов `generate_response(model_alias, messages)`. system_prompt из cfg.
- **orchestrator** — если `downstream <= 1`: auto-select единственный, return input_text. Иначе — LLM classifier → JSON с `chosen_id`.
- **output_tg** — ставит `ctx["final_output"] = input_text`. Шлёт в другой chat только если `cfg.tg_chat_id != ctx.chat_id` (иначе webhook сам шлёт).
- **http_request** — с SSRF-защитой (_ssrf_validate блокирует private IP).
- **agent_*** — динамическая обёртка над `AGENT_REGISTRY`.

### Оркестратор-нода (важнейший кусок)

```python
if ntype == "orchestrator":
    edges = ctx.get("_edges", [])
    downstream_ids = [e["to"] for e in edges if e["from"] == my_id]
    downstream_nodes = [nodes_map.get(nid) for nid in downstream_ids if nodes_map.get(nid)]

    # Single downstream — auto-select (иначе _execute_workflow отключит все ветки!)
    if len(downstream_nodes) <= 1:
        if downstream_nodes:
            ctx["orchestrator_choice"] = downstream_nodes[0].get("id")
        return input_text

    # LLM classifier
    classifier_prompt = f"Варианты:\n{options_text}\n\nЗАПРОС: {input_text[:500]}\n..."
    result = generate_response(model_alias, [...])
    data = json.loads(re.search(r'\{[^}]+\}', result["content"]).group())
    ctx["orchestrator_choice"] = data["chosen_id"]
```

### ⚠️ Тонкий момент в _execute_workflow

После `_execute_node(orchestrator)` код отключает неактивные ветки:

```python
if node.get("type") == "orchestrator":
    chosen = ctx.get("orchestrator_choice")
    if chosen:  # ← ВАЖНО: только если установлен!
        for branch_id in all_downstream:
            if branch_id != chosen:
                _collect_downstream(branch_id, edges, skipped_by_routing)
                skipped_by_routing.add(branch_id)
    # Иначе не скипаем (safe fallback)
```

**Баг был**: раньше скипали ВСЕГДА при type=orchestrator. `chosen=None → все ветки != None → все отключались`. Бот молчал. Исправлено коммитом `762c5c4`.

## AGENT_REGISTRY / ReAct-loop (agent_runner.py)

Второй путь (не через bots) — разовый запуск агента через `POST /agent/run` с `goal`:

1. `deduct_strict(user_id, 100 CH)` (service mode) или 5 CH (own-key mode)
2. `create_task(user_id, goal, context)` → кладём в `agent_tasks` dict
3. `await submit_task(task_id, goal, context)` → `asyncio.PriorityQueue`
4. Background worker (`_worker` loop):
   - `agent_id = await orch.classify(goal)` — keyword-match ИЛИ LLM-fallback → "react" если ничего
   - `agent_def = AGENT_REGISTRY[agent_id]`
   - ReAct-цикл: LLM думает → зовёт tool (web_search/run_llm/send_tg/…) → observe → до finish или max_steps
5. `agent_tasks[task_id]["status"] = "done"`, `"result"` = финальный текст

## Keyword-match (orchestrator classify)

**Баг** был — substring match: `"фер"` (ФЕР-сметы) матчил «оферте» → estimator. Фикс в `agent_runner.py::classify`:

```python
# Left word-boundary через regex (морфология РАБОТАЕТ: "юрист" матчит "юриста/юристу")
return re.search(rf'(?<![\wа-яёА-ЯЁ]){re.escape(kw)}', goal_lower) is not None
```

Оставшаяся проблема: keywords `смета` не матчит «смету» — нужно писать `смет`. Чистка keywords в `server/agents/registry.py` — TODO.
