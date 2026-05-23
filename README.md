# AI Студия Че

Веб-платформа для работы с AI: мультимодельный чат, ИИ-агенты с инструментами, чат-боты для мессенджеров, генерация сайтов и презентаций, биллинг через ЮKassa.

**Стек:** FastAPI · SQLAlchemy · SQLite/PostgreSQL · Pure HTML/JS (без фреймворков)

---

## 🚀 Быстрый старт

### 1. Установка
```bash
git clone <repo>
cd ai-service
python -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Конфиг — `.env`
```bash
# AI ключи (можно один — остальные просто не будут работать)
OPENAI_API_KEYS=sk-proj-...,sk-proj-...      # через запятую
ANTHROPIC_API_KEYS=sk-ant-...
ANTHROPIC_BASE_URL=                          # опц., если используете прокси
PERPLEXITY_API_KEYS=pplx-...
GROK_API_KEYS=xai-...
NANO_API_KEYS=AIza...                        # Google Imagen
KLING_API_KEYS=ak_xxx,sk_yyy                 # формат: access,secret
VEO_API_KEYS=AIza...

# Платежи (ЮKassa)
YOOKASSA_SHOP_ID=
YOOKASSA_SECRET_KEY=

# Auth
JWT_SECRET=                                  # если пусто — сгенерируется и сохранится в server/.jwt_secret
ENCRYPTION_KEY=                              # для шифрования юзерских API-ключей
ADMIN_EMAILS=admin@example.com               # через запятую
COOKIE_SECURE=0                              # 1 на проде с HTTPS

# Email (SMTP)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@example.com
SMTP_PASS=app_password
SMTP_FROM=AI Студия <noreply@example.com>
APP_URL=https://your-domain.com

# Публичный URL для Telegram webhook'ов
PUBLIC_APP_URL=https://your-domain.com       # без / в конце

# CORS
ALLOWED_ORIGINS=https://your-domain.com      # на проде указывайте конкретные домены

# DB (опц.) — по умолчанию SQLite
# DATABASE_URL=postgresql+psycopg://user:pass@host:5432/dbname

# Тесты
DISABLE_RATE_LIMIT=1                         # только для CI
```

### 3. Запуск
```bash
uvicorn main:app --reload                    # dev
uvicorn main:app --host 0.0.0.0 --port 8000  # prod
```

Открой `http://localhost:8000` — главная страница.

### 4. Тесты
```bash
pytest                                        # 56 тестов
```

---

## 🧠 Концепции

### Чат-бот vs ИИ-агент — это разные вещи

|  | Чат-бот | ИИ-агент |
|---|---|---|
| **Природа** | Реактивный | Проактивный |
| **Триггер** | Сообщение клиента | Задача от юзера |
| **Где живёт** | TG / VK / MAX / Avito | На платформе |
| **Делает** | LLM-ответ на каждое сообщение | Цепочку шагов с инструментами (web_search, generate_image…) |
| **Контекст** | История диалога | Контекст одной задачи |
| **Пример** | Бот техподдержки магазина | «Найди новости → напиши сводку → опубликуй в TG-канал» |
| **Стоимость** | ~5 CH за сообщение | 50 CH (сервис) или 5 CH (свой ключ) |
| **БД** | `UserBot`, `BotMessage` | `AgentTask`, `AgentConfig` |
| **UI-страница** | `/chatbots.html` | `/agents.html` |

### Внутренняя валюта — CH
1 CH ≈ 0.10 ₽ (настраивается). Цены моделей:

| Модель | CH |
|---|---|
| GPT-4o mini | 50 |
| GPT-4o | 100 |
| Claude Haiku | 40 |
| Claude Sonnet 4.6 | 120 |
| Claude Opus 4.6 | 300 |
| Perplexity Small | 30 |
| Perplexity Large | 80 |
| Grok 3 mini | 30 |
| Grok 3 | 80 |
| GPT Image low/medium/high | 20 / 76 / 300 |
| DALL-E 3 | 72 |
| Kling v1 / v1.5 | 500 / 800 |
| Veo 3 | 600 |
| Whisper | 15/мин |
| TTS | 3 / 1000 chars |

---

## 🗂 Структура проекта

```
ai-service/
├── main.py                          # FastAPI entry point
├── requirements.txt
├── pytest.ini
├── alembic/                         # миграции БД
├── server/
│   ├── db.py                        # engine (SQLite + Postgres support)
│   ├── models.py                    # 30+ ORM моделей
│   ├── ai.py                        # provider integrations (OpenAI/Anthropic/...)
│   ├── auth.py                      # JWT + bcrypt
│   ├── security.py                  # rate limit + SSRF + admin check
│   ├── crypto.py                    # Fernet шифрование юзерских API-ключей
│   ├── payments.py                  # ЮKassa
│   ├── email_service.py             # SMTP
│   ├── agent_runner.py              # ReAct loop + Orchestrator + Queue
│   ├── agents/registry.py           # 37 специализированных агентов
│   └── routes/
│       ├── auth.py                  # /auth/* (login/register/verify/refresh/cookies)
│       ├── chat.py                  # /message, /chat/*
│       ├── agent.py                 # /agent/run, /agent/{id}/status, WS
│       ├── tg_bots.py               # /tg/bots, /tg/webhook/{secret}
│       ├── admin.py                 # /admin/* (users, pricing, promos, faq…)
│       ├── payments.py              # /payment/*
│       ├── solutions.py             # /solutions/* (готовые AI-воркфлоу)
│       ├── sites.py                 # /sites/* (генератор сайтов)
│       ├── presentations.py         # /presentations/*
│       ├── analytics.py             # /analytics/* (графики, топы)
│       ├── voice.py                 # /voice/transcribe, /voice/speak
│       ├── webhooks.py              # /webhooks/* (HMAC-подписи, SSRF-защита)
│       ├── pipelines.py             # /pipelines/* (последовательные цепочки агентов)
│       ├── memory.py                # /memory/* (KV для агентов)
│       ├── ratings.py               # /ratings/*
│       ├── templates.py             # /templates/* (шаблоны промптов)
│       ├── presets.py               # /presets/* (workflow presets)
│       ├── sharing.py               # /sharing/* (общий доступ к ресурсам)
│       ├── org.py                   # /org/* (организации)
│       ├── subscriptions.py         # /subscriptions/* (recurring)
│       ├── telegram.py              # /auth/telegram/webapp (TG WebApp auth)
│       ├── user.py                  # /user/cabinet/stats
│       ├── user_apikeys.py          # /user/api-keys/*
│       └── public.py                # /pricing, /faq, /features (без auth)
├── views/
│   ├── index.html                   # главный SPA-чат
│   ├── chatbots.html                # CRUD TG-ботов с автоматическим setWebhook
│   ├── agents.html                  # 37 агентов + canvas-конструктор
│   ├── admin.html                   # админка
│   ├── sites.html                   # генератор сайтов
│   ├── presentations.html           # генератор КП
│   ├── analytics.html, templates.html, pipelines.html, org.html, etc.
│   └── js/
│       ├── utils.js                 # общие утилиты (api/apiPost, modals, focus-trap)
│       └── auth-core.js             # authFetchMe / authLogout / authStoreToken
└── tests/
    ├── conftest.py                  # in-memory SQLite + TestClient
    ├── test_auth.py                 # JWT, refresh, banned users
    ├── test_balance.py              # race condition (atomic UPDATE)
    ├── test_admin.py                # пагинация, adjust-balance safety
    ├── test_ssrf.py                 # 9 типов вредоносных URL заблокированы
    ├── test_cookies.py              # httpOnly cookies + logout
    ├── test_tg_bots.py              # CRUD + webhook receiver
    └── test_accessibility.py        # role=dialog, aria-modal на модалках
```

---

## 🔐 Безопасность

| Защита | Где |
|---|---|
| **Атомарное списание токенов** | `UPDATE ... WHERE balance >= cost` — нет race condition |
| **JWT с типом** (access/refresh) | refresh-токен не пройдёт на `/auth/me` |
| **httpOnly cookies** + Bearer | dual-mode: куки для браузера, токен для API |
| **bcrypt** напрямую (без passlib) | Python 3.14 совместимость |
| **Rate limit** | persist в JSON, по user_id из JWT, fallback IP |
| **SSRF-защита** | webhook URL и `browse_url` блокируют 127.0.0.1 / 192.168.x / 169.254.169.254 / file:// / gopher:// |
| **HMAC** на ЮKassa webhook | сверка X-Content-Signature |
| **Cap ₽150k** на сумму платежа | защита от подмены |
| **Fernet-шифрование** юзерских API-ключей | в БД |
| **CORS credentials off** при `ALLOWED_ORIGINS=*` | автоматически |
| **CASCADE delete** | `cascade="all, delete-orphan"` на User → Messages/Subscriptions/Transactions |
| **Email verify** для reset-password | защита от угона аккаунта |
| **Admin balance-cap** | ±10M CH/раз, потолок 2B, проверка отрицательного |

---

## 🤖 Чат-боты (Telegram)

```
[BotFather] → бот + token
       ↓
POST /tg/bots {name, bot_token, system_prompt, model}
       ↓
POST /tg/bots/{id}/activate  → setWebhook к Telegram
       ↓
Telegram → POST /tg/webhook/{secret}
       ↓
   AI-ответ (model + system_prompt + история)
       ↓
   sendMessage → клиент видит ответ в TG
```

- Командa `/start`, `/help` → приветствие
- Контекст: 10 последних сообщений в памяти
- Atomic deduct + refund при сбое AI
- Дневной лимит сообщений (сбрасывается каждые 24ч)
- Markdown с fallback на plain text

**Важно для прода:** установи `PUBLIC_APP_URL=https://your-domain.com` — Telegram должен достать наш webhook по HTTPS.

---

## 🎯 ИИ-агенты

37 специалистов в библиотеке: SMM, copywriter, lawyer, accountant, kp_agent, tender_parser, estimator, translator…

```
POST /agent/run {goal: "Найди новости об ИИ", api_mode: "service"}
       ↓
   create_task(...) → AgentTask
       ↓
   Orchestrator.classify(goal) → выбор агента
       ↓
   ReAct loop: (думаю → действую → наблюдаю)*
       ↓ инструменты:
   web_search · browse_url · run_llm · generate_image · generate_video ·
   send_tg_message · send_vk_post · write_output · delegate · finish ·
   remember · recall
       ↓
WebSocket /agent/{task_id}/ws (real-time шаги)
       ↓
   markdown-результат + 👍/👌/👎 рейтинг
```

**Канвас-конструктор** в `/agents.html` — для воркфлоу с триггерами `manual / webhook / cron`. Каналы (TG/VK) живут в чат-ботах, не здесь.

---

## 💳 Биллинг

- ЮKassa redirect-flow + webhook с HMAC
- Тарифы: Старт 590₽ / Про 1590₽ / Ультра 4590₽ (1k/3k/9k CH)
- Пакеты докупки: 600₽/1150₽/2700₽
- Реферал: +10% с первой оплаты приглашённого (защита от абуза — только 1 раз)
- Промокоды: discount или bonus_tokens; работают и на тарифы, и на пакеты

---

## 🛠 API endpoints (выборочно)

```
# Auth
POST   /auth/register                    регистрация (требует agreed_to_terms)
POST   /auth/verify-email                подтверждение email + 5000 CH бонус
POST   /auth/login                       → {token, refresh_token, user} + cookies
POST   /auth/refresh                     ротация токенов (cookie или body)
POST   /auth/logout                      очистка cookies
GET    /auth/me                          текущий пользователь

# Chat
POST   /chat/create                      → {chat_id}
POST   /message                          атомарное списание + AI + refund при сбое
GET    /chat/{chat_id}                   история
POST   /upload                           загрузка изображения

# AI Agents
POST   /agent/run                        запустить задачу
GET    /agent/{task_id}/status           статус + шаги
WS     /agent/{task_id}/ws               real-time stream

# Telegram bots
GET    /tg/bots                          список
POST   /tg/bots                          создать
POST   /tg/bots/{id}/activate            setWebhook
POST   /tg/bots/{id}/deactivate          deleteWebhook
GET    /tg/bots/{id}/info                диагностика + Telegram getWebhookInfo
POST   /tg/webhook/{secret}              приёмник Telegram updates

# Admin (paginated)
GET    /admin/users?offset=&limit=
GET    /admin/users/full?offset=&limit=
POST   /admin/users/{id}/adjust-balance  с safety caps
POST   /admin/users/{id}/toggle-ban
GET    /admin/support-requests?status=
GET    /admin/promos
PUT    /admin/pricing/models/{model_id}

# Public (без auth)
GET    /features                         feature flags
GET    /pricing/models                   стоимости моделей
GET    /pricing/packages                 пакеты докупки
GET    /pricing/exchange-rate            USD/RUB через ЦБ РФ
GET    /faq

# Health
GET    /health                           {status, db, version}
```

---

## ♿ Accessibility

- 13 модалок имеют `role="dialog"` + `aria-modal="true"`
- Focus trap (Tab/Shift+Tab внутри модалки)
- Esc закрывает верхнюю модалку
- Focus restoration после закрытия
- aria-label на всех иконочных кнопках

---

## 🚀 Прод-чеклист

- [ ] `JWT_SECRET` — не дефолтный (в env, постоянный)
- [ ] `ENCRYPTION_KEY` — не дефолтный (Fernet 32 байта в base64url)
- [ ] `COOKIE_SECURE=1` (HTTPS-only cookies)
- [ ] `ALLOWED_ORIGINS` — конкретные домены (НЕ `*`)
- [ ] `PUBLIC_APP_URL=https://your-domain.com`
- [ ] `ADMIN_EMAILS` — реальные email админов
- [ ] `DEPLOY_TOKEN` — секрет для `/internal/deploy`
- [ ] `YOOKASSA_*` — продакшн-ключи + сверь HMAC
- [ ] PostgreSQL вместо SQLite (`DATABASE_URL=postgresql+psycopg://...`)
- [ ] Reverse proxy (nginx/caddy) с HTTPS
- [ ] Бэкапы `chat.db` (или PG dump)
- [ ] systemd unit или supervisor для uvicorn
- [ ] Мониторинг `/health`

---

## 📜 Лицензия

Внутренний проект. Все права защищены.
