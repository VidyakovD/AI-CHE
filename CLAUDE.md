# AI Студия Че — CLAUDE.md

Этот файл — first-class контекст для AI-ассистента. Если зашёл в проект **в новом чате** — читай целиком, потом смотри `HANDOVER.md` (свежие изменения за последние сессии) и `TODO_NEXT.md` (что в работе).

## Что делать при каждом новом запуске
1. **Прочитай этот файл целиком** — здесь актуальное состояние (не из памяти).
2. Прочитай `HANDOVER.md` в корне — там история последних 5-10 сессий с диффом.
3. Прочитай `TODO_NEXT.md` — что в очереди.
4. Если нужны live-логи событий с прода — запроси у юзера выгрузку:
   ```
   GET https://aiche.ru/admin/actions.txt?since_hours=72&limit=2000
   Authorization: Bearer <admin token>
   ```
   (см. секцию «Audit log» ниже)
5. `git log --oneline -15` — последние коммиты.

## Краткое описание
**B2B AI-платформа для бизнеса.** Веб-приложение FastAPI + HTML SPA.
Главные инструменты:
- **Чат** с моделями: GPT-4o / Claude Sonnet+Opus / Perplexity / Grok / GPT-image / **Imagen 4** / **Veo 3** (видео)
- **Бизнес-решения** (Solutions) — 30 экспертных промптов с фикс-ценой 50/100 ₽, выдача PDF
- **Чат-боты** TG / VK / Avito / **MAX** с конструктором workflow + **6 готовых шаблонов** + **прайс-лист с semantic search** (text-embedding-3-small)
- **AI-агенты** с очередью + AI-сборка графа по описанию
- **Сайты** — фикс **1500 ₽ (Sonnet)** или **1990 ₽ (Opus премиум)**, фоновая генерация с polling, мини-браузер в превью
- **КП и Презентации** — фикс 50/100 ₽
- **Платежи** ЮKassa (только пополнение баланса, без подписок)
- **Админка**: пользователи, ключи, цены, контент, фичи, промокоды, **аудит-лог**, **pricing_config** (тарифы в БД)
- **Рефералка**: 10% от пополнения
- **Свои API-ключи юзера** (OpenAI/Claude/Gemini/Grok) — скидка 80% при использовании, fallback на наши при ошибке
- **Storage assets** (лидмагниты PDF/картинки/видео) — 50 ₽/мес за 100 МБ

## Стек
- **Backend:** Python 3.12, FastAPI, SQLAlchemy, SQLite (`chat.db`)
- **Frontend:** HTML / Tailwind CDN / vanilla JS (SPA без фреймворка)
- **AI провайдеры:**
  - OpenAI (gpt-4o, gpt-4o-mini, gpt-image-1, dall-e-3)
  - Anthropic (claude-sonnet-4-6, claude-opus-4-1-20250805) — прямой ключ, прокси awstore УБРАН
  - Grok (xai)
  - Perplexity (sonar)
  - **Google AI Studio через прокси** — Imagen 4 fast/std/ultra, Veo 2/3/3.1
- **Платежи:** ЮKassa
- **PDF:** xhtml2pdf (markdown → HTML → PDF) с фирменным CSS
- **Авторизация:** JWT через python-jose в **httpOnly cookie** (после миграции 2026-04-26) + CSRF middleware (double-submit `csrf_token` cookie ↔ `X-CSRF-Token` header). Legacy: `Authorization: Bearer` в header (back-compat). bcrypt через passlib. `decode_token` пробует все доступные ключи (env + file + LEGACY) — старые сессии не отваливаются при смене источника JWT_SECRET

## Структура файлов
| Файл | Что делает |
|---|---|
| `main.py` | Entry point, роутеры, CSP, middleware (rate-limit, request-id, CORS, body-size) |
| `server/routes/auth.py` | Регистрация, верификация, password reset, OAuth-exchange |
| `server/routes/oauth.py` | Google/VK OAuth (ключей в env пока нет — см. TODO_NEXT) |
| `server/routes/chat.py` | `/message`, `/upload`. **Auto-refund** если AI-видео/картинка вернулись с ошибкой |
| `server/routes/payments.py` | YooKassa init + webhook + confirm-tokens (UNIQUE на yookassa_payment_id) |
| `server/routes/sites.py` | **Фоновая генерация** с polling, `/quality-tiers` (Sonnet/Opus), edit-block AI-правка (real × 5) |
| `server/routes/chatbots.py` | CRUD ботов + 6 шаблонов + AI-improve + превью + аналитика + records + **прайс-лист с embeddings** + **export ZIP** |
| `server/routes/assets.py` | Storage assets (лидмагниты): upload/list/delete + публичный URL `/assets/public/{token}` |
| `server/routes/user_apikeys.py` | Свои API-ключи юзера (OpenAI/Anthropic/Gemini/Grok) с EncryptedString |
| `server/pricing.py` | Динамические цены через БД `pricing_config` с TTL-кэшем 60с. `get_price(key, default)` + `update_price()`, `seed_pricing_defaults()` |
| `server/routes/webhook.py` | TG / VK / Avito / MAX webhooks (TG требует X-Telegram-Bot-Api-Secret-Token) |
| `server/routes/widget.py` | JS-виджет на сайт + WebSocket с Origin-whitelist |
| `server/routes/solutions.py` | Бизнес-решения: запуск + PDF |
| `server/routes/agent.py` | AI-агенты с очередью |
| `server/routes/admin.py` | Админка + **`/admin/actions.txt`** (audit-log в plain text для копирования в чат) |
| `server/routes/public.py` | FAQ, плата по моделям, промокоды |
| `server/routes/user.py` | Личный кабинет, статистика, поддержка |
| `server/routes/presentations.py` | КП + презентации |
| `server/ai.py` | MODEL_REGISTRY, generate_response (с user_api_key), **try_with_keys helper**, image/video через прокси, **`_SecretFilter`** маскирует ключи в логах httpx/openai/anthropic |
| `server/auth.py` | JWT (с iss/aud), хэширование |
| `server/db.py` | SQLAlchemy + LIGHTWEIGHT_MIGRATIONS + WAL + foreign_keys + busy_timeout 30s |
| `server/billing.py` | **Атомарные** списания + `claim_welcome_bonus` + `claim_referral_signup_bonus` |
| `server/secrets_crypto.py` | Шифрование секретов БД через **HKDF**(JWT_SECRET) + sha256 fallback для legacy |
| `server/models.py` | ORM. Bot tokens — EncryptedString. Новое: ActionLog, BotRecord, BotConversationTurn |
| `server/payments.py` | YooKassa + `credit_referral_bonus(payment_id=...)` идемпотентный |
| `server/security.py` | Rate-limit per-IP, validation, `tg_webhook_secret`, `require_admin` |
| `server/agent_runner.py` | Очередь AI-агентов |
| `server/chatbot_engine.py` | Движок чат-ботов: workflow + ноды + persistent conv-память. **MAX**: `_max_headers()` (Authorization БЕЗ Bearer), `setup_max_webhook` со HTTPS-валидацией. **Прайс**: `_price_context_for_question()` с keyword-trigger + cosine similarity. **User-keys**: `_load_user_api_keys()`, `_call_ai_with_fallback()`. **Embeddings**: `_compute_embedding()`, `batch_update_price_embeddings()` |
| `server/workflow_builder.py` | AI-сборка графа из описания через Claude |
| `server/pdf_builder.py` | Markdown→PDF (xhtml2pdf), em→pt, page-break |
| `server/email_service.py` | SMTP отправка |
| `server/email_imap.py` | IMAP-trigger воркфлоу |
| `server/scheduler.py` | Cron-воркеры: schedule, apikey-check, PDF cleanup, DB backup, conv cleanup, **audit cleanup** |
| `server/worker_lock.py` | Advisory-локи через SQLite, **fail-CLOSED** |
| `server/audit_log.py` | **Helper `log_action()` для записи в action_logs** |
| `server/bot_templates.py` | **6 готовых шаблонов**: lead/sales/faq/booking/quiz/content |
| `server/bot_constructor_template.py` | Workflow для бота-конструктора в TG/MAX |
| `views/index.html` | Главная: чат + бизнес-решения + lightbox |
| `views/admin.html` | Админ-панель |
| `views/agents.html` | AI-агенты + Canvas |
| `views/chatbots.html` | Чат-боты: chooser (шаблон/AI/с-нуля) + галерея + превью + аналитика + records + AI-improve + **модалка «Прайс»** (taлица + CSV import + reembed) + **модалка «Лидмагниты»** + help-инструкции к токенам + roadmap каналов |
| `views/sites.html` | Сайты: chooser tier (Sonnet/Opus) + polling-генерация + edit-block AI-правка + **mini-browser в превью** (back/forward/reload/home через postMessage) |
| `views/presentations.html` | КП и презентации |
| `views/icons.js` | **Единый набор SVG-иконок** + **canonical brand_* лого** (Telegram/VK/Avito/MAX/OpenAI/Claude/Gemini/Grok/Perplexity из simple-icons.org CC0) + `getModelBrandIcon(model_id)` helper + **fetch-shim** (auto X-CSRF-Token) + **textarea autopatch maxlength** |

## Запуск (local dev)
```bash
DEV_MODE=true JWT_SECRET=dev-secret python -m uvicorn main:app --reload --port 8001
```

## Запуск (prod)
```
ssh -i 'C:\Users\Денис\.ssh\id_ed25519' root@194.104.9.219
cd /root/AI-CHE && git pull origin main && systemctl restart ai-che
```

## Деньги — РУБЛИ + КОПЕЙКИ (после рефакторинга 2026-04-25)
- Баланс юзера = `User.tokens_balance` в **копейках** (1 ₽ = 100 коп)
- Поля называются `tokens_balance`, `tokens_delta`, `ch_per_1k_*` — это legacy имена, **значение = копейки**
- UI: `window.fmtRub(kop)` → "X.XX ₽"
- **Подписки убраны.** Только пополнение баланса через `/payment/buy-tokens`
- Бонусы:
  - Welcome **50 ₽** (env `WELCOME_BONUS_RUB`) — атомарный gate `User.welcome_bonus_claimed_at`
  - Реферал **10% от каждого пополнения** друга — идемпотентность через `Transaction.yookassa_payment_id` UNIQUE

### Тарифы (актуально на 2026-04-27, все цены в БД `pricing_config`)
| Что | Цена | Pricing-key |
|---|---|---|
| Создание бота с нуля | бесплатно | `bot.scratch_create=0` |
| Бот из шаблона | бесплатно | `bot.template_create=0` |
| AI-конструктор бота | **≥ 1000 ₽** | `bot.ai_create_min=100_000` |
| AI-доработка / правки | real × 5 без минимума | `bot.ai_improve_min=0`, `ai.improve_margin_pct=500` |
| Реальные диалоги бота | real × 3 | `ai.reply_margin_pct=300` |
| Edit-block в сайте | real × 5 | переписан с фикс 5 ₽ |
| Storage файлов | 50 ₽/мес за 100 МБ | `storage.per_100mb_month=5_000` |
| Сайт Sonnet | 1500 ₽ | `site.standard=150_000` |
| Сайт Opus премиум | 1990 ₽ | `site.premium=199_000` |
| Свой API-ключ юзера | -80% (платит 20%) | `ai.user_key_discount_pct=20` |

Изменить любую цену — `POST /admin/pricing` с `{"key":"bot.ai_create_min","value_kop":150000}`. Кэш сбрасывается автоматически.

## AI провайдеры

### OpenAI/Anthropic/Grok
Прямые ключи в БД `api_keys` (provider=`openai`/`anthropic`/`grok`). Подгружаются в env при старте через `_load_all_apikeys_from_db`.

### Google (Imagen + Veo) — ВАЖНО
- Хостинг прода в NL (Dronten, ASN AS41745) — **Google AI Studio блочит этот ASN**, FAILED_PRECONDITION
- Решение: **прокси** в env `GOOGLE_HTTPS_PROXY=http://USER:PASS@HOST:PORT` (сейчас Clouvider Amsterdam)
- Используется ТОЛЬКО для Google-вызовов (Imagen, Veo) — остальное идёт напрямую
- Имена моделей актуальны на 2026-04:
  - **Imagen**: `imagen-4.0-fast-generate-001` (10₽), `imagen-4.0-generate-001` (15₽), `imagen-4.0-ultra-generate-001` (25₽)
    - Imagen 3 устарел, `negativePrompt` deprecated → пихаем в основной prompt текстом
  - **Veo**: `veo-3.0-fast-generate-001` (300₽), `veo-3.0-generate-001` + audio (500₽), `veo-3.1-fast-generate-preview` (400₽), `veo-2.0-generate-001` (200₽)
  - Per-model capabilities (важно):
    - `generateAudio` — только Veo 3.0/3.1 не-fast
    - `image` (i2v) — только Veo 3.x (не Veo 2)
    - `negativePrompt` — только Veo 2

### Видео-генерация Veo
- Асинхронная: `predictLongRunning` → operation → polling до 5 мин (внутри `veo_response`)
- Сохранение mp4 в `/uploads/vid_*.mp4`
- Fallback chain: если Veo 3.0 fast вернул 429/503 — пробуем 3.1 → 3.0 → 2.0
- **Если 429 «prepayment depleted» — auto-refund в `/message`** (юзер не платит за неудачу)

## Шаблоны ботов (6)
- `lead_capture` — лидогенерация: AI квалифицирует → request_contact → save_record + TG-уведомление
- `sales_warmup` — продажи / прогрев + ссылка на оплату
- `faq_support` — kb_rag + эскалация при низкой уверенности
- `booking` — запись на услугу: меню → дата → телефон → бронь
- `quiz_funnel` — серия вопросов → сегмент → персональная рекомендация
- `content_broadcast` — лид-магнит при подписке + рассылки

`server/bot_templates.py` — TEMPLATES list. Endpoint `POST /chatbots/from-template/{slug}`.

## Ноды workflow (chatbot_engine.py)
**Триггеры:** trigger_tg, trigger_vk, trigger_avito, trigger_max, trigger_webhook, trigger_imap, trigger_schedule, trigger_manual

**AI:** node_gpt, node_claude, node_gemini, node_grok, prompt, orchestrator

**Логика:** condition, switch, role_switch, delay, http_request, code_python (sandbox, off by default)

**Storage:** storage_get, storage_set, storage_push

**KB (RAG):** kb_add, kb_search_file, kb_search, kb_rag

**Output:** output_tg, output_tg_buttons, output_tg_file, output_tg_audio, output_vk, output_max, **output_max_buttons**, output_save, output_hook

**Богатый UX (новые):** **request_contact**, **request_location**, **output_photo**, **edit_message**, **chat_action_typing**

**Универсальный:** **save_record** (lead/booking/order/quiz/ticket/subscriber)

**Мета:** **bot_constructor** (создаёт дочерний бот по диалогу)

## Audit log (новое!)
Таблица `action_logs` — все значимые действия пишутся через `server.audit_log.log_action()`.

**Что логируется:**
- `auth.register` / `auth.verify_email` / `auth.oauth`
- `payment.webhook` / `payment.confirm` / `payment.referral_bonus`
- `ai.chat` / `ai.image` / `ai.video` — каждый AI-вызов с моделью + токены + цена
- `ai.media_error` — когда видео/картинка упали (с auto-refund)
- `site.generate_start` / `site.generate_done` / `site.generate_failed`
- `bot.create` / `bot.from_template` / `bot.ai_create` / `bot.ai_improve` / `bot.delete`
- `record.created` — новая заявка от чат-бота

**Эндпоинты для ассистента в новом чате:**
- `GET /admin/actions?since_hours=72&limit=500` — JSON
- `GET /admin/actions.txt?since_hours=72&limit=2000` — **plain text для копирования в чат**
- `GET /admin/actions.jsonl?since_hours=72&limit=5000` — JSONL для машинной обработки
- Фильтры: `action_prefix=ai.`, `level=error`, `only_errors=true`

**Cleanup:** info-level старше 30 дней, error/critical — старше 90 дней (`audit_cleanup_loop` в scheduler).

## Безопасность (после аудитов)
- Чат-ownership: `_assert_chat_owner` без `or_(user_id IS NULL)` — анон-чаты не утекают
- Токены ботов (TG/VK/Avito/widget/MAX) — `EncryptedString` через `secrets_crypto.encrypt/decrypt`
- YooKassa webhook: HMAC обязателен при secret, raw_body читается до json
- TG webhook требует `X-Telegram-Bot-Api-Secret-Token` (без — 401)
- Виджет WS: Origin-whitelist через `ChatBot.widget_allowed_origins`
- `/message` требует current_user (анонимы запрещены)
- CSP заголовок выставлен (allow blob: для превью сайтов и виджет sandbox iframe)
- nginx proxy_read_timeout=600s
- HKDF для Fernet-ключа в `secrets_crypto` (legacy sha256 поддержан для расшифровки старых)
- Welcome / referral бонусы — atomic gates на `User.*_at` (нельзя получить дважды даже на гонке)
- UNIQUE-индекс `uq_transactions_yookassa_id` — webhook идемпотентен
- Worker_lock fail-CLOSED — лучше пропустить tick чем выполнить дважды
- SVG sanitization — блокируем `<script>`/`onload=` в SVG-аплоадах

## Production-readiness
- **Sentry**: guarded `SENTRY_DSN` env. `pip install sentry-sdk[fastapi]`
- **Structured logs**: `STRUCTURED_LOGS=1` → JSON формат
- **X-Request-ID**: middleware, прокидывается в response header
- **Auto-backup chat.db**: ежесуточно через sqlite native backup, retention 14 дней в `/backups/`
- **Audit log**: всё пишется в `action_logs` (см. выше)

## Инфра
- Прод: `root@194.104.9.219` (Дронтен, NL, Clouvider), путь `/root/AI-CHE`, systemd `ai-che`
- venv: `/root/AI-CHE/venv/bin/python`
- env: `/root/AI-CHE/.env` — `OPENAI_API_KEYS`, `ANTHROPIC_API_KEYS`, `GROK_API_KEYS`, `GOOGLE_API_KEYS`, **`GOOGLE_HTTPS_PROXY`**, `JWT_SECRET`, `YOOKASSA_*`, `APP_URL=https://aiche.ru`
- Деплой: `git pull origin main && systemctl restart ai-che`
- БД: SQLite `chat.db` + WAL. Бэкапы автоматом в `/root/AI-CHE/backups/chat.db.YYYY-MM-DD`

## Правила разработки
- Ответы на русском
- Комментарии — минимальные, только где неочевидно
- API-ключи в БД `api_keys`, в env не хардкодим. `_load_all_apikeys_from_db` подгружает на старте
- **Биллинг:** только через `server.billing.deduct_strict/deduct_atomic/credit_atomic`. Прямой `user.tokens_balance += …` запрещён. Все суммы — копейки
- **Сессии БД вне FastAPI Depends:** только через `with db_session() as db:` (rollback при ошибке)
- **Секреты в БД** (IMAP-пароли, токены ботов): через `EncryptedString` TypeDecorator
- **Миграции схемы:** добавлять колонки через `LIGHTWEIGHT_MIGRATIONS` в `server/db.py` (идемпотентный ALTER TABLE IF NOT EXISTS)
- **Webhooks:** TG проверяется secret-token; ЮKassa — HMAC + двойная верификация через `Payment.find_one`
- **Картинки** в `/uploads/` (КОРЕНЬ проекта). Используй `os.path.dirname(_BASE_DIR)` для перехода на parent
- **Цены AI** — копейки в `model_pricing` БД и `TOKEN_COST` (server/ai.py) как fallback
- **Логи действий** — добавляй `log_action(...)` в новые endpoint'ы где есть бизнес-логика
- **Деплой:** `git pull origin main && systemctl restart ai-che`. NEVER `db.drop_all()`, NEVER reset api_keys/users/transactions

## Тесты
`pytest tests/` — **83 проходят** (актуально на 2026-04-27). Файлы:
- `tests/test_api.py` — auth, chat, chatbots CRUD, security, webhooks, **CookieAuth** (4 теста на cookie+CSRF), **YooKassaWebhookSignature** (4 теста на HMAC)
- `tests/test_billing.py` — atomic gates, race conditions, widget Origin/escape, injection
- `tests/test_critical_paths.py` — promo, conversation persistence, try_with_keys, secrets HKDF, edit-block refund
- `tests/conftest.py` — DEV_MODE=true + APP_ENV=dev + JWT_SECRET + apply migrations + **`_clear_cookies_and_rl` autouse fixture** (между тестами чистит client.cookies + SQLite rate-limit)

Запуск:
```bash
cd .claude/worktrees/intelligent-poincare-7d27bf/
DEV_MODE=true APP_ENV=dev JWT_SECRET=test-jwt-secret-32-chars-long-yes \
ALLOWED_ORIGINS=http://localhost:8000 \
python -m pytest tests/ --tb=line
```

## Деплой workflow
Я (Claude) деплою сам когда юзер просит. Команды:
```bash
# Из worktree push в main (fast-forward)
git push origin claude/intelligent-poincare-7d27bf:main

# Прод (HOME workaround для кириллицы в пути SSH-ключа)
HOME="C:\\Users\\Денис" ssh -i "C:\\Users\\Денис\\.ssh\\id_ed25519" \
  root@194.104.9.219 "cd /root/AI-CHE && git pull origin main && \
                       systemctl restart ai-che && systemctl is-active ai-che"
```

## Свежие коммиты (топ-5 на 2026-04-27)
- `3d5f6cf` — help-инструкции к токенам + scrollbar + roadmap каналов
- `40e4e12` — semantic vector search через OpenAI embeddings для прайса
- `868840f` — прайс-лист бота с smart-inject (только при вопросе о цене)
- `823ef92` — мини-браузер в превью + AI-правки real ×5 без фикс
- `624ed9a` — тарифы AI-create ≥1000 ₽ + свои API-ключи юзеров

Полный лог: `git log --oneline -25`. Развёрнутый разбор спринтов — `HANDOVER.md`.
