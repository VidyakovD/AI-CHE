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
5. `git log --oneline -25` — последние коммиты.

## Краткое описание
**B2B AI-платформа для бизнеса.** Веб-приложение FastAPI + HTML SPA + PWA + Telegram-бот управления + Public REST API.

**Главные продукты:**
- **Чат** с моделями: GPT-4o / Claude Sonnet+Opus+Haiku / Perplexity / Grok / GPT-image / Imagen 4 / Veo 3
- **🆕 Бизнес-решения PRO** (multi-agent orchestra) — **8 пилотов** работают параллельно через несколько специализированных агентов с реальным ресёрчем (web_search, browse_url, file_extract, vision)
- **Чат-боты** TG / VK / Avito / MAX / Widget / **🆕 WhatsApp (Wazzup24)** с workflow + 7 шаблонов + прайс-лист с semantic search
- **AI-агенты** с очередью + AI-сборка графа + 25+ специализированных ролей в `server/agents/registry.py`
- **Сайты** — фикс 1500/1990 ₽, фоновая генерация, WYSIWYG-редактор, sandbox-iframe + **public_token** в URL
- **🟢 КП (Proposals)** — отдельный модуль `/proposals.html`: 4 пресета + 4 шапки, бренды, прайсы, JSON-first генерация, WYSIWYG, AI-правка секций, версии, CRM-стадии, email-оркестратор, public-link
- **🟢 Презентации v2** — `/presentations.html`: PPTX/HTML/PDF, color picker, vision-анализ фото, графики, ТЗ-визард
- **🆕 Marketplace ботов** — публикация шаблонов с revenue-split 70/30 + админ-модерация
- **🆕 Public API** для SaaS-интеграций — `Bearer ai_che_<token>`, scopes, rate-limit
- **Платежи** ЮKassa с **54-ФЗ чеком** через ОФД
- **Админка** + аудит-лог + pricing_config
- **Свои API-ключи юзера** (-80% скидка)
- **Storage assets** (50 ₽/мес за 100 МБ) — теперь учитывает RAG-файлы тоже
- **🟢 PWA** — manifest + sw.js, install-prompt
- **🟢 Desktop standalone** — draggable titlebar, window-controls-overlay
- **🟢 TG management-бот** — push + управление, привязка через 6-знач код
- **🆕 Web Push API (VAPID)** — push в браузер при новой заявке/открытии КП
- **🟢 RAG база знаний** — bot/agent KnowledgeFile с embeddings + storage-биллинг

## Стек
- **Backend:** Python 3.12, FastAPI 0.111, SQLAlchemy 2.0, SQLite (`chat.db`)
- **Frontend:** HTML / Tailwind CDN / vanilla JS (SPA)
- **AI:**
  - OpenAI (gpt-4o, gpt-image-1, dall-e-3)
  - Anthropic (claude-sonnet-4-6, claude-opus-4-1, claude-haiku-4) — для streaming используется AsyncAnthropic
  - Grok (xai), Perplexity (sonar)
  - Google AI Studio через прокси (Imagen 4, Veo 2/3)
- **PDF:** xhtml2pdf + DejaVu Sans + Liberation Sans/Serif + Noto Sans/Serif (5 семейств для кириллицы)
- **DOCX:** python-docx 1.1.2 (heading 1-4, bold, lists, tables, разделители)
- **XLSX:** openpyxl (одна таблица = один лист)
- **PPTX:** python-pptx (нативные графики, speaker notes)
- **Push:** pywebpush 2.0.0 (VAPID)
- **Шифрование:** AES-256-GCM (cryptography из python-jose), HKDF от JWT_SECRET
- **Авторизация:** JWT в httpOnly cookie + CSRF (double-submit) + refresh single-use rotation
- **Public API auth:** Bearer ai_che_<prefix>_<secret> с sha256-hash + scope-проверкой

## Структура файлов

### Backend (server/)
| Файл | Что |
|---|---|
| `main.py` | Entry point, роутеры, CSP, middleware (rate-limit/CSRF/request-id), PWA endpoints, `/p/{token}` КП, `/s/{token}` Solution, `/qr/{token}`, race-safe `create_all` |
| `auth.py` | JWT + refresh single-use rotation (User.refresh_jtis JSON список до 10 сессий) + revoke_all_refresh_jtis (logout-everywhere при reset_password) |
| `db.py` | SQLAlchemy + LIGHTWEIGHT_MIGRATIONS + backfill site public_token |
| `models.py` | ORM: User, ChatBot (+ wazzup_*), ProposalBrand, ProposalProject (+ public_token, header_layout), SolutionRun (+ stages_state, attachments_json, public_token, user_mark), `SolutionRunTemplate`, `BotMarketplaceListing/Install`, `ApiToken`, `PushSubscription`, KnowledgeFile (+ last_billed_at) |
| `billing.py` | Атомарные списания + бонусы (deduct_strict/atomic, credit_atomic) |
| `security.py` | Rate-limit, validate_password, tg_webhook_secret, _csv_safe, _SecretFilter |
| `pricing.py` | Динамические цены через `pricing_config` (DEFAULTS) |
| `scheduler.py` | Cron-воркеры: scheduler/apikey/pdf/db_backup (AES-GCM) /conv/audit/storage-billing (StoredAsset + KnowledgeFile UNION) |
| `ai.py` | MODEL_REGISTRY, generate_response, _SecretFilter |
| `chatbot_engine.py` (3100+ строк) | Движок ботов + workflow + ноды + send_telegram/vk/max/whatsapp/avito/site + send_whatsapp (Wazzup24) |
| `bot_templates.py` | 7 шаблонов |
| `pdf_builder.py` | html_to_pdf_bytes + 5 семейств шрифтов + markdown_to_pdf |
| **`docx_builder.py`** | Markdown→DOCX (heading, bold, lists, tables, разделители) |
| **`xlsx_builder.py`** | Markdown→XLSX (каждая таблица — отдельный лист, главный лист «Отчёт» с резюме) |
| **`solutions_orchestra.py`** (~700 строк) | Multi-agent runtime: 7 stage-типов (web_search/browse_url/llm/synthesize/parallel_llm/file_extract/vision_describe/extract_urls/parallel_browse/generate_image), streaming AsyncAnthropic, restage(), pub/sub для SSE |
| **`push.py`** | Web Push (VAPID) — push_to_user, dedup expired 410/404 |
| `proposal_builder.py` (~1200 строк) | КП: parse_client_site, JSON-first prompt v3, _render_proposal_json → HTML, _PRESET_CSS (4 пресета), 4 header_layout, edit_section, bleach-санитизация |
| `presentation_builder.py` (~1100 строк) | Презентации v2 |
| `tg_management.py` | TG-бот управления |
| `email_service.py` | SMTP + send_with_attachment + login alerts |
| `email_imap.py` | IMAP-trigger + email threading |
| `secrets_crypto.py` | Шифрование через HKDF(JWT_SECRET) |
| `agent_runner.py` | Оркестратор AI-агентов + tool_browse_url (SSRF-safe) + tool_web_search + tool_run_llm + tool_generate_image |

### Routes (server/routes/)
| Роут | Что |
|---|---|
| `auth.py` | Регистрация/login + refresh single-use + revoke + marketing_consent в register |
| `payments.py` | YooKassa init + webhook (HMAC) + 54-ФЗ receipt (vat_code, tax_system_code из env) |
| `chat.py` | `/message` /upload, auto-refund |
| `sites.py` | Сайты + ZIP + sandbox-iframe + **public_token** lookup |
| `chatbots.py` | CRUD + 7 шаблонов |
| `assets.py` | Storage |
| `user_apikeys.py` | Свои API-ключи |
| `user.py` | Кабинет + transactions.csv + TG-link + **marketing-consent** + **push (subscribe/test/etc)** |
| `proposals.py` | brands CRUD, projects CRUD, generate, public-link, send-email, stage, price-lists |
| `presentations.py` | generate, estimate-cost, pptx, pdf, brief-assist |
| `webhook.py` | TG/VK/Avito/MAX webhooks + tg-mgmt + **wazzup24** |
| `widget.py` | Виджет на сайт + WS Origin-whitelist |
| **`solutions.py`** | 30 готовых решений (legacy plain) + **8 orchestra-пилотов**: start, stream (SSE), restage, save-template/run-template, share, reaction, docx/xlsx, **start-compare/compare/{group}** |
| **`marketplace.py`** | Публикация ботов + каталог + install (70/30 split) + review + admin-модерация |
| **`public_api.py`** | mgmt: /api-tokens CRUD; api: /api/v1/me, /api/v1/proposals/generate+get |
| `assistant.py` | AI-помощник по разделам с feedback |
| `qr_login.py` | QR-логин |
| `mobile.py` | Lite-режим + voice |
| `knowledge.py` | RAG база знаний |
| `solutions.py`, `agent.py`, `public.py`, `oauth.py`, `admin.py` | Прочие |

### Frontend (views/)
| Файл | Что |
|---|---|
| `index.html` | Главная: чат + бизнес-решения (с **🔬 Compare** и **PRO orchestra**) + кабинет (+ **📬 marketing-consent**, **🔔 Web Push subscribe/test**) |
| `admin.html`, `agents.html`, `chatbots.html`, `mobile.html`, `qr_confirm.html` | Соответствующие модули |
| `sites.html` | Сайты с WYSIWYG-редактором |
| `proposals.html` | КП: 3 вкладки + WYSIWYG + AI-правка + версии + публичная ссылка + email + дубль |
| `presentations.html` | Презентации v2 |
| `terms.html` | Оферта |
| `icons.js` | SVG-иконки + brand_* лого + CSRF + autopatch + **PWA-tags** + **aiAlert/aiConfirm/aiPrompt** + install-prompt |
| `manifest.json`, `sw.js`, `icon.svg`, `logo-*.png`, `favicon.png` | PWA + Web Push handler |
| `knowledge-ui.js` | UI RAG (общая для агентов и ботов) |

## Запуск (local dev)
```bash
DEV_MODE=true APP_ENV=dev JWT_SECRET=test-jwt-secret-32-chars-long-yes \
ALLOWED_ORIGINS=http://localhost:8000 \
python -m uvicorn main:app --reload --port 8001
```

## Запуск (prod)
```bash
HOME="C:\\Users\\Денис" ssh -i 'C:\\Users\\Денис\\.ssh\\id_ed25519' \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  root@194.104.9.219 \
  "cd /root/AI-CHE && git pull origin main && systemctl restart ai-che"
```

⚠️ **uvicorn слушает только 127.0.0.1**. Внешний :8000 закрыт UFW. Доступ только через nginx.

## Деньги — РУБЛИ + КОПЕЙКИ
- Баланс юзера = `User.tokens_balance` в **копейках** (1 ₽ = 100 коп)
- Поля называются `tokens_balance`, `tokens_delta`, `ch_per_1k_*` — это legacy имена, **значение = копейки**
- UI: `window.fmtRub(kop)` → "X.XX ₽"

### Тарифы (актуально на 2026-05-04, все цены в БД `pricing_config`)
| Что | Цена | Pricing-key |
|---|---|---|
| Создание бота с нуля | бесплатно | `bot.scratch_create=0` |
| Бот из шаблона | бесплатно | `bot.template_create=0` |
| AI-конструктор бота | ≥ 1000 ₽ | `bot.ai_create_min=100_000` |
| AI-доработка / правки | real × 5 | `ai.improve_margin_pct=500` |
| Реальные диалоги бота | real × 3 | `ai.reply_margin_pct=300` |
| Edit-block в сайте | real × 5 | `ai.improve_margin_pct=500` |
| Storage файлов (включая RAG) | 50 ₽/мес за 100 МБ | `storage.per_100mb_month=5_000` |
| Сайт Sonnet | 1500 ₽ | `site.standard=150_000` |
| Сайт Opus | 1990 ₽ | `site.premium=199_000` |
| Свой API-ключ юзера | -80% (платит 20%) | `ai.user_key_discount_pct=20` |
| **🟢 КП первый раз** | 50 ₽ | `proposal.create=5000` |
| **🟢 КП перегенерация** | 5 ₽ | `proposal.edit=500` |
| **🟢 КП AI-правка секции** | real × 5 | `ai.improve_margin_pct=500` |
| **🟢 КП авто-генерация** | 50 ₽ | `proposal.auto_create=5000` |
| **🟢 Презентация (по факту)** | real × 7 (margin внутри, в UI не показываем) | `presentation.margin_pct=700` |
| **🆕 Бизнес-решение orchestra (по stage'ам)** | real × 5 за каждый llm-stage | `ai.improve_margin_pct=500` |
| **🆕 Аудит лендинга** | до 250 ₽ | accumulated over stages |
| **🆕 Юр. проверка договора** | до 350 ₽ | accumulated |
| **🆕 Финансовый аудит Excel** | до 300 ₽ | accumulated |
| **🆕 Аудит соцсети канала** | до 250 ₽ | accumulated |
| **🆕 Холодная email-рассылка** | до 250 ₽ | accumulated |
| **🆕 Compare моделей** | × N (за каждый запуск) | per-stage real × margin |
| **🆕 Marketplace install** | price_kop листинга (70% автору) | per listing |

## 🆕 Multi-agent Orchestra для Solutions

`Solution.orchestra_json` содержит JSON-граф stage'ов. 7 типов stage:
- `web_search` — `tool_web_search(query, num_results)` (без списания)
- `browse_url` — `tool_browse_url(url)` с SSRF-защитой
- `parallel_browse` — N параллельных browse_url через asyncio.gather
- `extract_urls` — regex-выдёргивание URL из output stage'а
- `llm` / `synthesize` — `generate_response`, у synthesize в финале есть **streaming** через AsyncAnthropic
- `parallel_llm` — N параллельных llm с разными branches
- `file_extract` — PDF/DOCX/XLSX → text через `knowledge.extract_text`
- `vision_describe` — картинка → описание Claude Haiku
- `generate_image` — DALL-E через `tool_generate_image` → возвращает `![](url)` markdown

Промпт-шаблоны: `{{input}}`, `{{<id>.output}}`, `{{<id>.outputs[i]}}`, `{{<id>.outputs}}` — заменяются на текст из контекста.

**8 orchestra-пилотов** (см. `scripts/seed_orchestra_solutions.py`):
1. **Конкурентный анализ ниши** (150 ₽) — web_search → 5 параллельных browse → 5 deep-аналитиков → стратег
2. **Полный SWOT-анализ** (150 ₽) — web_search контекст → 4 параллельных квадранта (S/W/O/T) → Opus-стратег
3. **Контент-план месяц** (200 ₽) — trend-scout → 3 параллельных копирайтера (VK/TG/Insta) → планировщик
4. **Аудит лендинга** (250 ₽) — extract_urls + parallel_browse + опц. vision-скриншот → 3 аналитика (UX/SEO/CRO) → план + готовые тексты
5. **Юр. проверка договора** (350 ₽) — file_extract DOCX → структуризация + web_search норм → 3 юриста (риски/существенные/императивные) → ПРОТОКОЛ РАЗНОГЛАСИЙ со ссылками на ГК РФ
6. **Аудит соцсети канала** (250 ₽) — browse_url канала + опц. vision → 3 аналитика (контент/engagement/монетизация) → стратегия 90 дней
7. **Финансовый аудит Excel** (300 ₽) — file_extract XLSX → 3 аналитика (тренды/юнит-эк/риски) → 5 действий с цифрами
8. **Холодная email-рассылка** (250 ₽) — extract_urls + parallel_browse 5 сайтов → разведчик с фактами → parallel_llm копирайтеров → 5 писем + план кампании

**UX-фичи orchestra:**
- Live-progress через SSE (`/solutions/runs/{id}/stream`) с heartbeat 1s
- ↻ Re-run отдельного stage с extra_instruction (real × margin)
- ⭐ Save as template (input + attachments) → 1 клик повторить
- 🔗 Share через `public_token` → `/s/{token}` без auth (отдаёт PDF/markdown)
- 📄 PDF / 📝 DOCX / 📊 XLSX экспорты (один markdown → разные форматы)
- 👍/👎/💡 reaction + auto-flagging (3+ 👎 за 7 дней → email админу)
- 🔬 Compare: запустить на 2-3 моделях параллельно

## 🆕 WhatsApp канал (Wazzup24)

Поля `ChatBot.wazzup_api_key` (encrypted) + `wazzup_channel_id`. Webhook `POST /webhook/wazzup/{bot_id}` с HMAC-secret в URL. `send_whatsapp(api_key, channel_id, chat_id, text)` в `chatbot_engine.py`. Все 7 шаблонов работают через WhatsApp.

## 🆕 Web Push API (VAPID)

Браузер регистрируется через `PushManager.subscribe(applicationServerKey)`. Endpoints `/user/push/{vapid-public, subscribe, unsubscribe, status, test}`. Хуки: новая заявка из бота → push владельцу; клиент открыл `/p/{token}` КП → push.

VAPID-ключи на проде:
- `VAPID_PUBLIC_KEY=...`
- `VAPID_PRIVATE_KEY_FILE=/root/AI-CHE/.vapid_private.pem` (0o400)
- `VAPID_SUBJECT=mailto:admin@aiche.ru`

## 🆕 Marketplace ботов

`BotMarketplaceListing` (snapshot system_prompt + workflow_json + price + cover) проходит через `is_approved` админа. При install создаётся новый ChatBot у юзера; платный режим: списание + 70% автору / 30% платформе.

Endpoints в `server/routes/marketplace.py`:
- `POST /marketplace/listings` — опубликовать
- `GET /marketplace/listings` — публичный каталог одобренных
- `GET /marketplace/my-listings` — свои
- `POST /marketplace/listings/{id}/install` — установить
- `POST /marketplace/listings/{id}/review` — рейтинг 1-5
- `GET /marketplace/admin/pending`, `POST /admin/listings/{id}/approve|reject`

UI каталога ещё не сделан.

## 🆕 Public API для SaaS

`ApiToken` (prefix + sha256-hash секрета + scopes CSV). Auth: `Authorization: Bearer ai_che_<prefix>_<secret>`. Управление в кабинете через `/api-tokens` (mgmt_router). Доступные endpoints:
- `GET /api/v1/me` — баланс
- `POST /api/v1/proposals/generate {name, client_request, brand_id, ...}` → proposal_id + pdf_url, синхронно, списание `proposal.create` (50 ₽), auto-refund при ошибке
- `GET /api/v1/proposals/{id}` — статус

## Ноды workflow (chatbot_engine.py)
**Триггеры:** trigger_tg, trigger_vk, trigger_avito, trigger_max, trigger_webhook, trigger_imap, trigger_schedule, trigger_manual

**AI:** node_gpt, node_claude, node_gemini, node_grok, prompt, orchestrator

**Логика:** condition, switch, role_switch, delay, http_request, code_python (sandbox)

**Storage:** storage_get, storage_set, storage_push

**KB (RAG):** kb_add, kb_search_file, kb_search, kb_rag

**Output:** output_tg, output_tg_buttons, output_tg_file, output_tg_audio, output_vk, output_max, output_max_buttons, output_save, output_hook

**Богатый UX:** request_contact, request_location, output_photo, edit_message, chat_action_typing

**Универсальный:** save_record (lead/booking/order/quiz/ticket/subscriber/proposal_sent) — теперь с push владельцу

**Мета:** bot_constructor, **🟢 auto_proposal** (генерит КП из IMAP-письма + опц. SMTP-ответ + TG approval flow)

## Аудит-лог
Таблица `action_logs`. **Что логируется:** auth.* / payment.* / ai.* / proposal.* / **solution.\*** (orchestra_started, restage, reaction, auto_flagged, orchestra_compare) / **marketplace.\*** (published/installed/approved/rejected) / **api_token.\*** (created/revoked) / **knowledge.\*** / qr.* / record.created / asset.*

**Эндпоинты:** `/admin/actions(.txt|.jsonl)`. Cleanup retention в scheduler (info 30д, auth/payment 365д, error 90д).

## Безопасность

### Network/Infra
- ✅ HTTPS-only + HSTS
- ✅ UFW активен (22/80/443)
- ✅ uvicorn 127.0.0.1
- ✅ fail2ban + nginx server_tokens off

### Auth
- ✅ bcrypt + timing-safe verify + dummy-hash на login
- ✅ Password policy 10+ симв
- ✅ JWT в httpOnly cookie + CSRF (double-submit)
- ✅ **Refresh token rotation single-use** (User.refresh_jtis JSON, до 10 multi-device, reuse → revoke ALL)
- ✅ Login alert email при новом IP
- ✅ Aud/iss claims (но проверка пока не strict — отложено)

### Application
- ✅ SQLAlchemy ORM везде, CSRF, IDOR-проверки `filter_by(user_id=user.id)`
- ✅ Path traversal protection (ZIP, storage, /p/{token}, KB)
- ✅ CSV-injection: `_csv_safe`
- ✅ `_SecretFilter` на root-handler
- ✅ Storage billing race fix (UNION StoredAsset + KnowledgeFile)
- ✅ UNIQUE-индексы на yookassa_payment_id
- ✅ **Sites enumeration → `public_token`** (~160bit вместо int_id)
- ✅ **VK webhook требует `vk_secret` + compare_digest**
- ✅ **SSRF в agent tool_browse_url** (DNS rebinding + revalidate redirects)
- ✅ **bleach-санитизация** generated_html КП (legacy fallback + edit-section + save-html)
- ✅ **Agent /ws + /stream IDOR-защита** (cookie/query token)
- ✅ **TG-link rate-limit + email-alert** при привязке/перепривязке
- ✅ Iframe sandbox без allow-same-origin (sites preview)
- ✅ HKDF для Fernet-ключа
- ✅ http_request нода: двойной DNS + CIDR блок-лист
- ✅ code_python sandbox

### Compliance (152-ФЗ + 54-ФЗ)
- ✅ AES-256-GCM шифрование DB-бэкапов (`scheduler._db_backup_tick`, ключ в `.backup_encryption_key` 0o400)
- ✅ 54-ФЗ receipt в YooKassa: payment_subject + payment_mode + tax_system_code + vat_code (env)
- ✅ Маркетинговое согласие отдельно от оферты (User.marketing_consent, не предзаполняется)
- ✅ Из payment-логов убраны суммы (только payment_id)
- ⚠ SMTP не настроен на проде → юзеры не получают verification (нужен Unisender/SendPulse)
- ⚠ Прод в Нидерландах → нужна миграция в РФ (152-ФЗ ст. 18)
- ⚠ Регистрация в РКН — задача юзера

### Dependencies
- ✅ python-jose 3.4.0, multipart 0.0.26, dotenv 1.2.2, markdown 3.8.1, bleach 6.1.0
- ✅ python-docx 1.1.2, pywebpush 2.0.0
- ⚠️ starlette 0.37.2 (CVE pinned в FastAPI 0.111) — отдельный спринт upgrade

## Production-readiness
- ✅ Sentry (guarded `SENTRY_DSN`)
- ✅ Structured logs (`STRUCTURED_LOGS=1` → JSON)
- ✅ X-Request-ID middleware
- ✅ Auto-backup chat.db **с AES-GCM** + PRAGMA integrity_check, retention 14 дней
- ✅ Audit log с эшелонированной retention
- ✅ Idempotency-Key в /message
- ✅ CI workflow с pytest + ruff + pip-audit

## Инфра
- Прод: `root@194.104.9.219` (Дронтен, NL, Clouvider), путь `/root/AI-CHE`
- venv: `/root/AI-CHE/venv/bin/python`
- env: `/root/AI-CHE/.env` — все API-ключи, `JWT_SECRET`, `YOOKASSA_*`, `APP_URL=https://aiche.ru`, `TG_MGMT_BOT_TOKEN` (опц.), `GOOGLE_HTTPS_PROXY`, **`VAPID_PUBLIC_KEY`**, **`VAPID_PRIVATE_KEY_FILE`**
- `.backup_encryption_key` (0o400) — резервная копия в **1Password / Vault** обязательна
- `.vapid_private.pem` (0o400)
- Шрифты: `fonts-liberation` + `fonts-noto-core` через apt
- Деплой: `git pull origin main && systemctl restart ai-che`
- БД: SQLite `chat.db` + WAL. Бэкапы автоматом в `/root/AI-CHE/backups/chat.db.YYYY-MM-DD.enc`

## Правила разработки
- Ответы на русском
- Комментарии минимальные, только где неочевидно
- API-ключи в БД `api_keys`, в env не хардкодим
- **Биллинг:** только через `server.billing.deduct_strict/deduct_atomic/credit_atomic`. Все суммы — копейки
- **Сессии БД вне FastAPI Depends:** только через `with db_session() as db:`
- **Секреты в БД:** через `EncryptedString`
- **Миграции схемы:** `LIGHTWEIGHT_MIGRATIONS` в `server/db.py`
- **Webhooks:** TG/MAX/Wazzup24 через secret-token; ЮKassa через HMAC
- **Картинки** в `/uploads/` (КОРЕНЬ проекта)
- **Логи действий:** `log_action(...)` в новые endpoint'ы
- **Native dialogs запрещены** — везде `aiAlert/aiConfirm/aiPrompt`
- **Деплой:** `git push origin main && ssh ... git pull && systemctl restart ai-che`. NEVER `db.drop_all()`, NEVER reset api_keys/users/transactions
- **Public API endpoints должны быть scope-aware** через `authenticate_token(request, db, required_scope=...)`

## Тесты
`pytest tests/` — **164 проходят, 2 skipped** (актуально на 2026-05-04). Skipped — DOCX/XLSX builders когда python-docx/openpyxl не установлены в dev (на проде есть).
- `tests/test_api.py` — auth, chat, chatbots CRUD, security, webhooks, refresh single-use
- `tests/test_billing.py` — atomic gates, race conditions, widget Origin
- `tests/test_critical_paths.py` — promo, conversation, try_with_keys, secrets HKDF, edit-block refund, **TestSolutionsOrchestra**, **TestMarketplace**, **TestPublicAPI**
- `tests/test_assistant.py` — AI-помощник
- `tests/test_knowledge.py` — RAG
- `tests/test_mobile.py`, `tests/test_qr_login.py`

```bash
cd .claude/worktrees/festive-goldwasser-d084fe/
DEV_MODE=true APP_ENV=dev JWT_SECRET=test-jwt-secret-32-chars-long-yes \
ALLOWED_ORIGINS=http://localhost:8000 \
python -m pytest tests/ --tb=line
```

## Деплой workflow
```bash
git push origin claude/festive-goldwasser-d084fe:main

HOME="C:\\Users\\Денис" ssh -i "C:\\Users\\Денис\\.ssh\\id_ed25519" \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  root@194.104.9.219 \
  "cd /root/AI-CHE && git pull origin main && \
   systemctl restart ai-che && systemctl is-active ai-che"
```

При добавлении новых orchestra-решений:
```bash
ssh ... "cd /root/AI-CHE && /root/AI-CHE/venv/bin/python scripts/seed_orchestra_solutions.py"
```

## Свежие коммиты (топ-15 на 2026-05-04)
- `963c365` — fix(startup): catch table-already-exists race в create_all
- `75e2462` — feat: сравнение моделей + Marketplace ботов + Public API
- `1126e50` — feat: XLSX/streaming/auto-flag + inline images + WhatsApp + Web Push
- `bcc4cf3` — feat(orchestra-pro): re-run + templates + reactions + DOCX + 3 новых решения (соцсеть/Excel/cold-email)
- `5a91f68` — feat(orchestra): глубокий ресерч (file_extract / vision / browse) + 2 новых решения (аудит лендинга/юр.договор)
- `fa85629` — feat(solutions): multi-agent orchestra — параллельные специализированные агенты (3 пилота: SWOT/Comp/Content)
- `da5aee6` — feat(ui+docs): UI чекбокс маркетинговой рассылки + обновление HANDOVER/TODO
- `a2bffc0` — feat(compliance): РФ-чеклист — 152-ФЗ + 54-ФЗ + шифрование бэкапов
- `d90e2f1` — feat(security): refresh-rotation single-use + sites public_token + RAG billing
- `cc5afa5` — fix(security): аудит-чек-лист — XSS/SSRF/IDOR/auth-leak/abuse
- `48b8ed0` — feat(kp): конструктор шапки КП — 4 стиля
- `1a70a2c` — fix(image-gen): качество HD/high по умолчанию
- `21dfe81` — feat: единый маскот для всех приветствий + фиксы КП
- `516ddaf` — feat: welcome-маскот, фикс PDF/PPTX
- `e84a9f2` — feat(quick-wins): экономия + UX-помощь новичкам

Полный лог: `git log --oneline -30`. Развёрнутый разбор спринтов — `HANDOVER.md`.
