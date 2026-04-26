# AI Студия Че — CLAUDE.md

Этот файл задаёт контекст для сессий. Редактируй его по мере развития проекта.

## Что делать при каждом новом запуске
- Прочитай этот файл, чтобы вспомнить проект
- Прочитай файлы из `memory/` (особенно `project_state.md`, `feedback_business_focus.md`)
- Проверь `git log --oneline -15` для понимания последних изменений
- Прочитай `TODO_NEXT.md` в корне — что в работе

## Общее описание
**B2B AI-платформа для бизнеса.** Веб-приложение FastAPI + HTML SPA.
Главные инструменты:
- **Чат** с моделями: GPT-4o / Claude Sonnet / Perplexity / Grok / GPT-image (картинки)
- **Бизнес-решения** (Solutions) — 30 экспертных промптов с фикс-ценой
  50/100 ₽, выдача в виде PDF-отчёта
- **Чат-боты** для TG / VK / Avito / **MAX** с конструктором workflow
  (триггеры → AI-блоки → outputs)
- **AI-агенты** с очередью задач + AI-сборка графа по описанию
- **Сайты** под бизнес — фикс 1500 ₽, чат по ТЗ через GPT, генерация HTML
  через Claude, картинки upload, привязка чат-бота как виджета
- **КП и Презентации** — фикс 50/100 ₽, картинки, привязка чат-бота
- **Платежи** через ЮKassa (только пополнение баланса, без подписок)
- **Админка**: пользователи, API-ключи, цены, контент, фичи, промокоды
- **Рефералка**: 10% от пополнения друга на свой баланс

## Стек
- **Backend:** Python 3.12, FastAPI, SQLAlchemy, SQLite (`chat.db`)
- **Frontend:** HTML / Tailwind CDN / vanilla JS (SPA без фреймворка)
- **AI провайдеры:** OpenAI, Anthropic, Perplexity, Grok (Gemini/Kling/Veo/Nano скрыты)
- **Платежи:** ЮKassa (yookassa SDK)
- **PDF:** xhtml2pdf (markdown → HTML → PDF) с фирменным CSS
- **Авторизация:** JWT через python-jose, bcrypt через passlib

## Структура файлов
| Файл | Что делает |
|---|---|
| `main.py` | Точка входа — FastAPI, подключение роутеров, CSP |
| `server/routes/*.py` | HTTP-эндпоинты (auth, chat, payments, admin, oauth, sites, presentations, chatbots, agent, solutions, webhook, widget, public, user) |
| `server/ai.py` | Логика вызова AI-провайдеров, MODEL_REGISTRY, openai_image_response с edit-by-reference |
| `server/auth.py` | JWT-токены (с iss/aud), хэширование паролей |
| `server/db.py` | SQLAlchemy engine, SessionLocal, db_session, **LIGHTWEIGHT_MIGRATIONS** |
| `server/billing.py` | **Атомарные** списания/начисления (deduct_atomic, deduct_strict, credit_atomic). Все суммы — в КОПЕЙКАХ |
| `server/secrets_crypto.py` | Шифрование секретов в БД (Fernet от JWT_SECRET) + EncryptedString TypeDecorator |
| `server/models.py` | ORM-модели. Токены ботов (TG/VK/Avito/MAX/widget) — EncryptedString |
| `server/payments.py` | YooKassa init + credit_referral_bonus (без PLANS — подписки убраны) |
| `server/security.py` | Rate limiting, валидация, tg_webhook_secret, require_admin |
| `server/agent_runner.py` | AI-агенты с приоритетной очередью |
| `server/chatbot_engine.py` | Движок чат-ботов (исполнение workflow), MAX/TG/VK/Avito helpers |
| `server/workflow_builder.py` | AI-сборка графа воркфлоу по описанию задачи |
| `server/pdf_builder.py` | Markdown → PDF для бизнес-решений (xhtml2pdf + фирменный CSS) |
| `server/email_service.py` | Отправка email |
| `server/email_imap.py` | IMAP-trigger для воркфлоу |
| `views/index.html` | Главная страница (чат + бизнес-решения + lightbox + viewport) |
| `views/admin.html` | Админ-панель |
| `views/agents.html` | AI-агенты + Canvas-воркфлоу + AI-сборка |
| `views/chatbots.html` | Чат-боты CRUD (TG/VK/Avito/MAX/виджет) |
| `views/workflows.html`, `workflow.html` | Воркфлоу-модуль (СКРЫТ из навигации) |
| `views/sites.html` | Конструктор сайтов с превью + viewport-resize + ботом |
| `views/presentations.html` | Презентации/КП с картинками + ботом |

## Запуск
```
uvicorn main:app --reload
```

## Деньги — РУБЛИ + КОПЕЙКИ (после рефакторинга 2026-04-25)
- Баланс юзера = `User.tokens_balance` в **копейках** (1 ₽ = 100 коп)
- Токены CH полностью **убраны** из UX. В коде имена колонок остались
  (`tokens_balance`, `tokens_delta`, `ch_per_1k_*`) для совместимости
  с существующей БД, но семантика — копейки.
- UI: `window.fmtRub(kop)` → "X.XX ₽" (определён в каждом view).
- Подписки **отключены**: убраны PLANS, /payment/create, /payment/confirm,
  cancel_subscription, _sub_dict, вкладка «Подписки» в кабинете.
- Только пополнение баланса через `/payment/buy-tokens` (название
  legacy, по факту это «buy-rubles»).

## AI-провайдеры и цены
- OpenAI / Anthropic / Grok — прямые ключи, без прокси.
- Gemini / Kling / Veo / Nano — модели УБРАНЫ из главного экрана.
- Картинки — gpt-image-1 (новая) + DALL-E 3 (legacy). 15 ₽ / шт.

## Правила разработки
- Ответы на русском языке
- Комментарии в коде — минимальные, только где неочевидно
- API-ключи хранятся в БД (`api_keys`) и восстанавливаются в env при старте
  через `_load_all_apikeys_from_db`. Фильтр `status != "disabled"`
- `loadChats()` вызывать без `await` чтобы не блокировать UI
- **Биллинг:** любые изменения баланса — только через `server.billing.deduct_strict/deduct_atomic/credit_atomic`. Прямой `user.tokens_balance += …` запрещён (race condition). Все суммы — копейки
- **Сессии БД вне FastAPI Depends:** только через `with db_session() as db:` (rollback при ошибке)
- **Секреты в БД** (IMAP-пароли, токены чат-ботов): через `EncryptedString` TypeDecorator или вручную `secrets_crypto.encrypt/decrypt`
- **Миграции схемы:** добавлять колонки через `LIGHTWEIGHT_MIGRATIONS` в `server/db.py` (идемпотентный ALTER TABLE IF NOT EXISTS)
- **Webhooks:** TG проверяется `X-Telegram-Bot-Api-Secret-Token`; ЮKassa — HMAC обязателен (raw_body читается ДО json) + двойная верификация через `Payment.find_one`
- **Чат-доступ:** `_assert_chat_owner` без `or_(user_id IS NULL)` — анон-чаты не утекают
- **Картинки** сохраняются в `/uploads/` (КОРЕНЬ проекта), не в `server/uploads`. Используй `os.path.dirname(_BASE_DIR)` для перехода на parent
- **Цены AI** — в копейках в `model_pricing` БД и `TOKEN_COST` (server/ai.py) как fallback
- **Деплой:** `git pull origin main && systemctl restart ai-che`. NEVER `db.drop_all()`, NEVER reset api_keys/users/transactions
