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
| `main.py` | Точка входа — FastAPI, все эндпоинты |
| `ai.py` | Логика вызова AI-провайдеров |
| `auth.py` | JWT-токены, хэширование паролей, коды верификации |
| `db.py` | SQLAlchemy engine и Session |
| `models.py` | ORM-модели |
| `payments.py` | Логика ЮKassa |
| `security.py` | Rate limiting, валидация |
| `agent_runner.py` | AI-агенты с очередью задач |
| `email_service.py` | Отправка email |
| `index.html` | Главная страница (чат) |
| `admin.html` | Админ-панель |
| `agents.html` | AI-агенты |
| `chatbots.html` | Чат-боты |
| `workflows.html` | Воркфлоу |
| `workflow.html` | Редактирование воркфлоу |

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
