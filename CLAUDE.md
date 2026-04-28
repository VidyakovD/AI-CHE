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
**B2B AI-платформа для бизнеса.** Веб-приложение FastAPI + HTML SPA + PWA + Telegram-бот управления.

**Главные продукты:**
- **Чат** с моделями: GPT-4o / Claude Sonnet+Opus+Haiku / Perplexity / Grok / GPT-image / Imagen 4 / Veo 3
- **Бизнес-решения** (Solutions) — 30 экспертных промптов с фикс-ценой 50/100 ₽
- **Чат-боты** TG / VK / Avito / MAX с workflow + 7 шаблонов + прайс-лист с semantic search
- **AI-агенты** с очередью + AI-сборка графа
- **Сайты** — фикс 1500/1990 ₽, фоновая генерация, **WYSIWYG-редактор (body-level contenteditable)**
- **🟢 КП (Proposals)** — отдельный модуль `/proposals.html`: 4 пресета оформления, бренды, прайсы, JSON-first генерация, WYSIWYG-правка, AI-правка секций, версии, CRM-стадии, email-оркестратор, public-link, threading
- **🟢 Презентации v2** — `/presentations.html`: PPTX/HTML/PDF, color picker, vision-анализ фото, графики, ТЗ-визард, парсинг сайта клиента
- **Платежи** ЮKassa
- **Админка** + аудит-лог + pricing_config
- **Свои API-ключи юзера** (-80% скидка)
- **Storage assets** (50 ₽/мес за 100 МБ)
- **🟢 PWA** — manifest + sw.js, install-prompt
- **🟢 Desktop standalone** — draggable titlebar, window-controls-overlay
- **🟢 TG management-бот** — push + управление, привязка через 6-знач код

## Стек
- **Backend:** Python 3.12, FastAPI, SQLAlchemy, SQLite (`chat.db`)
- **Frontend:** HTML / Tailwind CDN / vanilla JS
- **AI:**
  - OpenAI (gpt-4o, gpt-image-1, dall-e-3)
  - Anthropic (claude-sonnet-4-6, claude-opus-4-1, claude-haiku-4)
  - Grok (xai), Perplexity (sonar)
  - Google AI Studio через прокси (Imagen 4, Veo 2/3)
- **PDF:** xhtml2pdf + DejaVu Sans + Liberation Sans/Serif + Noto Sans/Serif (5 семейств для кириллицы)
- **PPTX:** python-pptx (нативные графики, speaker notes)
- **Авторизация:** JWT в httpOnly cookie + CSRF (double-submit)

## Структура файлов

### Backend (server/)
| Файл | Что |
|---|---|
| `main.py` (516 строк) | Entry point, роутеры, CSP, middleware (rate-limit/CSRF/request-id), PWA endpoints (manifest/sw/icon), `/p/{token}` для публичных КП |
| `auth.py` | JWT, httpOnly cookies, CSRF |
| `db.py` | SQLAlchemy + LIGHTWEIGHT_MIGRATIONS |
| `models.py` (953 строк) | ORM: User (+ tg_*), ChatBot, ProposalBrand, ProposalProject, ProposalVersion, ProposalPriceList, ProposalPriceItem, PresentationProject (расширена) |
| `billing.py` | Атомарные списания + бонусы |
| `security.py` | Rate-limit, validate_password (10+ симв), tg_webhook_secret, _csv_safe |
| `pricing.py` | Динамические цены через `pricing_config` |
| `scheduler.py` | Cron-воркеры (storage-billing, db_backup, audit cleanup) |
| `ai.py` | MODEL_REGISTRY, generate_response, _SecretFilter (на root-handler) |
| `chatbot_engine.py` (3045 строк) | Движок ботов + workflow + ноды + auto_proposal |
| `bot_templates.py` | 7 шаблонов (lead/sales/faq/booking/quiz/content/auto_proposal_email) |
| `pdf_builder.py` | html_to_pdf_bytes + 5 семейств шрифтов + resolve_pdf_font |
| **🟢 `proposal_builder.py`** (997 строк) | КП: parse_client_site, JSON-first prompt v3, _render_proposal_json → HTML, _PRESET_CSS (4 пресета), edit_section, signature/tagline/usp/guarantees |
| **🟢 `presentation_builder.py`** (1061 строк) | Презентации v2: estimate/calc cost (margin ×7 внутри), Claude prompt v3 с JSON-слайдами, _render_html_preview_inner с SVG-графиками, build_pptx_with_palette, _render_pdf_html, describe_image_via_claude (vision), parse_client_site_for_style, _resolve_colors_for_project (HEX > пресет), _build_custom_palette |
| **🟢 `tg_management.py`** (513 строк) | TG-бот управления: send_message_sync/async, generate/consume_link_code, handle_update (/start /link /unlink /me /stats /menu + callback), notify_user(user_id, text, kind) |
| `email_service.py` | SMTP + send_with_attachment + login alerts |
| `email_imap.py` | IMAP-trigger + email threading (In-Reply-To → ProposalProject.outbox_message_id) |
| `secrets_crypto.py` | Шифрование через HKDF(JWT_SECRET) |

### Routes (server/routes/)
| Роут | Что |
|---|---|
| `auth.py` | Регистрация/login (с TG login alert при новом IP) |
| `payments.py` | YooKassa init + webhook (HMAC обязателен) |
| `chat.py` | `/message` /upload, auto-refund при ошибке |
| `sites.py` | Сайты + path-traversal guard в ZIP |
| `chatbots.py` | CRUD + 7 шаблонов + аналитика (1 SQL вместо 9) + records (с _csv_safe) + ZIP export |
| `assets.py` | Storage с биллингом |
| `user_apikeys.py` | Свои API-ключи юзера |
| `user.py` | Кабинет + transactions.csv (P0 fix) + **TG-link endpoints** (status/code/unlink/notifications) |
| **🟢 `proposals.py`** | brands CRUD, projects CRUD, generate (JSON-first + edit_section AI-правка + версии), public-link, send-email, stage (CRM), price-lists CRUD + CSV-импорт, save-html (WYSIWYG), duplicate |
| **🟢 `presentations.py`** | generate (JSON слайды → HTML/PPTX/PDF), estimate-cost (динамика), pptx, preview-html, pdf, brief-assist (ТЗ-визард через Claude Haiku) |
| `webhook.py` | TG/VK/Avito/MAX webhooks + **`/webhook/tg-mgmt/{secret}`** (path_secret + X-Telegram-Bot-Api-Secret-Token) |
| `widget.py` | Виджет на сайт + WS Origin-whitelist |
| `solutions.py`, `agent.py`, `public.py`, `oauth.py`, `admin.py` | Соответствующие модули |

### Frontend (views/)
| Файл | Что |
|---|---|
| `index.html` | Главная: чат + бизнес-решения + кабинет (вкладка **«📲 Приложение»**). Draggable titlebar в standalone. WYSIWYG-режим |
| `admin.html`, `agents.html`, `chatbots.html` | Соответствующие модули |
| `sites.html` | Сайты с WYSIWYG-редактором (contenteditable=true на body), AI-правка блока, замена картинок (cache-bust) и иконок (SVG/FA/Lucide) |
| **🟢 `proposals.html`** | КП: 3 вкладки (Мои КП / Оформление / Прайсы) + CRM-фильтры + WYSIWYG + AI-правка секций + версии + публичная ссылка + email + дубль |
| **🟢 `presentations.html`** | Презентации v2: topic/audience/slide_count slider + extra_info + URL клиента + 4 color picker + 4 пресета + графики + ТЗ-визард + динамическая цена |
| `presentations.html` (legacy) | Старые КП через doc_type='kp' — фильтруются, есть совет открыть /proposals.html |
| `terms.html` | Оферта |
| `icons.js` | SVG-иконки + brand_* лого + fetch-shim CSRF + textarea autopatch + **PWA-tags автоустановка** + **aiAlert/aiConfirm/aiPrompt** (custom modals) + install-prompt API |
| **🟢 `manifest.json`** | PWA: name/icons/start_url/shortcuts, display:standalone, window-controls-overlay |
| **🟢 `sw.js`** | Service Worker: static cache-first, HTML network-first + offline fallback, push handler |
| **🟢 `icon.svg`** | Стилизованная Ч (maskable) |

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

⚠️ **uvicorn слушает только 127.0.0.1** (после security audit). Внешний :8000 закрыт UFW. Доступ только через nginx.

## Деньги — РУБЛИ + КОПЕЙКИ
- Баланс юзера = `User.tokens_balance` в **копейках** (1 ₽ = 100 коп)
- Поля называются `tokens_balance`, `tokens_delta`, `ch_per_1k_*` — это legacy имена, **значение = копейки**
- UI: `window.fmtRub(kop)` → "X.XX ₽"

### Тарифы (актуально на 2026-04-28, все цены в БД `pricing_config`)
| Что | Цена | Pricing-key |
|---|---|---|
| Создание бота с нуля | бесплатно | `bot.scratch_create=0` |
| Бот из шаблона | бесплатно | `bot.template_create=0` |
| AI-конструктор бота | ≥ 1000 ₽ | `bot.ai_create_min=100_000` |
| AI-доработка / правки | real × 5 | `ai.improve_margin_pct=500` |
| Реальные диалоги бота | real × 3 | `ai.reply_margin_pct=300` |
| Edit-block в сайте | real × 5 | `ai.improve_margin_pct=500` |
| Storage файлов | 50 ₽/мес за 100 МБ | `storage.per_100mb_month=5_000` |
| Сайт Sonnet | 1500 ₽ | `site.standard=150_000` |
| Сайт Opus | 1990 ₽ | `site.premium=199_000` |
| Свой API-ключ юзера | -80% (платит 20%) | `ai.user_key_discount_pct=20` |
| **🟢 КП первый раз** | 50 ₽ | `proposal.create=5000` |
| **🟢 КП перегенерация** | 5 ₽ | `proposal.edit=500` |
| **🟢 КП AI-правка секции** | real × 5 | `ai.improve_margin_pct=500` |
| **🟢 КП авто-генерация** | 50 ₽ | `proposal.auto_create=5000` |
| **🟢 Презентация (по факту)** | real × 7 (margin внутри, в UI не показываем) | `presentation.margin_pct=700` |

## 🟢 КП-конструктор (proposals)

### Модели
- `ProposalBrand` — name/logo/3 цвета/шрифт/preset/реквизиты + tagline/usp_list/guarantees/tone(business/friendly/premium/tech)/intro_phrase/cta_phrase
- `ProposalProject` — name/brand/bot/price_list/client_*/extra_notes/generated_html|pdf/crm_stage(new/sent/opened/replied/won/lost)/sent_at/opened_at/replied_at/won_at/lost_at/public_token/outbox_message_id
- `ProposalVersion` — снапшоты до 10 на КП с note
- `ProposalPriceList` + `ProposalPriceItem` — отдельный модуль прайсов (не из бота)

### Pipeline генерации
1. Pre-validation (длина 30-25000, валидный URL)
2. Парсинг сайта клиента (если задан) → `parse_client_site` → текст-контекст
3. Прайс: `fetch_price_from_list(price_list_id)` (новый) → fallback `fetch_price_from_bot(bot_id)` (legacy)
4. Claude prompt v3 → JSON {hero, understanding, offering, pricing, timeline, cta}
5. Backend рендерит в HTML по фиксированному шаблону + preset_css (minimal/classic/bold/compact)
6. PDF через xhtml2pdf
7. Снапшот в ProposalVersion

### Фишки
- **WYSIWYG**: contenteditable=true на body — юзер кликает в любой текст, печатает
- **AI-правка секции**: клик по блоку → AI переписывает только его (real × 5)
- **Версионирование**: до 10 версий, можно откатиться
- **Email-orchestration** (нода `auto_proposal`): IMAP → детект ключевых слов → генерация → SMTP-ответ с PDF + threading
- **Whitelist**: `cfg.email_whitelist=domain1.ru` для авто-режима
- **Pre-approval mode** в auto: вместо отправки шлёт владельцу TG-уведомление
- **Публичная ссылка**: `/p/{public_token}` без auth, при первом открытии → `crm_stage='opened'`
- **Threading**: IMAP-watcher парсит `In-Reply-To`/`References` → находит proposal по `outbox_message_id` → `crm_stage='replied'`
- **8 готовых палитр** B2B (Че, B2B классика, Изумруд, Бургунди, Графит, Стальной, Тёплый беж, Виноград)
- **5 семейств шрифтов** в PDF (DejaVu, Liberation Sans/Serif, Noto Sans/Serif)
- **TG push** при отправке КП → inline-кнопки «Выиграно/Отказ»

## 🟢 Презентации v2 (presentations)

### Поля PresentationProject
topic / audience / slide_count(3-40) / extra_info / color_scheme(legacy) / **bg_color / text_color / accent_color / title_color** (HEX, кастомные приоритетнее) / **client_site_url / client_site_ctx** (парсинг сайта для стиля под клиента) / **custom_charts** (JSON массив явных графиков) / slides_json / pptx_path / html_preview / pdf_path

### Pipeline
1. Парсинг сайта клиента → site_ctx (опц.)
2. **Vision-описания** загруженных фото через Claude Haiku (≤8 картинок)
3. Custom charts → подаются в prompt + страховка добавления если AI забыл
4. Claude prompt v3 → JSON со слайдами 7 типов: title/section/content/two_column/chart/quote/cta
5. `_resolve_colors_for_project` → кастомная палитра (через `_build_custom_palette` из 4 hex → авто panel/accent2/muted)
6. **HTML preview** (карусель + SVG-графики bar/line/pie)
7. **PPTX** (`build_pptx_with_palette`) — нативные графики через python-pptx + speaker notes + картинки
8. **PDF** (xhtml2pdf, landscape A4)

### Цены
- **Маржа ×7** внутри (`presentation.margin_pct=700`) — но **НЕ показывается в UI**
- В UI динамическая «≈ X-Y ₽» (зависит от слайдов / extra_info / images_count / has_site)
- Endpoint `/presentations/estimate-cost` принимает {slide_count, extra_info_len, images_count, has_site}

### ТЗ-визард (`/presentations/brief-assist`)
Юзер пишет идею в свободной форме → Claude Haiku → JSON {topic, audience, extra_info, suggested_slide_count, structure_hint, questions} → кнопка «Применить» подставляет в форму.

### UX
- Color picker × 4 (фон/акцент/заголовки/текст) + 4 быстрых пресета (Тёмная/Светлая/Корп/Белая)
- URL сайта клиента → AI считывает стиль/тон, адаптирует лексику
- Vision: AI описывает каждое фото 1 предложением
- Графики: inline-форма kind/title/labels/values
- 3 формата: PPTX / HTML / PDF

## 🟢 Три приложения (PWA + Desktop + TG)

### PWA (мобильное и десктоп)
- `views/manifest.json` + `views/sw.js` + `views/icon.svg` (раздаются через main.py)
- `display:standalone` + `display_override:[window-controls-overlay,...]`
- Shortcuts: Чат / Боты / КП / Сайты — в меню приложения
- Service worker: static cache-first, HTML network-first + offline-fallback, API не кэшируется
- В кабинете → вкладка «📲 Приложение» — кросс-платформенная инструкция (iOS/Android/Mac/Windows)

### Desktop standalone-режим
- `@media (display-mode: standalone|window-controls-overlay)` в index.html включает `.app-titlebar.standalone-only` — draggable titlebar (`-webkit-app-region: drag`) с эмодзи и градиентом

### TG management-бот
- Отдельный бот (env `TG_MGMT_BOT_TOKEN` + `TG_MGMT_BOT_USERNAME`)
- Webhook: `POST /webhook/tg-mgmt/{secret}` (path_secret + header secret)
- Привязка: 6-знач код 10 мин TTL → `/link XXXXXX` или deep-link
- Команды: `/start /link /unlink /me /stats /menu`
- Inline-меню: профиль, стата 7 дней, последние КП/заявки, toggle подписок
- **Push-уведомления** через `notify_user(user_id, text, kind)`:
  - При отправке КП → push с inline-кнопками «Выиграно/Отказ»
  - При новой заявке через `save_record`
  - Респектит `User.tg_notify_*` флаги
- Setup: `setWebhook` через Telegram API с `secret_token=<derived from TG_MGMT_BOT_TOKEN>`

## AI провайдеры

### OpenAI/Anthropic/Grok
Прямые ключи в БД `api_keys`. Подгружаются на старте через `_load_all_apikeys_from_db`.

### Google (Imagen + Veo) — ВАЖНО
- Хостинг прода в NL — Google AI Studio блочит ASN
- Решение: прокси `GOOGLE_HTTPS_PROXY` в env
- Используется ТОЛЬКО для Google-вызовов
- Модели: Imagen 4 fast/std/ultra, Veo 2/3.0/3.0-fast/3.1 + audio + i2v

## Ноды workflow (chatbot_engine.py)
**Триггеры:** trigger_tg, trigger_vk, trigger_avito, trigger_max, trigger_webhook, **trigger_imap**, trigger_schedule, trigger_manual

**AI:** node_gpt, node_claude, node_gemini, node_grok, prompt, orchestrator

**Логика:** condition, switch, role_switch, delay, http_request, code_python (sandbox)

**Storage:** storage_get, storage_set, storage_push

**KB (RAG):** kb_add, kb_search_file, kb_search, kb_rag

**Output:** output_tg, output_tg_buttons, output_tg_file, output_tg_audio, output_vk, output_max, output_max_buttons, output_save, output_hook

**Богатый UX:** request_contact, request_location, output_photo, edit_message, chat_action_typing

**Универсальный:** save_record (lead/booking/order/quiz/ticket/subscriber/proposal_sent)

**Мета:** bot_constructor, **🟢 auto_proposal** (генерит КП из IMAP-письма + опц. SMTP-ответ + TG approval flow + whitelist по доменам/keywords)

## Аудит-лог
Таблица `action_logs` — все значимые действия пишутся через `server.audit_log.log_action()`.

**Что логируется:** auth.* / payment.* / ai.* / **proposal.*** (created/generated/manual_sent/auto_sent/section_edited/html_edited/version_restored/stage_changed/duplicated/client_replied/public_opened/...) / record.created / asset.* / proposal.pricelist_*

**Эндпоинты:**
- `GET /admin/actions?since_hours=72&limit=500` — JSON
- `GET /admin/actions.txt?since_hours=72&limit=2000` — plain text для чата
- `GET /admin/actions.jsonl` — JSONL

**Cleanup:** info — 30 дней, error/critical — 90 дней, forensic (auth/payment/record) — 365 дней

## Безопасность

### Network/Infra
- ✅ HTTPS-only + HSTS (1 год), HTTP→HTTPS redirect 301
- ✅ UFW активен (только 22/80/443)
- ✅ uvicorn слушает 127.0.0.1 (был 0.0.0.0)
- ✅ fail2ban на SSH
- ✅ nginx server_tokens off
- ✅ apt auto updates

### Auth
- ✅ bcrypt + timing-safe verify
- ✅ Password policy: 10+ симв, 2 класса, чёрный список
- ✅ JWT в httpOnly cookie + CSRF (double-submit, hmac.compare_digest)
- ✅ Login alert email при новом IP (User.last_login_ip)
- ✅ Refresh token rotation
- ✅ Audience/issuer claims

### Application
- ✅ SQLAlchemy ORM (все запросы препаред)
- ✅ CSRF middleware
- ✅ IDOR: `filter_by(user_id=user.id)` везде
- ✅ Path traversal protection: ZIP сайтов + storage cleanup используют `Path.resolve().relative_to(uploads_root)`
- ✅ CSV-injection: `_csv_safe()` префиксит `=+-@\t\r` апострофом
- ✅ CSV-import: верхняя граница 1 млрд ₽
- ✅ `_SecretFilter` на root-handler — ловит секреты во всех логгерах
- ✅ Storage billing race fix
- ✅ Welcome / referral бонусы — atomic gates
- ✅ UNIQUE-индексы на yookassa_payment_id
- ✅ Worker_lock fail-CLOSED
- ✅ TG webhook требует `X-Telegram-Bot-Api-Secret-Token`
- ✅ MAX webhook требует `?secret=`
- ✅ YooKassa webhook HARD-FAIL без `YOOKASSA_SECRET_KEY`
- ✅ OAuth state-параметр для CSRF
- ✅ Iframe sandbox без allow-same-origin (sites preview)
- ✅ HKDF для Fernet-ключа
- ✅ SVG sanitization
- ✅ http_request нода: двойной DNS + CIDR блок-лист
- ✅ code_python sandbox: whitelist AST + wallclock timeout
- ✅ Native dialogs убраны → custom modals (aiAlert/aiConfirm/aiPrompt)

### Dependencies
- ✅ python-jose 3.4.0, multipart 0.0.26, dotenv 1.2.2, markdown 3.8.1
- ⚠️ starlette 0.37.2 (CVE-2024-47874, 2025-54121) — pinned в FastAPI 0.111
- ⚠️ xhtml2pdf 0.2.16 (CVE-2024-25885) — нет fix-версии

## Production-readiness
- ✅ Sentry (guarded `SENTRY_DSN`)
- ✅ Structured logs (`STRUCTURED_LOGS=1` → JSON)
- ✅ X-Request-ID middleware
- ✅ Auto-backup chat.db с PRAGMA integrity_check, retention 14 дней
- ✅ Audit log с эшелонированной retention
- ✅ Idempotency-Key в /message
- ✅ CI workflow с pytest + ruff + pip-audit

## Инфра
- Прод: `root@194.104.9.219` (Дронтен, NL, Clouvider), путь `/root/AI-CHE`
- venv: `/root/AI-CHE/venv/bin/python`
- env: `/root/AI-CHE/.env` — все API-ключи, `JWT_SECRET`, `YOOKASSA_*`, `APP_URL=https://aiche.ru`, **`TG_MGMT_BOT_TOKEN`** (опц.), **`TG_MGMT_BOT_USERNAME`**, `GOOGLE_HTTPS_PROXY`
- Шрифты: установлены `fonts-liberation` + `fonts-noto-core` через apt
- Деплой: `git pull origin main && systemctl restart ai-che`
- БД: SQLite `chat.db` + WAL. Бэкапы автоматом в `/root/AI-CHE/backups/chat.db.YYYY-MM-DD`

## Правила разработки
- Ответы на русском
- Комментарии минимальные, только где неочевидно
- API-ключи в БД `api_keys`, в env не хардкодим
- **Биллинг:** только через `server.billing.deduct_strict/deduct_atomic/credit_atomic`. Все суммы — копейки
- **Сессии БД вне FastAPI Depends:** только через `with db_session() as db:`
- **Секреты в БД:** через `EncryptedString`
- **Миграции схемы:** `LIGHTWEIGHT_MIGRATIONS` в `server/db.py`
- **Webhooks:** TG/MAX через secret-token; ЮKassa через HMAC
- **Картинки** в `/uploads/` (КОРЕНЬ проекта)
- **Логи действий:** `log_action(...)` в новые endpoint'ы
- **Native dialogs запрещены** — везде `aiAlert/aiConfirm/aiPrompt`
- **Деплой:** `git push origin main && ssh ... git pull && systemctl restart ai-che`. NEVER `db.drop_all()`, NEVER reset api_keys/users/transactions

## Тесты
`pytest tests/` — **89 проходят** (актуально на 2026-04-28).
- `tests/test_api.py` — auth, chat, chatbots CRUD, security, webhooks, CookieAuth, YooKassaWebhookSignature
- `tests/test_billing.py` — atomic gates, race conditions, widget Origin
- `tests/test_critical_paths.py` — promo, conversation, try_with_keys, secrets HKDF, edit-block refund, **TestUserApiKeys** (3), **TestBotPriceList** (3)
- `tests/conftest.py` — DEV_MODE + APP_ENV=dev + JWT_SECRET + apply migrations + `_clear_cookies_and_rl`

```bash
cd .claude/worktrees/eloquent-carson-885bc0/
DEV_MODE=true APP_ENV=dev JWT_SECRET=test-jwt-secret-32-chars-long-yes \
ALLOWED_ORIGINS=http://localhost:8000 \
python -m pytest tests/ --tb=line
```

## Деплой workflow
```bash
git push origin claude/eloquent-carson-885bc0:main

HOME="C:\\Users\\Денис" ssh -i "C:\\Users\\Денис\\.ssh\\id_ed25519" \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  root@194.104.9.219 \
  "cd /root/AI-CHE && git pull origin main && \
   systemctl restart ai-che && systemctl is-active ai-che"
```

## Свежие коммиты (топ-15 на 2026-04-28)
- `d1d8e41` — feat(presentations): color picker + сайт клиента + vision + графики + ТЗ-визард
- `03d842b` — feat(presentations): полная переработка модуля — слайды, графики, PPTX
- `8714682` — feat(apps): три приложения — PWA + Desktop standalone + TG management bot
- `ea7487c` — feat(proposals): JSON-first генерация — стабильное оформление + 4 пресета
- `ba30acf` — feat(proposals): свой раздел прайсов в КП — независимый от ботов
- `2bbddd9` — fix(editor): полная правка текста + замена картинки + понятные кнопки
- `5f1465e` — fix(ui): WYSIWYG-правка КП + 14 sync->async функций
- `b241bba` — feat(proposals): B.5-B.8 + C.9-C.11 + D.12-D.13 — CRM/версии/threading/шрифты
- `e93ec13` — feat(proposals): A.1-A.4 — edit-режим, AI-правка секций, валидация, версии
- `7f5c4d6` — feat(proposals): улучшения КП — отправка email, лучший шаблон, prompt v2
- `3dc580b` — fix(pdf+ui): кириллица в PDF + кастомные модалки
- `e24d96a` — feat(proposals): email-orchestration — нода auto_proposal + шаблон IMAP→SMTP
- `4e00538` — feat(proposals): генерация PDF + парсинг сайта клиента + UI с превью
- `1657f0a` — feat(proposals): отдельный модуль КП + бренды + контекст клиента
- `99377aa` — deps: фикс 11 CVE — python-jose, multipart, dotenv, markdown

Полный лог: `git log --oneline -30`. Развёрнутый разбор спринтов — `HANDOVER.md`.
