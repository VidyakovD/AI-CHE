# AI Студия Че — CLAUDE.md

Этот файл — first-class контекст для AI-ассистента. Если зашёл в проект **в новом чате** — читай целиком, потом смотри `HANDOVER.md` (свежие изменения) и `TODO_NEXT.md` (что в работе).

## Что делать при каждом новом запуске
1. **Прочитай этот файл целиком** — здесь актуальное состояние.
2. Прочитай `HANDOVER.md` — детальная история последних спринтов.
3. Прочитай `TODO_NEXT.md` — что в очереди + действия юзера.
4. `git log --oneline -25` — последние коммиты.
5. Если нужны live-логи прода — запроси у юзера дамп `/admin/actions.txt?since_hours=72`.

## Краткое описание
**B2B AI-платформа для предпринимателей.** Веб-приложение FastAPI + HTML SPA + PWA + Public REST API + Webhooks + CRM-интеграции.

Простой пользовательский гайд (для самих юзеров): **`USER_GUIDE.md`** — 1100 строк, 19 разделов, без жаргона.

## Главные продукты (на 2026-05-05)

- **Чат с AI** — GPT-4o / Claude Sonnet+Opus+Haiku / Perplexity / Grok / GPT-image / Imagen 4 / Veo 3 + **голосовой ввод (Whisper) и TTS (6 голосов OpenAI)**
- **Бизнес-решения PRO** (multi-agent orchestra) — **8 пилотов** параллельно через несколько агентов с web_search / browse_url / file_extract / vision: Конкурентный анализ, SWOT, Контент-план, Аудит лендинга, Юр.договор, Аудит соцсети, Финаудит Excel, Холодная email-рассылка
- **Чат-боты** в 6 каналах: TG / VK / Avito / MAX / Widget / **WhatsApp (Wazzup24)** + 7 шаблонов + прайс-лист с semantic search + RAG БЗ
- **AI-агенты** с workflow-конструктором (50+ блоков) + 25+ специализированных ролей
- **Сайты под ключ** — 1500/1990 ₽, фоновая генерация, WYSIWYG, sandbox-iframe + public_token
- **КП (Proposals)** — `/proposals.html`: 4 пресета + 4 шапки, бренды, прайсы, JSON-first генерация, WYSIWYG, AI-правка секций, версии, CRM-стадии, email-orchestrator, **электронная подпись клиентом (canvas + audit-trail + auto-CRM `won`)**
- **Презентации v2** — `/presentations.html`: PPTX/HTML/PDF, color picker, vision-анализ фото, графики, ТЗ-визард
- **Marketplace ботов** — публикация шаблонов с revenue-split 70/30 + админ-модерация (`/marketplace.html`)
- **Public API** (`/api.html`) — Bearer-токены + scope-проверка + Webhooks (7 событий с HMAC-подписью + auto-disable) + полная документация
- **CRM-интеграции** — Bitrix24/amoCRM/generic webhook native UI с маппингом полей, тестовая кнопка
- **Cron-расписания orchestra** — превращает разовые покупки в подписку («каждый понедельник запусти SWOT»)
- **Платежи ЮKassa** с **54-ФЗ чеком** через ОФД
- **Админка** + аудит-лог + pricing_config + **2FA для админов (TOTP)**
- **Свои API-ключи юзера** (-80% скидка)
- **Storage** (50 ₽/мес за 100 МБ) с биллингом для StoredAsset + KnowledgeFile
- **PWA + Desktop standalone + TG management-бот + Web Push (VAPID)**
- **RAG база знаний** — KnowledgeFile с embeddings + storage-биллинг

## Стек

- **Backend:** Python 3.10 (на новом сервере) / 3.12 (NL legacy), FastAPI 0.111, SQLAlchemy 2.0, SQLite (`chat.db`)
- **Frontend:** HTML / Tailwind CDN / vanilla JS (SPA)
- **AI:**
  - OpenAI (gpt-4o, gpt-image-1, dall-e-3, whisper-1, tts-1)
  - Anthropic (claude-sonnet-4-6, claude-opus-4-1, claude-haiku-4) — streaming через AsyncAnthropic
  - Grok (xai), Perplexity (sonar)
  - Google AI Studio через прокси (Imagen 4, Veo 2/3, Gemini)
- **PDF:** xhtml2pdf + DejaVu Sans + Liberation Sans/Serif + Noto Sans/Serif
- **DOCX:** python-docx 1.1.2 / **XLSX:** openpyxl / **PPTX:** python-pptx
- **2FA:** pyotp 2.9.0 (TOTP)
- **Push:** pywebpush 2.0.0 (VAPID)
- **Шифрование:** AES-256-GCM (cryptography), HKDF от JWT_SECRET
- **Авторизация:** JWT в httpOnly cookie + CSRF (double-submit) + refresh single-use rotation + опционально 2FA для админов

## 🆕 Инфраструктура (на 2026-05-05 после миграции)

### PROD-сервер (новый)
- **IP:** `193.187.92.147`
- **Локация:** Москва, RU 🇷🇺
- **Провайдер:** HOSTKEY (AS50867)
- **OS:** Ubuntu 22.04.5 LTS
- **Hardware:** 2 vCPU / 4 ГБ RAM / 60 ГБ NVMe / 3 ТБ трафика
- **Python:** 3.10.12 (системный)
- **Путь:** `/root/AI-CHE`
- **venv:** `/root/AI-CHE/venv/bin/python`
- **Сервис:** `systemctl status ai-che` (4 worker'а на 127.0.0.1:8000)
- **nginx:** `/etc/nginx/sites-available/aiche.ru` (HTTP пока, SSL после DNS)
- **UFW:** только 22/80/443
- **fail2ban:** на SSH (5 fails / 10 min → ban 1h)
- **Пароль root:** хранится в `/root/.aiche-server-password` (chmod 400)
- **SSH-ключ Claude:** добавлен в `~/.ssh/authorized_keys` (через paramiko в первой миграции)

### Старый сервер (legacy, после переключения DNS можно выключить)
- IP: `194.104.9.219`
- Локация: Дронтен, Нидерланды (Clouvider, AS41745)
- Python 3.12 / venv `/root/AI-CHE/venv`
- **Состояние:** работает параллельно, держим как backup до подтверждения миграции

### AI-прокси (для всех 5 провайдеров)
- **URL:** `http://GYDMQ9mG:L1N8SBFc@156.233.103.240:62134` (передал юзер, лежит в `/root/AI-CHE/.env`)
- **ENV:** `AI_HTTPS_PROXY` (общий fallback) + `GOOGLE_HTTPS_PROXY` (legacy специфичный)
- Helper в `server/ai.py`:
  - `_ai_proxy(provider)` — provider-specific → AI_HTTPS_PROXY → None
  - `_openai_client_kwargs(provider)` — http_client с прокси для SDK
- Применено в: OpenAI (chat + images), Anthropic, Google (был), Grok, Perplexity

### DNS
- **Регистратор:** см. в env / у юзера (REG.RU?)
- **A-запись `aiche.ru`** была `194.104.9.219`, юзер переключил → новый IP
- **Распространение DNS:** в момент написания этого файла ещё идёт (TTL до 30 минут)
- **TTL:** должен быть низкий (300 сек) для быстрого отката

### SSL (TODO после полного DNS)
- certbot 1.21.0 + python-certbot-nginx уже установлены
- Команда: `certbot --nginx -d aiche.ru -d www.aiche.ru --email vidyakov@obsidian.ai --agree-tos --no-eff-email`
- После SSL заменить `nginx.conf` на `nginx-ssl.conf`: `sed 's|__DOMAIN__|aiche.ru|g' /root/AI-CHE/deploy/nginx-ssl.conf > /etc/nginx/sites-available/aiche.ru && nginx -t && systemctl reload nginx`

## Структура файлов

### Backend (server/)
| Файл | Что |
|---|---|
| `main.py` | Entry point, роутеры, CSP, middleware (rate-limit/CSRF/request-id), PWA endpoints, **`/p/{token}` HTML-страница КП с canvas-подписью**, `/p/{token}/pdf`, **`/p/{token}/sign`**, `/s/{token}` Solution, `/qr/{token}`, `/healthz`, race-safe `create_all` |
| `auth.py` | JWT + refresh single-use rotation + revoke_all_refresh_jtis + `_atomic_jtis_update` (race-fix) |
| `db.py` | SQLAlchemy + LIGHTWEIGHT_MIGRATIONS + backfill site public_token |
| `models.py` | ORM: User (+ totp_secret/enabled, notifications_last_seen_at, onboarding_completed), ChatBot, ProposalProject (+ public_token, header_layout), **ProposalSignature**, SolutionRun (+ stages_state, attachments_json, public_token, user_mark), `SolutionRunTemplate`, `BotMarketplaceListing/Install`, `ApiToken`, **`ApiWebhook`**, **`CrmConnection`**, **`OrchestraSchedule`**, **`IdempotencyRecord`** (multi-worker safety), `PushSubscription`, KnowledgeFile (+ last_billed_at) |
| `billing.py` | Атомарные списания + бонусы (deduct_strict/atomic, credit_atomic) |
| `security.py` | Rate-limit, validate_password, tg_webhook_secret, _csv_safe, _SecretFilter, ADMIN_EMAILS |
| `pricing.py` | Динамические цены через `pricing_config` (DEFAULTS) |
| `scheduler.py` | Cron-воркеры: scheduler/apikey/pdf/db_backup (AES-GCM)/conv/audit/storage-billing/**orchestra_schedules**/**idempotency_cleanup** |
| `ai.py` | MODEL_REGISTRY, generate_response, _SecretFilter, **_ai_proxy()**, **_openai_client_kwargs()** |
| `chatbot_engine.py` | Движок ботов + workflow + ноды + send_telegram/vk/max/whatsapp/avito/site + send_whatsapp + record.created → push + webhook + **CRM dispatch** |
| `bot_templates.py` | 7 шаблонов |
| `pdf_builder.py` | html_to_pdf_bytes + 5 семейств шрифтов + markdown_to_pdf |
| `docx_builder.py` | Markdown→DOCX |
| `xlsx_builder.py` | Markdown→XLSX |
| `solutions_orchestra.py` | Multi-agent runtime: 7 stage-типов, streaming AsyncAnthropic, restage(), pub/sub для SSE, hooks (push/webhook) |
| `push.py` | Web Push (VAPID) |
| `webhooks.py` | **NEW** — Public API webhooks dispatcher: HMAC-SHA256 подпись, fire-and-forget threading, auto-disable после 10 ошибок |
| **`crm.py`** | **NEW** — CRM-интеграции: dispatch_record_to_crm, mapping для Bitrix24/amoCRM/generic, fire-and-forget |
| `proposal_builder.py` | КП: parse_client_site, JSON-first prompt v3, _render_proposal_json → HTML, _PRESET_CSS (4 пресета), 4 header_layout, edit_section, bleach-санитизация |
| `presentation_builder.py` | Презентации v2 |
| `tg_management.py` | TG-бот управления |
| `email_service.py` | SMTP + send_with_attachment + login alerts |
| `email_imap.py` | IMAP-trigger + email threading |
| `secrets_crypto.py` | Шифрование через HKDF(JWT_SECRET) |
| `agent_runner.py` | Оркестратор AI-агентов + **prompt-injection защита в tool_run_llm** + tool_browse_url (SSRF-safe) + tool_web_search + tool_run_llm + tool_generate_image |

### Routes (server/routes/)
| Роут | Что |
|---|---|
| `auth.py` | Регистрация/login + **2FA для админов (TOTP)** + refresh single-use + revoke + marketing_consent |
| `payments.py` | YooKassa init + webhook (HMAC) + 54-ФЗ receipt |
| `chat.py` | `/message` /upload, auto-refund, **DB-based idempotency** (UNIQUE user_id+key) |
| `sites.py` | Сайты + ZIP + sandbox-iframe + public_token + push/webhook hooks |
| `chatbots.py` | CRUD + 7 шаблонов |
| `assets.py` | Storage |
| `user_apikeys.py` | Свои API-ключи |
| `user.py` | Кабинет + transactions.csv + TG-link + marketing-consent + **/notifications/recent + /onboarding + /recent-objects + push** |
| `proposals.py` | brands CRUD, projects CRUD, generate, public-link, send-email, stage, price-lists, signature в `_project_to_dict` |
| `presentations.py` | generate, estimate-cost, pptx, pdf, brief-assist |
| `webhook.py` | TG/VK/Avito/MAX/wazzup webhooks + tg-mgmt |
| `widget.py` | Виджет на сайт + WS Origin-whitelist |
| `solutions.py` | 30 plain + 8 orchestra-пилотов: start (возвращает run_id!), stream (SSE), restage, save-template/run-template, share, reaction, docx/xlsx, **start-compare/compare/{group}**, auto-flag dedup через ActionLog |
| `marketplace.py` | Публикация ботов + каталог + install (70/30, anti-pump для платных) + review + admin-модерация |
| `public_api.py` | mgmt: /api-tokens CRUD + **/api-tokens/webhooks CRUD + /test**; api: /api/v1/me (is_verified check), /api/v1/proposals/generate+get; atomic UPDATE requests_count |
| **`schedules.py`** | **NEW** — `/orchestra-schedules/*` CRUD + toggle, _calc_next_run для 8 frequency-пресетов |
| **`crm.py`** | **NEW** — `/crm/connections/*` CRUD + test + providers (Bitrix24/amoCRM/webhook) |
| `assistant.py` | AI-помощник по разделам с feedback |
| `qr_login.py` | QR-логин |
| `mobile.py` | Lite-режим + voice/parse + voice/transcribe + **voice/tts (TTS, 6 голосов OpenAI)** |
| `knowledge.py` | RAG база знаний (`_abs_path` defense-in-depth) |
| `admin.py` | + **/admin/2fa/setup/enable/disable/status (TOTP)** + **/admin/reencrypt-secrets** + admin/listings/{id}/approve/reject |

### Frontend (views/)
| Файл | Что |
|---|---|
| `index.html` | Главная: чат + бизнес-решения + кабинет + **welcome quick-action chips + блок «Недавнее» + welcome-tour 4 шага + 🎤 голосовой ввод + 🔊 TTS-кнопка + поле 2FA-кода + ссылки на /marketplace.html и /api.html в sidebar** |
| `admin.html` | + **🛍 Marketplace модерация + 🔐 Безопасность 2FA** |
| `agents.html` | Workflow-конструктор |
| `chatbots.html` | + **📤 Опубликовать в Marketplace + понятные подписи на 9 кнопках** |
| **`marketplace.html`** | **NEW** — каталог + публикация + мои публикации |
| **`api.html`** | **NEW** — Public API: 🔑 Токены / 🔗 Webhooks / 📞 CRM / 📖 Docs |
| `proposals.html` | + блок «Подписано клиентом» в карточке + auto-save черновика + cost-hint под кнопкой + drag-n-drop CSV прайса |
| `presentations.html`, `sites.html` | как раньше + toast/aiAlertError |
| `terms.html`, `qr_confirm.html` | без изменений |
| `mobile.html`, `workflow.html` | без изменений |
| `icons.js` | **+1700 строк новых helpers**: aiBalance (sticky pill), aiNotifRefresh (колокольчик), aiTour (welcome-tour), aiDraft (auto-save), aiToast (non-blocking), aiSkeleton (loaders), aiCmdPalette (Ctrl+K), aiVoice (Whisper+TTS), aiAlertError, humanizeError, aiDragDrop, NODE_TYPE_LABELS (90+ маппингов), aiCostHint, touch-targets fix, **`/auth/me` для balance** |
| `manifest.json`, `sw.js`, `icon.svg` | PWA |
| `knowledge-ui.js` | UI RAG |

### Скрипты (scripts/)
| Файл | Что |
|---|---|
| **`migrate_export.sh`** | Экспорт данных со старого: chat.db + uploads + .env + ключи в tar.gz |
| **`migrate_setup.sh`** | Первичная настройка нового: пакеты, venv, UFW, fail2ban, systemd, nginx |
| **`migrate_import.sh`** | Распаковка архива на новом сервере с rollback-бэкапом + integrity-check |

### Templates (deploy/)
| Файл | Что |
|---|---|
| **`ai-che.service`** | Production systemd unit (4 workers, journal logs, минимум hardening) |
| **`nginx.conf`** | HTTP-only заглушка под `__DOMAIN__` |
| **`nginx-ssl.conf`** | Production HTTPS с TLSv1.2/1.3, HSTS, CSP, кэш статики, SSE |

### Документация (корень)
| Файл | Что |
|---|---|
| `CLAUDE.md` | (этот файл) |
| `HANDOVER.md` | Подробная история всех спринтов с диффом |
| `TODO_NEXT.md` | Что делать в следующих сессиях + действия юзера |
| **`USER_GUIDE.md`** | **NEW** ~1100 строк, 19 разделов простым языком для юзеров |
| **`MIGRATION.md`** | Полный гайд переноса между серверами + EU-прокси setup |

## Запуск (local dev)
```bash
DEV_MODE=true APP_ENV=dev JWT_SECRET=test-jwt-secret-32-chars-long-yes \
ALLOWED_ORIGINS=http://localhost:8000 \
python -m uvicorn main:app --reload --port 8001
```

## Деплой (новый сервер)
```bash
# С локальной машины
HOME="C:\\Users\\Денис" ssh -i 'C:\\Users\\Денис\\.ssh\\id_ed25519' \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  root@193.187.92.147 \
  "cd /root/AI-CHE && git pull origin main && systemctl restart ai-che"
```

⚠️ **uvicorn слушает только 127.0.0.1**. Внешний 8000 закрыт UFW. Доступ только через nginx.

## Деньги — РУБЛИ + КОПЕЙКИ
- Баланс юзера = `User.tokens_balance` в **копейках** (1 ₽ = 100 коп)
- Поля называются `tokens_balance`, `tokens_delta`, `ch_per_1k_*` — это legacy имена, **значение = копейки**
- UI: `window.fmtRub(kop)` → "X.XX ₽"

### Тарифы (на 2026-05-05, все цены в БД `pricing_config`)
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
| КП первый раз | 50 ₽ | `proposal.create=5000` |
| КП перегенерация | 5 ₽ | `proposal.edit=500` |
| КП AI-правка секции | real × 5 | `ai.improve_margin_pct=500` |
| Презентация | real × 7 (margin внутри, в UI не показываем) | `presentation.margin_pct=700` |
| Бизнес-решение orchestra | real × 5 за каждый llm-stage | `ai.improve_margin_pct=500` |
| **Голосовой ввод (Whisper)** | 5 ₽ за запрос | фикс |
| **TTS (озвучка)** | 2.25 ₽ / 1000 симв (минимум 50 коп) | фикс |
| Marketplace install | price_kop листинга (70% автору) | per listing |

## Multi-agent Orchestra (PRO решения)

`Solution.orchestra_json` содержит JSON-граф stage'ов. **10 типов stage:**
- `web_search`, `browse_url`, `parallel_browse`, `extract_urls`
- `llm`, `synthesize`, `parallel_llm`
- `file_extract` (PDF/DOCX/XLSX), `vision_describe` (картинка через Claude Haiku), `generate_image` (DALL-E)

**8 orchestra-пилотов** (см. `scripts/seed_orchestra_solutions.py`):
1. **Конкурентный анализ ниши** (150 ₽)
2. **Полный SWOT-анализ** (150 ₽)
3. **Контент-план месяц** (200 ₽)
4. **Аудит лендинга** (250 ₽)
5. **Юр. проверка договора** (350 ₽)
6. **Аудит соцсети канала** (250 ₽)
7. **Финансовый аудит Excel** (300 ₽)
8. **Холодная email-рассылка** (250 ₽)

**UX-фичи orchestra:**
- Live-progress через SSE с heartbeat 1s
- ↻ Re-run отдельного stage с extra_instruction
- ⭐ Save as template
- 🔗 Share через public_token → `/s/{token}` без auth
- 📄 PDF / 📝 DOCX / 📊 XLSX экспорты
- 👍/👎/💡 reaction + auto-flagging (3+ 👎 за 7 дней → email)
- 🔬 Compare: запустить на 2-3 моделях параллельно
- **📅 Расписания** — `/orchestra-schedules` (8 frequency-пресетов, worker раз в минуту)

## Webhook'и Public API (`/api-tokens/webhooks`)

7 событий с HMAC-SHA256 подписью `X-Aiche-Signature: sha256=<hex>`:
- `proposal.opened` / `proposal.sent` / `proposal.signed`
- `record.created`
- `solution.done`
- `site.done` / `site.failed`

10 ошибок подряд → auto-disable. SSRF-защита (no localhost/private CIDR).

## CRM (`/crm/connections`)

Native поддержка:
- **Bitrix24** — incoming webhook `crm.lead.add`, mapping PHONE/EMAIL → массив объектов
- **amoCRM** — Webhooks "Свой URL"
- **Generic webhook** — для Zapier/Make/N8N

При `record.created` → fire-and-forget POST в каждую active интеграцию (через threading). 10 ошибок → auto-disable. UI на `/api.html` → вкладка «📞 CRM».

## Аудит-лог
Таблица `action_logs`. Логируется: auth.* / payment.* / ai.* / proposal.* / **proposal.signed** / solution.* / orchestra_schedule.* / marketplace.* / api_token.* / **api_webhook.*** / **crm.connection_*** / admin.* / record.created / asset.*

Endpoints: `/admin/actions(.txt|.jsonl)`. Cleanup retention в scheduler.

## Безопасность

### Network/Infra
- ✅ HTTPS-only + HSTS (после SSL)
- ✅ UFW активен (только 22/80/443)
- ✅ uvicorn 127.0.0.1
- ✅ fail2ban + nginx server_tokens off
- ✅ AI_HTTPS_PROXY для всех 5 провайдеров (РФ-сервер не блокирован)

### Auth
- ✅ bcrypt + timing-safe verify + dummy-hash на login
- ✅ Password policy 10+ симв
- ✅ JWT в httpOnly cookie + CSRF (double-submit)
- ✅ **Refresh token rotation single-use** (race-safe `_atomic_jtis_update`)
- ✅ **2FA админки (TOTP)** — `/admin/2fa/setup/enable/disable/status`, поле кода в loginModal
- ✅ Login alert email при новом IP
- ✅ Aud/iss claims (но проверка пока не strict — отложено)

### Application
- ✅ SQLAlchemy ORM везде, CSRF, IDOR-проверки
- ✅ Path traversal protection
- ✅ CSV-injection: `_csv_safe`
- ✅ `_SecretFilter` на root-handler
- ✅ Storage billing race fix (UNION StoredAsset + KnowledgeFile)
- ✅ UNIQUE-индексы на yookassa_payment_id
- ✅ **Multi-worker idempotency** (DB-table `IdempotencyRecord` с UNIQUE(user_id, key))
- ✅ Sites enumeration → `public_token` (~160bit)
- ✅ VK webhook требует `vk_secret` + compare_digest
- ✅ SSRF в agent tool_browse_url + presentation_builder + Public API webhooks + CRM (DNS rebinding + scheme whitelist + private CIDR блок)
- ✅ bleach-санитизация generated_html КП
- ✅ Agent /ws + /stream IDOR-защита
- ✅ TG-link rate-limit + email-alert при привязке
- ✅ http_request нода: двойной DNS + CIDR блок-лист
- ✅ code_python sandbox
- ✅ **Image URL whitelist** (logo/cover/signature: только http/https/data:image/)
- ✅ **Marketplace anti-pump** (нет повторной установки платного листинга)
- ✅ **Public API atomic UPDATE requests_count** (без race в multi-worker)
- ✅ **CSRF cookie+Bearer fix**: при наличии cookie CSRF обязателен независимо от Bearer
- ✅ **Электронная подпись КП**: SHA-256 hash от proposal_id+name+email+ts+sig+ip — невозможно подделать
- ✅ **Prompt-injection защита** в tool_run_llm (обёртка `<user_data>` теги + system-guard)
- ✅ **`/admin/reencrypt-secrets`** для ротации JWT_SECRET без потери EncryptedString-полей

### Compliance (152-ФЗ + 54-ФЗ)
- ✅ AES-256-GCM шифрование DB-бэкапов
- ✅ 54-ФЗ receipt в YooKassa: payment_subject + payment_mode + tax_system_code + vat_code
- ✅ Маркетинговое согласие отдельно от оферты + UI чекбокс
- ✅ Из payment-логов убраны суммы
- ✅ **Сервер в РФ (Москва)** — после миграции с NL
- ⚠ SMTP не настроен → юзеры не получают verification (нужен Unisender/SendPulse)
- ⚠ Регистрация в РКН — задача юзера
- ⚠ Прод-shop ЮKassa — задача юзера

## Production-readiness
- ✅ Sentry (guarded `SENTRY_DSN`)
- ✅ Structured logs (`STRUCTURED_LOGS=1` → JSON)
- ✅ X-Request-ID middleware
- ✅ Auto-backup chat.db с AES-GCM + PRAGMA integrity_check, retention 14 дней
- ✅ Audit log с эшелонированной retention
- ✅ Idempotency-Key в /message (DB-based, multi-worker safe)
- ✅ CI workflow с pytest + ruff + pip-audit
- ✅ **182 теста** проходят на каждом коммите

## Правила разработки
- Ответы на русском
- Комментарии минимальные, только где неочевидно
- API-ключи в БД `api_keys`, в env не хардкодим
- **Биллинг:** только через `server.billing.deduct_strict/deduct_atomic/credit_atomic`. Все суммы — копейки
- **Сессии БД вне FastAPI Depends:** только через `with db_session() as db:`
- **Секреты в БД:** через `EncryptedString`
- **Миграции схемы:** `LIGHTWEIGHT_MIGRATIONS` в `server/db.py` для добавления колонок к существующим таблицам. Для целиком новых таблиц достаточно `Base.metadata.create_all`.
- **Webhooks:** TG/MAX/Wazzup24 через secret-token; ЮKassa через HMAC; Public API webhooks с HMAC-SHA256 в `X-Aiche-Signature`
- **Картинки** в `/uploads/` (КОРЕНЬ проекта)
- **Логи действий:** `log_action(...)` в новые endpoint'ы
- **Native dialogs запрещены** — везде `aiAlert/aiConfirm/aiPrompt`
- **Деплой:** `git push origin main && ssh ... git pull && systemctl restart ai-che`. NEVER `db.drop_all()`, NEVER reset api_keys/users/transactions
- **Public API endpoints должны быть scope-aware** через `authenticate_token(request, db, required_scope=...)` + `is_verified` check
- **Race на multi-worker:** все RMW операции должны быть либо `UPDATE ... SET = + 1`, либо UNIQUE-protected. См. IdempotencyRecord, ApiToken.requests_count.

## Тесты
`pytest tests/` — **182 проходят, 2 skipped** (актуально на 2026-05-05).
- `tests/test_api.py` — auth, chat, chatbots, security, refresh single-use
- `tests/test_billing.py` — atomic gates, race conditions, widget Origin
- `tests/test_critical_paths.py` — promo, conversation, secrets HKDF, edit-block refund, **TestSolutionsOrchestra**, **TestMarketplace**, **TestPublicAPI**
- **`tests/test_new_features.py`** — **+18 новых тестов** (signature/webhook/schedule/2fa/tts/idempotency)
- `tests/test_assistant.py`, `tests/test_knowledge.py`, `tests/test_mobile.py`, `tests/test_qr_login.py`

```bash
DEV_MODE=true APP_ENV=dev JWT_SECRET=test-jwt-secret-32-chars-long-yes \
ALLOWED_ORIGINS=http://localhost:8000 \
python -m pytest tests/ --tb=line
```

## Деплой workflow
```bash
git push origin claude/<branch>:main

HOME="C:\\Users\\Денис" ssh -i "C:\\Users\\Денис\\.ssh\\id_ed25519" \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  root@193.187.92.147 \
  "cd /root/AI-CHE && git pull origin main && \
   systemctl restart ai-che && systemctl is-active ai-che"
```

При добавлении новых orchestra-решений:
```bash
ssh ... "cd /root/AI-CHE && /root/AI-CHE/venv/bin/python scripts/seed_orchestra_solutions.py"
```

## Свежие коммиты (топ-25 на 2026-05-05)
- `6e9fd0a` — feat(ops): toolkit миграции на новый сервер + AI-прокси
- `2784f5a` — feat: workflow-labels + drag-drop + CRM-интеграции + USER_GUIDE.md
- `5cf647b` — fix(security): idempotency через DB + тесты новых модулей + reencrypt endpoint
- `158a96a` — feat(orchestra): cron-планировщик расписаний
- `984d16b` — fix(ux): balance pill дублировал ЛК-кнопку + кнопки бота
- `0a1202b` — feat(voice): Whisper + TTS endpoints
- `04ded59` — feat(security): 2FA админки (TOTP) + prompt-injection защита
- `0a8bdf7` — feat(kp): электронная подпись КП с canvas + audit-trail
- `5619224` — feat(api): Webhooks для Public API + UI + полная документация
- `8589cbd` — feat(marketplace): UI каталога + публикации + модерации
- `55f191d` — fix(ux): balance_kopecks из /auth/me
- `a2c52b8` — fix(ux): aiBalance.refresh использует /auth/me
- `58c2f62` — feat(ux): 6 mid-priority улучшений (push/Esc/Ctrl+K/toast/skeleton/touch)
- `d8ffb61` — feat(ux): 5 quick wins (колокольчик/auto-save/welcome-tour/cost/humanizeError)
- `6f02d1e` — fix(audit): 13 P1/P2/P3 багов
- `802424d` — docs: обновлены CLAUDE.md / HANDOVER.md / TODO_NEXT.md
- `963c365` — fix(startup): catch table-already-exists race
- `75e2462` — feat: сравнение моделей + Marketplace ботов + Public API
- `1126e50` — feat: XLSX/streaming/auto-flag + WhatsApp + Web Push
- `bcc4cf3` — feat(orchestra-pro): re-run + templates + reactions + DOCX
- `5a91f68` — feat(orchestra): глубокий ресерч (file_extract / vision / browse)
- `fa85629` — feat(solutions): multi-agent orchestra v1
- `da5aee6` — feat(ui+docs): UI чекбокс маркетинговой рассылки
- `a2bffc0` — feat(compliance): РФ-чеклист — 152-ФЗ + 54-ФЗ + шифрование бэкапов
- `d90e2f1` — feat(security): refresh-rotation single-use + sites public_token + RAG billing

Полный лог: `git log --oneline -50`. Развёрнутые описания спринтов — `HANDOVER.md`.
