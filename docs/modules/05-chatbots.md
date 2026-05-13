# Модуль 05 — Chatbots

> **Что это:** многоканальные чат-боты юзеров: TG / VK / Avito / MAX / WhatsApp (Wazzup24) / Widget на сайт. 7 шаблонов + 40+ типов узлов в workflow + AI-конструктор + AI-доработка + RAG + прайс-листы. Open когда: работаешь с ботом юзера, добавляешь канал, чинишь workflow-нод, дебажишь webhook.

## TL;DR

- **Главный engine:** [server/chatbot_engine.py](server/chatbot_engine.py) (~2400 строк после декомпозиции) — workflow DAG executor, ноды, send-helpers.
- **Декомпозированные части (`56ce8d6`):**
  - [server/messaging/senders.py](server/messaging/senders.py) — `send_telegram`, `send_vk`, `send_max`, `send_whatsapp`, `send_avito`, `send_widget`
  - [server/messaging/voice.py](server/messaging/voice.py) — Whisper + TTS для чатбота
  - [server/sandbox.py](server/sandbox.py) — code_python sandbox
- **Routes:** [server/routes/chatbots.py](server/routes/chatbots.py) + [server/routes/webhook.py](server/routes/webhook.py) (incoming TG/VK/MAX/WA/Avito) + [server/routes/widget.py](server/routes/widget.py).
- **UI:** [views/chatbots.html](views/chatbots.html) (2232 строки) + [views/marketplace.html](views/marketplace.html) для публикации.
- **Шаблоны:** [server/bot_templates.py](server/bot_templates.py) — 7 готовых (магазин, бронь, ремонт, и т.д.).
- **TG mgmt-бот:** [server/tg_management.py](server/tg_management.py) — управление аккаунтом через TG.

## 6 каналов

| Канал | Webhook | Особенности |
|---|---|---|
| **Telegram** | `/webhook/tg/{secret}` | secret-token в URL; основной канал |
| **VK** | `/webhook/vk/{secret}` | `vk_secret` + `compare_digest`; long polling fallback |
| **Avito** | `/webhook/avito/{secret}` | OAuth client_credentials, polling |
| **MAX** | `/webhook/max/{secret}` | российский мессенджер, не путать с провайдером Anthropic MAX |
| **WhatsApp (Wazzup24)** | `/webhook/wazzup/{secret}` | через шлюз Wazzup24 |
| **Widget** | WebSocket `/ws/widget/{bot_id}` | embedded чат на сайт, Origin-whitelist |

## Workflow — ноды (40+ типов)

Граф `nodes + edges` в `Chatbot.workflow_json`. Главные категории:

| Категория | Ноды |
|---|---|
| **Триггеры** | trigger_message, trigger_command, trigger_schedule, trigger_button |
| **LLM** | llm_chat, llm_classify, llm_extract, web_search |
| **Каналы** | send_message, send_image, send_file, send_audio, send_button |
| **Логика** | branch_condition, switch_value, delay, loop |
| **Данные** | save_variable, http_request, code_python (sandbox) |
| **Knowledge** | rag_search, file_extract |
| **CRM/email** | send_email, schedule_followup, crm_post |
| **Голос** | tts_voice, whisper_transcribe |

Все имена — в `NODE_TYPE_LABELS` ([views/icons.js](views/icons.js)) — 90+ маппингов.

## Модели

| Таблица | Поля важные |
|---|---|
| `chatbots` | user_id, name, channel, workflow_json, is_active, channel_config (token, etc.), price_items_json |
| `bot_conversations` | bot_id, user_chat_id, state (variables), last_message_at |
| `bot_records` | bot_id, type (lead/order/booking), fields_json, created_at — то что бот собрал у клиента |
| `bot_price_items` | bot_id, name, price_kop, sku, description (semantic search) |
| `bot_marketplace_listings` / `installs` | публикация (см. [12-marketplace.md](12-marketplace.md)) |

## Endpoints (CRUD + AI)

| Метод | Endpoint | Что |
|---|---|---|
| GET | `/bots` | Список |
| POST | `/bots` | Создание (бесплатно) |
| GET | `/bots/{id}` | Детали |
| PUT | `/bots/{id}` | Update |
| DELETE | `/bots/{id}` | — |
| POST | `/bots/{id}/template` | Из шаблона (бесплатно) |
| POST | `/bots/ai-create` | **AI-конструктор** (≥1000 ₽) — Claude генерит граф |
| POST | `/bots/{id}/ai-improve` | **AI-доработка** (real × 5) |
| GET | `/bots/{id}/analytics` | Конверсии, реакции |
| POST | `/bots/{id}/publish-marketplace` | Опубликовать как шаблон |
| Webhook'и | `/webhook/tg/{secret}` и т.д. | Incoming сообщения |

## RAG + прайсы

- **RAG knowledge:** см. [11-knowledge-rag.md](11-knowledge-rag.md) — `owner_type='bot'`, embedding chunks.
- **Прайс-листы:** `bot_price_items` с semantic-search через embedding (когда клиент пишет «есть синие кроссовки?» — ищем по описанию).

## Events

При `record.created` (бот собрал лида/заказ) бот:
1. Сохраняет в `bot_records`
2. Триггерит **push** ([17-push.md](17-push.md))
3. Триггерит **CRM dispatch** ([15-crm.md](15-crm.md)) — fire-and-forget в Bitrix24/amoCRM/webhook
4. Триггерит **public API webhook** `record.created` ([13-public-api.md](13-public-api.md))

## Безопасность

- ✅ **TG webhook secret-token** валидация
- ✅ **VK secret + compare_digest** на webhook
- ✅ **Widget WS Origin-whitelist** (только разрешённые домены юзера)
- ✅ **http_request нода:** двойной DNS + CIDR блок-лист (SSRF)
- ✅ **code_python sandbox** — restricted builtins, no import os
- ✅ **bleach** на исходящих сообщениях если HTML

## AI-конструктор бота — анти-паттерны

[server/workflow_builder.py](server/workflow_builder.py) — System-prompt содержит **анти-паттерны** + автоочистку orphan-нод (`6f590f0`). После `0dd0642` — **один триггер на граф** (если юзер хочет 3 канала — это 3 отдельных бота, не один с мульти-триггером).

## Гочча

- **`/iterate` сайта ≠ `/ai-improve` бота** — разные endpoints, не путать.
- **Marketplace отключён в проде** (см. [12-marketplace.md](12-marketplace.md)) — но код botов работает.
- **Voice в боте билит через chatbot-канал**, не через chat. Отдельная биллинг-логика.

## Тесты

- `tests/test_api.py::TestChatbots`
- `tests/test_crm.py` — record.created → CRM dispatch

## Зависимости

- [03-ai-core](03-ai-core.md) — workflow_builder + LLM-ноды
- [11-knowledge-rag](11-knowledge-rag.md) — rag_search нода
- [15-crm](15-crm.md) — record.created → CRM
- [17-push](17-push.md) — push при record.created
- [13-public-api](13-public-api.md) — webhook record.created
- [12-marketplace](12-marketplace.md) — публикация шаблона
