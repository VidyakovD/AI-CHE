# 02. Архитектура

## Структура репо

```
ai-service/
├── main.py                          # FastAPI entry, middlewares (CORS, body-size, security headers, rate-limit), startup
├── chat.db                          # SQLite БД (не в git; бэкапы: chat.db.backup-*)
├── .env                             # Локальные секреты (не в git)
├── .env.example                     # Шаблон env (в git)
├── requirements.txt
├── ai-che.service                   # systemd unit template
├── Оферта.txt
├── CLAUDE.md                        # Общая инфа для Claude (в git)
├── TODO_NEXT.md                     # Список задач (в git)
│
├── server/
│   ├── ai.py                        # 46K — вызов AI-провайдеров. KEY FILE.
│   ├── chatbot_engine.py            # 70K — движок ботов: _execute_workflow, orchestrator-node, _execute_node. KEY FILE.
│   ├── agent_runner.py              # 37K — ReAct-цикл, Orchestrator-класс, Registry, queue
│   ├── auth.py                      # JWT (iss/aud), bcrypt, verify tokens
│   ├── billing.py                   # Атомарные списания CH (deduct_atomic, deduct_strict, credit_atomic)
│   ├── db.py                        # engine, SessionLocal, db_session() CM, LIGHTWEIGHT_MIGRATIONS
│   ├── email_service.py             # SMTP + шаблоны (verify, password reset, low balance, admin alert)
│   ├── email_imap.py                # IMAP-trigger для воркфлоу
│   ├── knowledge.py                 # БЗ / RAG
│   ├── models.py                    # ORM модели (User, ChatBot, AgentConfig, Solution, …)
│   ├── payments.py                  # ЮKassa интеграция, PLANS = {starter, pro, ultra}
│   ├── scheduler.py                 # trigger_schedule + apikey_check_loop + advisory-lock
│   ├── secrets_crypto.py            # Fernet шифрование IMAP-паролей, key-versioning
│   ├── security.py                  # Rate-limit (SQLite), CORS, security headers, tg_webhook_secret
│   ├── worker_lock.py               # Advisory locks для multi-worker через SQLite
│   ├── admin_audit.py               # log_admin_action helper
│   ├── agents/
│   │   └── registry.py              # 22 агента с keywords+system_prompts
│   ├── yandex_disk.py               # Я.Диск API
│   └── routes/
│       ├── auth.py                  # /auth/register, login, verify-email, reset-password, change-password/email
│       ├── oauth.py                 # /auth/oauth/{google,vk}/{start,callback,exchange}
│       ├── user.py                  # /user/cabinet/stats, /user/transactions.csv, /user/low-balance-threshold, /user/referral/stats
│       ├── chat.py                  # /message, /upload, /chat/*, /kling/status
│       ├── payments.py              # /payment/{create,confirm,webhook,buy-tokens,confirm-tokens}
│       ├── admin.py                 # /admin/* (users, apikeys, pricing, promos, audit-log, reencrypt-secrets)
│       ├── solutions.py             # /solutions/*, /solutions/runs/{id}/continue
│       ├── sites.py                 # /sites/* + /sites/hosted/{id}/* (sandbox iframe!)
│       ├── presentations.py         # /presentations/*
│       ├── agent.py                 # /agent/run, /status, /cancel, /config
│       ├── chatbots.py              # /chatbots/* (auto-deploy webhook, getMe для @username)
│       ├── webhook.py               # /webhook/{tg,vk,avito}/{bot_id}
│       ├── public.py                # /plans, /faq, /pricing/*, /promo/apply, feature flags
│       ├── widget.py                # /widget/{bot_id}.js — встраиваемый JS
│       ├── user_apikeys.py          # /user-apikeys/* (для режима «свой ключ» агентов)
│       └── deps.py                  # get_db, current_user, optional_user, _user_dict, _sub_dict, _tx_dict, _deduct helper
│
├── views/                           # HTML SPA (каждый файл — отдельная страница)
│   ├── index.html                   # 170K — главная, чат, кабинет, solutions, forgot-password
│   ├── admin.html                   # 90K — админка
│   ├── agents.html                  # 150K+ — ИИ агенты (библиотека + мои + конструктор)
│   ├── chatbots.html                # 24K — список чат-ботов
│   ├── workflows.html               # 12K — список воркфлоу (отдельный редактор)
│   ├── workflow.html                # 62K — редактор воркфлоу
│   ├── sites.html                   # 53K — конструктор сайтов через AI
│   ├── presentations.html           # 29K — КП/презентации
│   └── terms.html                   # оферта
│
├── scripts/
│   ├── seed_business_prompts.py     # Сидер 30 бизнес-промптов
│   └── update_pricing.py            # Миграция тарифов (ModelPricing, TokenPackages)
│
├── uploads/                         # Загрузки юзеров (не в git, /uploads/*)
│   └── sites/<project_id>/          # Hosted AI-сайты
│
├── docs/
│   └── connect_bot.md               # Инструкция для клиента
│
└── tests/
    └── test_api.py                  # Очень ограниченные тесты (нуждается в расширении)
```

## Модели БД (server/models.py)

| Таблица | Смысл | Ключевые поля |
|---|---|---|
| `users` | Юзеры | email, password_hash, tokens_balance (CH), is_verified, is_banned, referral_code, oauth_provider/sub, low_balance_threshold, low_balance_alerted_at |
| `verify_tokens` | Email verify / reset / oauth exchange | token, purpose, used, expires_at |
| `subscriptions` | Подписки юзеров | plan, tokens_total, tokens_used, price_rub, status, yookassa_payment_id (**UNIQUE**) |
| `transactions` | Все движения CH | type (payment/usage/bonus/refund), tokens_delta, amount_rub, description, model, yookassa_payment_id (**INDEX**) |
| `messages` | История чатов | chat_id, role, content, model, user_id, tokens_used |
| `chatbots` | Боты юзеров | user_id, name, model, system_prompt, tg/vk/avito_*, widget_*, workflow_json, status, max_replies_day, replies_today |
| `agent_configs` | Настроенные AI-агенты | user_id, name, enabled_blocks (JSON), channels (JSON), settings (JSON — содержит wfc_nodes!), status |
| `solutions`, `solution_categories`, `solution_steps`, `solution_runs` | Готовые решения |
| `api_keys` | API-ключи AI-провайдеров | provider, key_value, status (ok/error/unknown), last_error, last_check |
| `user_api_keys` | Ключи юзеров для режима «свой ключ» | user_id, provider, api_key |
| `model_pricing` | Цены AI-моделей в CH/1k tokens | model_id, ch_per_1k_input, ch_per_1k_output, cost_per_req, min_ch_per_req |
| `token_packages` | Разовые покупки | name, tokens, price_rub, is_active |
| `pricing_settings` | Глобальные настройки | key, value (напр. ch_to_rub=0.10) |
| `promo_codes`, `promo_uses` | Промокоды |
| `feature_flags` | Фичи (можно вкл/выкл) | name, enabled |
| `support_requests` | Обращения в поддержку |
| `usage_logs` | Детальная статистика токенов | user_id, model, input_tokens, output_tokens, cached_tokens, ch_charged |
| `imap_credentials` | IMAP-креды для triggers (password Fernet-шифрован) |
| `workflow_store` | K-V per-bot (для storage_get/set/push нод) |
| `knowledge_files` | Файлы БЗ |
| `company_profiles` | Профили компаний (для презентаций) |
| `site_projects`, `site_templates` | Конструктор сайтов |
| `presentation_projects`, `presentation_templates` | Презентации |
| `exchange_rates` | Курс USD/RUB с ЦБ РФ (автообновление) |
| `admin_audit_log` | Критичные действия админов |

## Миграции

**Alembic нет.** Используется lightweight через `server/db.py::LIGHTWEIGHT_MIGRATIONS`:

```python
LIGHTWEIGHT_MIGRATIONS = [
    ("users", "low_balance_threshold", "INTEGER DEFAULT 100"),
    ("users", "low_balance_alerted_at", "DATETIME"),
]

LIGHTWEIGHT_INDEXES = [
    ("uq_subscriptions_yookassa_id", "CREATE UNIQUE INDEX IF NOT EXISTS ..."),
    ("ix_transactions_yookassa_id", "CREATE INDEX IF NOT EXISTS ..."),
]
```

Применяются в main.py на старте: `apply_lightweight_migrations()`.

Добавить новую колонку → допиши в LIGHTWEIGHT_MIGRATIONS → restart → автоприменится идемпотентно.

## Роуты: кто за что

| URL prefix | Файл | Цель |
|---|---|---|
| `/auth/*` | routes/auth.py + oauth.py | Регистрация, логин, JWT, OAuth |
| `/user/*` | routes/user.py | Кабинет, stats, CSV, рефералы |
| `/message`, `/chat/*`, `/upload` | routes/chat.py | Основной чат |
| `/payment/*`, `/plans` | routes/payments.py + public.py | ЮKassa flow |
| `/admin/*` | routes/admin.py | Админка |
| `/solutions/*` | routes/solutions.py | Готовые решения |
| `/sites/*` | routes/sites.py | Конструктор сайтов (+ sandbox iframe для hosted) |
| `/presentations/*` | routes/presentations.py | КП/презентации |
| `/agent/*` | routes/agent.py | Запуск ReAct-агентов (статус, отмена) |
| `/chatbots/*` | routes/chatbots.py | CRUD ботов + auto-deploy webhook + getMe |
| `/webhook/{tg,vk,avito}/{id}` | routes/webhook.py | Входящие сообщения от мессенджеров |
| `/widget/{id}.js` | routes/widget.py | JS-виджет для встраивания на сайт |
| `/pricing/*`, `/faq`, `/features` | routes/public.py | Публичные эндпоинты |
| `/internal/deploy` | main.py | Deploy trigger (DEPLOY_TOKEN) |

## Фоновые задачи (asyncio tasks)

Стартуются в `main.py::startup()`:

1. `scheduler_loop` (server/scheduler.py) — проверка `trigger_schedule` нод каждые 30 сек + advisory-lock через `worker_lock`
2. `apikey_check_loop` (server/scheduler.py) — health-check API-ключей раз в час, email админу на сломанные
3. `imap_loop` (server/email_imap.py) — проверка IMAP каждые 60 сек + advisory-lock
4. `init_agent_queue` (server/agent_runner.py) — priority-queue для ReAct-агентов
5. Обновление курса USD/RUB от ЦБ РФ (в `startup_public`)

## Middleware chain (в main.py)

1. CORS — с fail-fast если `ALLOWED_ORIGINS` не задан
2. `rate_limit_middleware` — SQLite WAL store, работает между workers
3. `body_size_and_headers` — лимит 12 MB + security headers (HSTS/X-Frame/X-CT-Opts/Referrer/Permissions)

## Worker pool

На проде **`uvicorn --workers 2`**. Это критично:
- Rate-limit должен быть shared → SQLite (есть)
- Scheduler должен lock-аться → worker_lock.py (есть)
- In-memory кэши не шарятся между процессами (это известно)
