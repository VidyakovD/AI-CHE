# HANDOVER — для нового AI-ассистента

Если ты впервые в этом проекте — после `CLAUDE.md` прочитай этот файл. Тут **состояние на 2026-04-28 после спринтов «КП-конструктор», «Презентации v2» и «Три приложения»**.

---

## 🆕 Спринт «Презентации v2» (2026-04-28, последний)

Юзер: «надо чтобы получались стильные презентации, не привязываемся к стилю сервиса; цвета — на усмотрение пользователя; считывать фото; графики; ТЗ через ИИ; сайт клиента → стиль; форматы PPTX/HTML/PDF; цена не показываем формулу».

### Полная переработка модуля презентаций (`d1d8e41`, `03d842b`)

**Backend (`server/presentation_builder.py` 1061 строк):**
- `_claude_prompt` v3 — JSON со слайдами 7 типов: title/section/content/two_column/chart/quote/cta
- `_render_html_preview_inner` — карусель с навигацией стрелками/точками/keys + SVG-графики bar/line/pie
- `build_pptx_with_palette` — нативный PPTX через python-pptx (chart-объекты, speaker notes, картинки скачиваются)
- `_render_pdf_html` — landscape A4, каждый слайд на странице
- `describe_image_via_claude` — vision-описания через Claude Haiku (≤8 картинок)
- `parse_client_site_for_style` — парсит сайт клиента через `proposal_builder.parse_client_site`
- `_resolve_colors_for_project` — кастомная палитра приоритетнее пресета
- `_build_custom_palette` из 4 hex → авто panel/accent2/muted (через _shift_hex/_lighten_hex)
- `estimate_cost_kop` / `calc_actual_cost_kop` — **margin ×7 внутри** (`presentation.margin_pct=700`), но в UI не показывается

**Расширение модели PresentationProject (миграция через LIGHTWEIGHT):**
- topic / audience / slide_count(3-40) / extra_info
- bg_color / text_color / accent_color / title_color (HEX, кастомные)
- client_site_url / client_site_ctx (парсинг сайта)
- custom_charts (JSON массив явных графиков)
- slides_json / pptx_path / html_preview / pdf_path

**Routes (`server/routes/presentations.py`):**
- POST `/presentations/projects/{id}/generate` — переписан под новый builder
- POST `/presentations/estimate-cost` — динамика (slide_count, extra_info_len, images_count, has_site)
- GET `/presentations/projects/{id}/pptx` — скачать PPTX
- GET `/presentations/projects/{id}/preview-html` — HTML preview в iframe
- GET `/presentations/projects/{id}/pdf` — скачать PDF
- POST `/presentations/brief-assist` — ТЗ-визард через Claude Haiku

**Frontend (`views/presentations.html`):**
- Форма: name + topic + audience + slider 3-30 + extra_info textarea + URL клиента
- Загрузка картинок (multi, до 10)
- 4 color picker (фон/акцент/заголовки/текст) + 4 быстрых пресета (Тёмная/Светлая/Корп/Белая)
- Графики: inline-форма (kind/title/labels/values)
- Кнопка «✨ AI-помощник по ТЗ» → отдельная модалка с brief-assist
- Динамическая цена «≈ X-Y ₽» (debounced, без формулы)
- После генерации: iframe-preview + кнопки PPTX/PDF/HTML

### python-pptx
Установлен на проде через `requirements.txt` + apt.

### Шрифты для PDF
Установлены `fonts-liberation` и `fonts-noto-core` через apt. `pdf_builder.py:resolve_pdf_font` маппит web-имя → доступный TTF (5 семейств: DejaVu, Liberation Sans/Serif, Noto Sans/Serif).

---

## Спринт «Три приложения» (2026-04-28)

Юзер: «хочу 3 приложения и связать: веб + мобильное (через сохранение на рабочий стол) + десктоп (на компе ещё прикольнее) + управление через TG-бот».

### PWA (мобильное + десктоп) (`8714682`)

**Файлы:**
- `views/manifest.json` — name/icons/start_url/scope/shortcuts (Чат/Боты/КП/Сайты), display:standalone, display_override:[window-controls-overlay,...]
- `views/sw.js` — service worker:
  - Static cache-first (icon/manifest/icons.js)
  - HTML network-first с offline-fallback (страница «Нет интернета» в фирменном стиле)
  - API НЕ кэшируется
  - Push-handler (для будущих native push)
- `views/icon.svg` — стилизованная Ч в фирменных цветах (maskable)
- `main.py` — endpoints `/manifest.json`, `/sw.js`, `/icon.svg`, `/favicon.ico` с правильными MIME + `Service-Worker-Allowed: /`

**`views/icons.js`:**
- Авто-установка PWA-тегов в `<head>` каждой страницы (link rel=manifest, theme-color, apple-touch-icon, apple-mobile-web-app-*)
- Регистрация SW на load (только https://)
- Перехват `beforeinstallprompt` → `window.aiShowInstall()` с кросс-платформенной инструкцией
- `window.aiCanInstall()`, `window.aiIsInstalled()` для UI

**В кабинете → новая вкладка «📲 Приложение»** с инструкциями для iOS/Android/Mac/Windows.

### Desktop standalone-режим

В `views/index.html`:
```html
<div class="app-titlebar standalone-only">🤖 AI Студия Че Desktop App</div>
```

```css
@media (display-mode: standalone), (display-mode: window-controls-overlay) {
  body { padding-top: env(titlebar-area-height, 0); }
  .app-titlebar { -webkit-app-region: drag; ... }
}
```

В обычном браузере titlebar скрыт (`@media not all and (display-mode: standalone)`).

### TG Management-бот (`server/tg_management.py` 513 строк)

**Отдельный бот** для управления АГЕНТАМИ (не клиентский). Регистрируется через @BotFather, токен в env `TG_MGMT_BOT_TOKEN` + `TG_MGMT_BOT_USERNAME`.

**Webhook**: `POST /webhook/tg-mgmt/{path_secret}` (двойная проверка: path_secret + X-Telegram-Bot-Api-Secret-Token, оба = `tg_webhook_secret(token)`).

**Привязка**: 6-знач код 10 мин TTL. Юзер генерит в кабинете → отправляет `/link XXXXXX` в боте, или открывает deep-link `t.me/bot?start=LINK_XXXXXX`.

**Команды**: `/start /link /unlink /me /stats /menu`

**Inline-меню**: профиль / стата 7 дней / последние КП-заявки / toggle подписок (proposals/records/errors).

**Push-уведомления** через `notify_user(user_id, text, kind)`:
- При отправке КП (ручная или auto_proposal) → push с inline-кнопками «Выиграно/Отказ»
- При новой заявке через `save_record` ноду
- Респектит `User.tg_notify_*` флаги

**Расширение User (миграция):**
- tg_user_id / tg_username / tg_link_code / tg_link_expires
- tg_notify_proposals / tg_notify_records / tg_notify_errors

**REST для UI** (`server/routes/user.py`):
- GET `/user/tg-link/status` (linked + bot_configured + flags)
- POST `/user/tg-link/code` (генерация кода + deep-link)
- POST `/user/tg-link/unlink`
- PUT `/user/tg-link/notifications` (toggle флаги)

**UI в кабинете → Настройки**: блок «🤖 Telegram-бот управления» с генерацией кода, copy-кнопкой, отвязкой, чекбоксами на типы push'ей.

---

## Спринт «КП-конструктор» (2026-04-27 → 2026-04-28, БОЛЬШОЙ)

Юзер: «надо чтобы получались стильные КП и в случае выбора шаблона всегда получали одинаково оформленные КП, без потери стиля, шапки, подвала, чтобы персонализация была более глубокой».

### Этап 1: Разделение КП и Презентаций (`1657f0a`)

Раньше КП и Презентации были одним модулем (через `doc_type`). Создан отдельный `/proposals.html`:
- Новые модели: `ProposalBrand` (лого/3 цвета/шрифт/preset/реквизиты), `ProposalProject` (контекст клиента + бренд + бот для прайса)
- `server/routes/proposals.py` — CRUD endpoints с валидацией HEX-цветов, whitelist шрифтов и стилей
- `views/proposals.html` — 2 вкладки (Мои КП / Оформление), модалка бренда с цвет-пикерами

### Этап 2: Генерация PDF (`4e00538`)

`server/proposal_builder.py`:
- `parse_client_site` — httpx с timeout/MAX_BYTES/SSRF-защитой
- `generate_proposal` — Claude prompt с (бренд + клиент + сайт-контекст + прайс) → HTML → PDF через xhtml2pdf
- Шаблон с фирменными цветами/шрифтом/лого/контактами
- Auto-refund при ошибке AI

### Этап 3: Email-orchestration (`e24d96a`)

`server/chatbot_engine.py` — нова нода `auto_proposal`:
- IMAP → детект ключевых слов → генерация КП → SMTP-ответ с PDF + threading через In-Reply-To
- В preview-режиме no-op
- Audit-log `proposal.auto_sent`
- Шаблон `auto_proposal_email` в `bot_templates.py` (7-й шаблон): trigger_imap → auto_proposal → save_record

### Этап 4: Многочисленные улучшения (`e93ec13` A.1-A.4 + `b241bba` B.5-B.8 + C.9-C.11 + D.12-D.13)

**A. Качество:**
- A.1 Ручная правка HTML (textarea) → переделана в **WYSIWYG** (`5f1465e`): contenteditable=true на body, медиа отключаются
- A.2 AI-правка одной секции (real × 5)
- A.3 Pre-validation до списания
- A.4 Версионирование (до 10 на КП, можно откатиться)

**B. UX:**
- B.5 Дублирование КП (`/duplicate` endpoint)
- B.6 CRM lifecycle (new/sent/opened/replied/won/lost) + воронка-индикатор + фильтр стадий
- B.7 Email threading (IMAP-watcher парсит In-Reply-To → ProposalProject.outbox_message_id → crm_stage='replied')
- B.8 Публичная ссылка `/p/{token}` без auth, при первом открытии → opened_at + crm_stage='opened'

**C. Auto-mode:**
- C.9 Whitelist в `auto_proposal`: `cfg.require_keywords` + `cfg.email_whitelist` (домены)
- C.10 Pre-approval mode: вместо отправки шлёт TG-уведомление владельцу
- C.11 Подпись (signature_url) в подвале PDF + UI upload в форме бренда

**D. Production:**
- D.12 8 готовых палитр B2B (Че, B2B классика, Изумруд, Бургунди, Графит, Стальной, Тёплый беж, Виноград)
- D.13 Доп. шрифты TTF: Liberation Sans/Serif + Noto Sans/Serif установлены apt'ом, resolve_pdf_font маппит Inter→LiberationSans, Playfair→LiberationSerif

### Этап 5: JSON-first генерация + 4 пресета (`ea7487c`)

**Проблема:** AI генерил HTML напрямую → шаблон/шапка/подвал «плыли», КП с одним пресетом выходили разными.

**Решение:** AI возвращает только **структурированный JSON со слотами**, backend рендерит в HTML по фиксированному шаблону.

- `_claude_prompt_json` → JSON {hero, understanding, offering, pricing, timeline, cta}
- `_render_proposal_json` рендерит по фиксированному шаблону + preset_css
- 4 РЕАЛЬНО разных пресета `_PRESET_CSS`: minimal (тонкие линии, hero без фона) / classic (двойные линии, hero gradient) / bold (крупные h1 30pt, плашки-tagline) / compact (плотный для длинных)
- ProposalBrand расширен (с миграцией): tagline / usp_list / guarantees / tone(business/friendly/premium/tech) / intro_phrase / cta_phrase
- Гарантии бренда — стабильный блок без AI-вариаций
- Tagline в шапке каждого КП

### Этап 6: Прайсы для КП (`ba30acf`)

**Раньше:** прайс тянулся из `ChatBot.BotPriceItem` — неудобно (не у всех есть бот, разговорный прайс ≠ оформительский).

**Теперь:** свой модуль:
- `ProposalPriceList` (юзер → списки) + `ProposalPriceItem` (price_list → позиции)
- `ProposalProject.price_list_id` — приоритетнее `bot_id`
- 9 endpoints: GET/POST/PUT/DELETE `/price-lists` и `/items` + `/import-csv`
- CSV-импорт с auto-detect разделителя, UTF-8/CP1251, валидация цены ≤1 млрд ₽
- 3-я вкладка в `/proposals.html` «📋 Прайсы» с CRUD + inline-таблица позиций

---

## Спринт «Security audit» (2026-04-27, перед КП-спринтом)

### Чек-лист безопасности (по запросу юзера)

**Network/Infra (`67fc9df`, через ssh):**
- ✅ uvicorn → 127.0.0.1 (был 0.0.0.0 — обход nginx)
- ✅ UFW активен (только 22/80/443)
- ✅ fail2ban на SSH
- ✅ nginx server_tokens off

**Auth (`67fc9df`):**
- ✅ Password policy ужесточена: 10+ симв, 2 класса, чёрный список
- ✅ Login alert email при входе с нового IP (User.last_login_ip)

**Application (`89fab31`, `8d91f56`):**
- ✅ P0 регрессия: `/transactions.csv` декоратор применился к `_csv_safe` (helper) вместо endpoint'а
- ✅ Path traversal в ZIP-экспорте сайта
- ✅ CSV-injection в records.csv (применён `_csv_safe`)
- ✅ CSV-import: верхняя граница 1 млрд ₽
- ✅ `_SecretFilter` теперь на root-handler (был только на httpx/openai/anthropic)
- ✅ Storage billing race fix
- ✅ Path-safety в storage cleanup (`Path.resolve().relative_to(uploads_root)`)

**Dependencies (`99377aa`):**
- ✅ pip-audit нашёл 12 CVE → 11 закрыто:
  - python-jose 3.3.0 → 3.4.0 (PYSEC-2024-232/233)
  - python-multipart 0.0.9 → 0.0.26 (3 CVE)
  - python-dotenv 1.0.1 → 1.2.2
  - markdown 3.6 → 3.8.1
- ⚠️ starlette 0.37.2 — pinned в FastAPI 0.111
- ⚠️ xhtml2pdf 0.2.16 — нет fix-версии

### Откат hardening systemd unit (`2e88f8d`)

Сегодняшний daemon-reload впервые применил полный hardening (ProtectHome=true, ProtectSystem=strict, MemoryDenyWriteExecute) — эти директивы лежали в файле с прошлого audit, но на проде systemd не делал reload, поэтому работал старый простой unit.

После применения сначала ExecStart упал с 203/EXEC (ProtectHome блочил доступ к /root/AI-CHE/venv), затем uvicorn workers crash'или после регистрации агентов (вероятно ProtectSystem=strict + MemoryDenyWriteExecute).

Откатился к минимально безопасному набору: NoNewPrivileges + PrivateTmp.

---

## Спринт «UX-улучшения + тесты» (2026-04-27 утром)

`9d2c5bf` UX:
- Внятная ошибка пустого workflow (вместо «(Бот не ответил)»)
- Человеческие сообщения Kling вместо «No Kling keys»

`c513a79` Perf:
- Analytics N+1 fix (9 SQL → 4 SQL через conditional aggregation)
- LRU embedding cache (вместо clear-всё-при-переполнении)

`6491a34` Reliability:
- Sites polling финальный fetch при истечении wallclock 10 мин (защита от tab-suspend)

`2682311` Tests:
- TestUserApiKeys (3 теста: encrypted save/load/preview, length validation, provider whitelist)
- TestBotPriceList (3 теста: keyword trigger, substring fallback, CSV exponential cap)

`33bf5ca` Sites editor fixes:
- Тексты со смешанным контентом (`<h1>Привет <span>мир</span></h1>` теперь редактируются)
- Иконки SVG / FontAwesome / Lucide — клик меняет
- Замена картинки с cache-bust + fallback по имени файла

---

## Спринт «Полная WYSIWYG + sync→async fix» (2026-04-27 вечером, `5f1465e`, `2bbddd9`)

**Проблема:** `/agents.html` не работала (кнопки не реагировали).

**Причина:** 14 функций (в agents/workflow/index/proposals) использовали `await aiConfirm/aiAlert` без объявления `async function` — синтаксическая ошибка JS, блокирующая загрузку всего скрипта.

Затронутые: `pollStatus`, `wfcSaveAsTemplate/wfcApplyTemplate/wfcDeleteTemplate/wfcClear`, `applyFeatureFlags/renderSettingsTab`, `toggleAiSectionMode`, `wfSaveAsTemplate/wfApplyTemplate/wfDeleteTemplate/triggerUpload/saveWorkflow/clearCanvas`. Все помечены как async.

**Edit-режим КП → WYSIWYG:** заменил textarea с HTML на contenteditable=true на `<body>` целиком в iframe. Кликаешь в любой текст — печатаешь. Esc — выход. Сохранение через postMessage.

**Sites editor:** аналогично — `contenteditable=true` на body, медиа (img/svg/video/iframe) получают `contenteditable=false`. Юзер кликает где угодно и печатает.

---

## Спринт «UX правки + кастомные модалки» (2026-04-27, `3dc580b`)

**Проблема 1:** PDF КП показывал квадратики вместо кириллицы.
**Решение:** `_ensure_cyrillic_font_registered()` регистрирует DejaVu Sans (TTF из `/usr/share/fonts/truetype/dejavu/`) в ReportLab + family-mapping для bold/italic. `_inject_dejavu_font_face()` добавляет @font-face в `<head>` HTML перед pisa.CreatePDF.

**Проблема 2:** Браузерные confirm/alert/prompt — чужеродны.
**Решение:** Глобальные `aiAlert(msg, type)`, `aiConfirm(msg, opts)`, `aiPrompt(msg, default, opts)` в `views/icons.js` (грузится на всех страницах). Тёмный фон, оранжевые кнопки, иконки info/success/error/warn/question, Esc/Enter/click-outside, inline CSS чтобы работало даже без Tailwind.

Заменены ~77 native dialogs в 5 user-facing views: proposals/sites/chatbots/index/presentations + 38 в admin/agents/workflow.

---

## Спринт «Bot pricing rework + Price-list + MAX fix» (2026-04-27 утром)

### Bot pricing
| Действие | Цена | Где списывается |
|---|---|---|
| Создание с нуля Canvas | бесплатно | `POST /chatbots` — без `deduct` |
| Из шаблона | бесплатно | `POST /chatbots/from-template/{slug}` |
| AI-конструктор | ≥ 1000 ₽ | `bot.ai_create_min` |
| AI-доработка / правки | real × 5, без фикс | `bot.ai_improve_min=0`, `ai.improve_margin_pct=500` |
| Реальные диалоги бота | real × 3 | `ai.reply_margin_pct=300` |
| Edit-block в сайте | real × 5 | переписан с фикс 5 ₽ на real × 5 |
| Storage файлов | 50 ₽/мес за 100 МБ | `storage.per_100mb_month` |

### Свои API-ключи юзера
Юзер может в кабинете → вкладка «Свои API» подключить свой OpenAI/Claude/Gemini/Grok ключ:
- Хранится `EncryptedString` через HKDF от JWT_SECRET
- При AI-вызове бота → `_load_user_api_keys(user_id)` загружает в ctx
- Скидка: `ai.user_key_discount_pct=20` — юзер платит 20% от обычной цены

### Прайс-лист бота с semantic vector search
Новая модель `BotPriceItem` (bot_id, name, price_kop, price_text, category, description, sort_order, embedding_json — 1536-dim вектор text-embedding-3-small).

При вопросе клиента:
1. `_price_keyword_in_text` — детектит триггер
2. `_cached_query_embedding` — embedding запроса с TTL 10 мин
3. Cosine similarity → top-15 при threshold 0.30
4. Inject в system_prompt
5. Fallback на substring если OpenAI недоступен

### MAX полный fix (`bb18a4f`, `7bfa9cc`, `d88077b`, `d81a0b5`)

Каскад из 3 багов:
1. MAX API deprecation — `?access_token=` больше не работает, требует `Authorization` header
2. MAX ожидает Authorization БЕЗ префикса `Bearer`
3. JWT_SECRET race — `auth.py` импортировался раньше `ai.py` (где был load_dotenv())

---

## Кто юзер и что делаем
- Юзер — Денис, владелец `aiche.ru`. **B2B AI-платформа** для предпринимателей.
- Стек: FastAPI + SQLite + JS SPA. Прод в Нидерландах (Clouvider).
- Юзер — не программист. Общаемся по-русски, понятно, без терминов где можно. Делаешь — катаешь сразу на прод.

## Текущая «фаза» проекта
Платформа умеет:
1. **Чат с AI** (GPT-4o, Claude Sonnet/Opus/Haiku, Perplexity, Grok, Imagen, Veo)
2. **Бизнес-решения** — 30 готовых промптов с фикс-ценой, выдача PDF
3. **Чат-боты для бизнеса** в TG/MAX/VK/Avito/widget — 7 шаблонов
4. **Конструктор сайтов** — фоновая генерация, два tier (Sonnet/Opus), WYSIWYG-редактор
5. **🟢 КП-конструктор** — отдельный модуль с брендами, прайсами, JSON-first генерацией, WYSIWYG, AI-правкой секций, версиями, CRM, email-оркестратором
6. **🟢 Презентации v2** — PPTX/HTML/PDF, color picker, vision, графики, ТЗ-визард, парсинг сайта клиента
7. **AI-агенты** с очередью
8. **🟢 PWA + Desktop standalone + TG management-бот**

Финансы: Welcome 50₽, реферал 10%. Платежи через ЮKassa (тестовый shop). Деплой ручной.

## Что НЕ сделано (но понятно как)
1. **OAuth Google/VK** — код готов, ждёт `GOOGLE_CLIENT_ID`/`VK_CLIENT_ID` в env
2. **Прод ЮKassa** — сейчас тестовый shop, нужен live shop_id+secret
3. **TG management-бот** — реализация готова, но юзеру нужно создать бот через @BotFather и заполнить env `TG_MGMT_BOT_TOKEN`/`TG_MGMT_BOT_USERNAME` + `setWebhook`
4. **Web Push API через VAPID** — push в браузер без TG (defer)
5. **starlette апгрейд** — нужно обновить FastAPI 0.111 → 0.115+ (отдельный спринт)
6. **2FA для админки** — TOTP (отдельный спринт)
7. **Cloudflare/CDN+WAF** — сейчас прямой запрос в Дронтен NL

## Что обычно ломается
1. **Google AI Studio 429 «prepayment depleted»** — закончились кредиты в Google Cloud билле. Auto-refund в `/message` уже работает.
2. **Veo 3.0 fast 503** — fallback на 3.1 → 3.0 → 2.0 автоматически.
3. **Anthropic 60 сек timeout** — было до спринта sites-async, сейчас 600 (`ANTHROPIC_TIMEOUT_SEC=600`).
4. **Backup в git** — `backups/` в `.gitignore`, но `git add -A` может зацепить. Использовать `git add -A ':!backups/'`.

## Как разобраться в новой задаче от юзера
1. **Прочитай эти файлы.** Серьёзно — здесь ВСЁ актуально.
2. **Запроси audit log за нужный период:**
   ```bash
   curl https://aiche.ru/admin/actions.txt?since_hours=72&only_errors=true \
        -H "Authorization: Bearer <admin token>"
   ```
3. **`git log --oneline -25`** — что трогали недавно.
4. **Grep tool** — где живёт фича.
5. Если фича большая — сначала **отвечай планом**, потом делай.
6. После деплоя — **подтверди работу** живым curl.

## Стиль работы юзера
- «Делай по порядку», «делай на свое усмотрение» = можешь катать сразу на прод
- «Точечно поправим» = не бойся ошибиться, лучше быстрее
- Любит чёткие списки с эмодзи 🟢🟣 для статусов и tier'ов
- Не любит вопросы из серии «А что предпочитаете?» — лучше прими решение сам
- При проблеме — пришлёт скриншот, по нему ориентируйся

## Канал коммуникации
- Все правки — сразу `git push origin claude/eloquent-carson-885bc0:main` → `ssh ... git pull && systemctl restart ai-che`
- Отчёт после каждого блока — короткий список что сделано + ссылки на файлы (file:line)
- При крупных спринтах — обновляй `CLAUDE.md` и `HANDOVER.md`

## Полезные команды
```bash
# Запуск local dev
DEV_MODE=true APP_ENV=dev JWT_SECRET=test-jwt-secret-32-chars-long-yes \
ALLOWED_ORIGINS=http://localhost:8000 python -m uvicorn main:app --reload --port 8001

# Тесты
DEV_MODE=true APP_ENV=dev JWT_SECRET=test-jwt-secret-32-chars-long-yes \
ALLOWED_ORIGINS=http://localhost:8000 python -m pytest tests/

# JS syntax check (после правок views/*.html)
node -e "
const fs=require('fs');
for(const f of ['views/index.html','views/proposals.html','views/sites.html','views/chatbots.html','views/presentations.html','views/agents.html']){
  const src=fs.readFileSync(f,'utf8');
  const m=src.match(/<script>([\s\S]*?)<\/script>/g)||[];
  for(let i=0;i<m.length;i++){try{new Function(m[i].replace(/^<script>|<\/script>$/g,''));}catch(e){console.log(f+' #'+i+': '+e.message);}}
  console.log(f+': OK');
}"

# SSH прод (с обходом для кириллицы в HOME)
HOME="C:\\Users\\Денис" ssh -i 'C:\\Users\\Денис\\.ssh\\id_ed25519' \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  root@194.104.9.219

# Logs прод
journalctl -u ai-che -n 100 --no-pager

# Audit log dump (admin)
curl -H "Authorization: Bearer <admin token>" \
     "https://aiche.ru/admin/actions.txt?since_hours=72&limit=2000"

# Apply migrations + sanity import (local)
DEV_MODE=true python -c "
import sys;sys.path.insert(0,'.')
from server.db import Base,engine,apply_lightweight_migrations
from server import models
Base.metadata.create_all(bind=engine)
apply_lightweight_migrations()
import main; print('routes:',len(main.app.routes))"
```
