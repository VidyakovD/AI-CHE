# AI Студия Че — CLAUDE.md

Этот файл задаёт контекст для сессий. Редактируй его по мере развития проекта.

## Что делать при каждом новом запуске
- Прочитай этот файл, чтобы вспомнить проект
- Прочитай файлы из `memory/` если есть
- Проверь `git log --oneline -10` для понимания последних изменений

## Общее описание
Веб-платформа (FastAPI + HTML) с мультипровайдерным AI-ассистентом:
- Чат с несколькими AI-моделями (GPT, Claude, Gemini, Perplexity, Grok)
- Генерация видео (Kling, Veo) и изображений (DALL-E)
- AI-агенты с пошаговым выполнением
- Готовые решения (Solutions) — конструктор AI-воркфлоу
- Платежи через ЮKassa (подписки + докупка токенов)
- Админка (пользователи, API-ключи, цены, контента, фичи, промокоды)
- Реферальная система, промокоды, курс валют (ЦБ РФ)

## Стек
- **Backend:** Python 3.x, FastAPI, SQLAlchemy, SQLite (`chat.db`)
- **Frontend:** Чистые HTML/CSS/JS (SPA без фреймворка)
- **AI провайдеры:** OpenAI, Anthropic, Gemini, Perplexity, Grok, Kling, Veo, Nano
- **Платежи:** ЮKassa (yookassa)
- **Авторизация:** JWT через python-jose, bcrypt через passlib

## Структура файлов
| Файл | Что делает |
|---|---|
| `main.py` | Точка входа — FastAPI, подключение роутеров |
| `server/routes/*.py` | HTTP-эндпоинты (auth, chat, payments, admin, oauth, sites, …) |
| `server/ai.py` | Логика вызова AI-провайдеров |
| `server/auth.py` | JWT-токены (с iss/aud), хэширование паролей |
| `server/db.py` | SQLAlchemy engine, SessionLocal, db_session, lightweight миграции |
| `server/billing.py` | **Атомарные** списания/начисления CH (deduct_atomic, deduct_strict, credit_atomic) |
| `server/secrets_crypto.py` | Шифрование секретов в БД (Fernet, ключ из JWT_SECRET) |
| `server/models.py` | ORM-модели |
| `server/payments.py` | Логика ЮKassa |
| `server/security.py` | Rate limiting, валидация, tg_webhook_secret, require_admin |
| `server/agent_runner.py` | AI-агенты с очередью задач |
| `server/chatbot_engine.py` | Движок чат-ботов (исполнение workflow) |
| `server/email_service.py` | Отправка email |
| `server/email_imap.py` | IMAP-trigger для воркфлоу |
| `views/index.html` | Главная страница (чат) |
| `views/admin.html` | Админ-панель |
| `views/agents.html` | AI-агенты |
| `views/chatbots.html` | Чат-боты |
| `views/workflows.html` | Воркфлоу (список) |
| `views/workflow.html` | Редактирование воркфлоу |
| `views/sites.html` | Конструктор сайтов |
| `views/presentations.html` | Презентации/КП |

## Запуск
```
uvicorn main:app --reload
```

## Внутренняя валюта
Токены CH (Che). Цена запроса зависит от модели и курса USD/RUB.

## Модели
GPT / Claude / Gemini / Perplexity / Grok — текст; DALL-E — картинки; Kling / Veo — видео

## Правила разработки
- Ответы на русском языке
- Комментарии в коде — минимальные, только где неочевидно
- API ключи хранятся в БД и восстанавливаются в env при старте (`_load_all_apikeys_from_db`)
- `loadChats()` вызывать без `await` чтобы не блокировать UI
- **Биллинг:** любые изменения CH-баланса — только через `server.billing.deduct_strict/deduct_atomic/credit_atomic`. Прямой `user.tokens_balance += ...` запрещён (race condition).
- **Сессии БД вне FastAPI Depends:** только через `with db_session() as db:` (rollback при ошибке).
- **Секреты в БД** (IMAP пароли и т.п.): шифруем через `server.secrets_crypto.encrypt`, читаем через `decrypt`.
- **Миграции схемы:** добавлять новые колонки через `LIGHTWEIGHT_MIGRATIONS` в `server/db.py` (идемпотентный ALTER TABLE).
- **Webhooks:** TG проверяется `X-Telegram-Bot-Api-Secret-Token` (см. `tg_webhook_secret`); ЮKassa — HMAC + двойная верификация через `Payment.find_one`.
