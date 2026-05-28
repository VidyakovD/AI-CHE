# TODO_NEXT — задачи на очереди, по модулям

> Что делать в новых сессиях. **Структура по модулям** — открой нужный `.md` в `docs/modules/` для контекста перед работой.

_Последнее обновление: 2026-05-28 (гига-сессия 26-28 мая — 49 закрытых задач + true zero-downtime blue/green + 5 новых модулей агентов)._

## 🆕 Состояние 2026-05-28

**Прод**: HEAD = `e9dad93+`, gunicorn + 2 инстанса (8000 GREEN + 8001 BLUE) за nginx upstream. **120/120 = 100% zero-downtime** при rolling deploy. Деплой = `scripts/deploy.sh`. См. [docs/DEPLOY.md](docs/DEPLOY.md).

### Закрыто в гига-сессии 26-28 мая (29 коммитов: `236020e` → `15d2502`):

**Архитектура:**
- ✅ **Blue/green deploy** с nginx upstream — true zero-downtime (scripts/deploy.sh)
- ✅ **gunicorn** вместо uvicorn standalone (gunicorn.conf.py)
- ✅ **2 systemd unit'а** (ai-che + ai-che-blue) на портах 8000+8001

**Биллинг:**
- ✅ **ИИ-агент = real_cost × 3** через `calc_agent_cost_kop` (5 точек: chat/cron/manual/webhook/tg-relay)
- ✅ **Динамические цены** USD × курс ЦБ × 3 (cron daily recalc)
- ✅ **Embedding billing** на /knowledge/upload (1 ₽/МБ + refund при ошибке)

**Новые модули (5):**
- ✅ 🏋 **Тренер** (`coach`) — программы + фиксация прогресса
- ✅ 🎯 **Директолог Я.Директ** (`direct_ads`) — анализ/A-B/рекомендации
- ✅ 🥗 **Питание** (`nutrition`) — проактивный с [LEARNED:fact]
- ✅ 📰 **Новостник** (`news_aggregator`) — автопостинг TG
- ✅ 💙 **ВК Реклама** (`vk_ads`) — анализ через ads API

**Инфра модулей:**
- ✅ **Скилы (Итерация 4)** — register_agent(skills=[...]) с price_delta
- ✅ **Settings_schema** — UI рендерит форму вместо raw JSON

**КП:**
- ✅ **Custom-домены через CNAME** + Let's Encrypt (B2B value)
- ✅ **Watermark «ПОДПИСАНО» + QR** в PDF → /p/{token}/verify
- ✅ **A/B стили КП** (default/sales/consultative/technical)
- ✅ **Bulk-импорт из CSV** до 100 строк
- ✅ **Auto-fill из email** (LLM парсит → fields)
- ✅ **Cron auto-followup** уже было
- ✅ **Calendar integration в КП** — событие при подписи

**Сайты:**
- ✅ **Custom-домен** через CNAME + nginx upstream + certbot
- ✅ **Платный хостинг + SEO** (meta + sitemap + robots + OG)
- ✅ **Чат-бот авто-встраивается** в сайт сразу после выбора
- ✅ **Регенерация другой моделью** (Sonnet→Opus переключение)
- ✅ **Auto-flag failed generations** (cron monitor + админ алерт)
- ✅ **A/B preset стили** — уже сделано в КП, для сайтов будущим спринтом

**VK интеграции:**
- ✅ **VK community-бот в /chatbots.html** — было, но переподтверждено
- ✅ **VK community-бот к Че** — личный (/agents-modular.html → 📲 → VK)
- ✅ **VK Ads модуль** — анализ кампаний (нужен user-токен)

**Сервисы:**
- ✅ **Раздел «Сервисы»** в sidebar + `/services/voice.html` (Whisper)

**Календарь:**
- ✅ **Cron sync** раз в 30 мин (cached_events_json)

**A11y:**
- ✅ **Skip-link** на 6 страницах (index/proposals/sites/chatbots/agents/admin)

**Безопасность:**
- ✅ **Wave 4 audit** — 10 фиксов (3×P0 + 3×P1 + 3×P2)
- ✅ **cron_min_interval_ok** (5 мин минимум на per-user cron)

**Bugfix-ы:**
- ✅ Perplexity всегда 3 ₽ → alt_model fallback
- ✅ TG-бот ConnectError → убран AI_HTTPS_PROXY fallback
- ✅ Ctrl+Enter переносит → keypress preventDefault
- ✅ Sites preview белый/наезд/фото-edit (3 фикса)
- ✅ Цветные эмодзи на главной → убраны

**Прокси:**
- ✅ Xray VLESS заменён на HTTP `154.205.231.44:63602`

### Закрыто в этой сессии (28 коммитов поверх `236020e`):

**Wave 4 security audit (10 фиксов):**
- P0×3: validate_external_url, /api/v1 rate-limit, scope CSV strip
- P1×3: MCP fail-closed, webhook HMAC sort_keys, follow_redirects=False
- P2×3: bulk_prepare worker_lock, search_notes cap, FinanceTransaction BIGINT cap
- cron_min_interval_ok (5 мин минимум) против DoS на per-user cron

**TESTING_LOG отложенные:**
- Embedding billing на /knowledge/upload (1 ₽/МБ + refund при ошибке)
- XLSX bloat guards (100 листов, 10K симв/ячейка)
- 7 багов от юзера: Perplexity всегда 3₽ (alt_model fallback), TG-бот retry+proxy,
  Ctrl+Enter keypress, Sites preview белый/наезд/фото в edit-mode, цветные эмодзи на главной

**Новые фичи:**
- ✅ **Custom-домены через CNAME + Let's Encrypt** (модуль 09-sites)
- ✅ **Скилы модулей (Итерация 4)** — чекбоксы с доплатой
- ✅ **Биллинг ИИ-агент real_cost × 3** (margin из ai.reply_margin_pct)
- ✅ **Питание-агент проактивный** (рацион + калории + [LEARNED:fact])
- ✅ **Тренер** — новый модуль coach (планы + фиксация прогресса + скилы)
- ✅ **Чат-бот на сайте** — relay TG/MAX + AI с предобучением
- ✅ **Платный хостинг сайтов + SEO** (meta + sitemap + robots)
- ✅ **Новостник** — модуль news_aggregator с автопостингом в TG
- ✅ **Динамические цены USD × курс ЦБ × 3** + accumulator недокопеек
- ✅ **Раздел «Сервисы» + распознавание голоса** (/services/voice.html)

**Инфра:**
- ✅ **gunicorn вместо uvicorn** (max_requests, graceful_timeout)
- ✅ **blue/green** с nginx upstream + proxy_next_upstream → true zero-downtime
- ✅ **scripts/deploy.sh** для координированного rollout

_Последнее обновление до этой сессии: 2026-05-22._

> ℹ️ **Pre-launch stage:** база на проде пустая, только админ. Перфоманс/N+1/A-B-тесты/аналитика usage преждевременны. Приоритет — security к моменту запуска, оставшиеся блокеры на стороне юзера.

## 🆕 Состояние 2026-05-22

**533 теста проходят. Прод обновлён, `ai-che active`.**

Главные направления Loom (модуль 23) — ✅ Фазы 0 и 1 полностью закрыты,
✅ Phase 2 закрыта (Calendar, Питание, Заметки). 26 модулей в AGENT_REGISTRY.

Архитектурный сдвиг TG/MAX: модель «общий бот» → **«юзер подключает свой бот»**
через @BotFather. См. `server/personal_bot_relay.py`. Старый `tg_management.py`
оставлен для legacy push-уведомлений (`notify_user` через `user.tg_user_id`).

### 🔴 Pre-launch блокеры (только юзер может)

| | Что | Где |
|---|---|---|
| 1 | **РКН** — регистрация оператора ПДн (152-ФЗ ст.22) | pd.rkn.gov.ru |
| 2 | **Ротация Google API key** (скомпрометирован) | aistudio.google.com/apikey |
| 3 | **Google OAuth Test Users** для Calendar scope | console.cloud.google.com |

✅ Закрыто: ЮKassa+ОФД (Evotor подключен 2026-05-22).

### 🟡 Открытые задачи следующих сессий (после 28 мая)

| Приоритет | Что |
|---|---|
| 🟢 low | **Multi-LLM паттерны** Pipeline/Parallel/Verify (workflow patterns) — большой scope |
| 🟢 low | **Module manifest.yaml** формат — рефакторинг 28 модулей (для будущего marketplace) |
| 🟢 low | **Splitting `chatbot_engine._execute_node`** (1100 строк) — высокий риск, без покрытия тестами |
| 🟢 low | **Тесты на OAuth flow** для Calendar (mock exchange request) |
| 🟢 low | **A11y aria-label + alt + label-for** на 6 страницах (skip-link уже есть) |
| 🟢 low | **Самопрогон 40 пилотов Solutions** на синтетических кейсах для тюна промптов |
| 🟢 low | **Видео-приветствие** через Veo в КП (зависит от Google Cloud credits) |
| 🟢 low | **Settings_schema** для остальных 23 модулей (5 уже сделано) |
| 🟢 low | **VK ads — write-операции** (создание/изменение кампаний). MVP только READ |
| 🟢 low | **Splitting `views/icons.js`** (2700 строк) — косметика |
| 🟢 low | **Marketplace withdrawal flow** — продуктовое решение |
| 🟢 low | **Migrate notify_user() на personal_tg** — отложено (риск потери push при не-personal-bot)|

#### Снято с очереди (проверено в сессии 2026-05-28):
- ~~Удалить /user/tg-link/* /user/max-link/*~~ — index.html всё ещё их использует
  для notification-settings + legacy push. Не удаляем, оставляем работать
  параллельно с personal-bot flow.
- ~~notify_user через personal_tg~~ — текущий код в `tg_management.py` работает,
  миграция несёт риск потери уведомлений у юзеров с не-personal botом.
  Делать только когда все юзеры мигрированы на personal-bot.

---

## 🔴 ГЛАВНАЯ ЗАДАЧА — ИИ Агенты (модульные)

**См. [docs/modules/23-agents-modular-roadmap.md](docs/modules/23-agents-modular-roadmap.md)** — детальная дорожная карта.

**TL;DR:** Юзер прислал ТЗ v0.2 2026-05-16 (внутреннее кодовое имя «Project Loom», бренд остаётся AI Студия Че). Концепция: оркестратор + каталог модулей с прокачкой L0→L4 + LLM Router (4 провайдера по силе) + персонализация. 50-60% инфры уже есть, надстраиваем поверх. Мобильное — только PWA, без native. Все 10 открытых вопросов закрыты, можно стартовать Фазу 0.

**Согласованные первые 3 модуля:** Почта (Gmail+Yandex IMAP), Копирайтер с прокачкой (расширение Креаторов), Финансы личные (CSV-импорт).

**Старая ветка ИИ Агенты v2** ([22-agents-v2-roadmap.md](docs/modules/22-agents-v2-roadmap.md)) — приостановлена. Иitre 1-2 (Knowledge Hub + 🔍 Поисковик) в проде, оставлены технически.

**Принятые решения (2026-05-15):**
- Тариф: **pay-per-run** (50-300 ₽), как Solutions
- Knowledge Hub: **100 МБ бесплатно**, дальше 50 ₽/100 МБ (как сейчас storage)
- Автоответчик: **отдельная роль**, существующие чат-боты живут параллельно

**Прогресс старой v2 (приостановлено):**
- ✅ Иitre 1 (Knowledge Hub) — `c45ecb8` + `448c330`. Категории, auto-classify, UI, backfill. **Переиспользуется.**
- ✅ Иitre 2 (Поисковик + agents-v2.html) — `c436eae`. **Заморожено**, страница оставлена.
- ⏸ Иitre 3 (5 ролей) — отменена, концепция изменена.

**Project Loom получено 2026-05-16:** ТЗ v0.2 от юзера → новый модуль 23 (singleton-агент). См. roadmap.

**🆕 Сессия 2026-05-16 (вторая половина) — аудит Фазы 0-1 + крупные фичи:**
- ✅ Закрыто 14/16 пунктов аудита (биллинг / race / rate-limit / PrivacyGuard / cache leak / native dialogs / interaction_count / level backdoor / dead code / JSON robust / meta cap / категории каталога / /me/full / локальные SVG)
- ✅ **Cron-runtime** для модулей: schedule_cron реально запускает invoke_module по расписанию через `server/cron/agents_modules.py` (worker-lock, биллинг, прокачка). 10 unit-tests.
- ✅ **Webhook-триггер**: POST /api/agents/triggers/webhook/{token} даёт внешним сервисам (CRM/Zapier) дёргать модуль. 128 бит токен, UI генерации/копирования.
- ✅ **Manual invoke**: «▶ Запустить сейчас» — тест прямо из UI.
- ✅ **Admin stats**: /admin/agents-stats — распределение по статусам, TOP модулей, levels, cron-active, выручка.
- ✅ **Tailwind**: agents-modular.html переведена на /styles.css с CDN-fallback.
- 364 теста проходят (было 299 → +65).

**🆕 Сессия 2026-05-18 (вторая половина) — техдолг аудита + 152-ФЗ:**
- ✅ **PrivacyGuard wiring** ([server/ai.py](server/ai.py)) — все LLM-вызовы маскируют PII перед отправкой, размаскируют в ответе. 7 wiring-тестов.
- ✅ **Atomic interaction_count** — `m.interaction_count = (...) + 1` RMW заменён на SQL UPDATE через `increment_module_interaction()` в 4 местах (cron + manual invoke + send_message + webhook). 3 интеграционных теста с реальной БД.
- ✅ **Лимит модулей** — макс 12 одновременно через `pricing_config["agents.max_enabled_modules"]`. Проверка в `connect_module` при подключении нового и re-enable отключённого.
- ✅ **log_action** в 6 endpoint'ах: `patch_my_agent`, `disconnect_module`, `patch_module_memory`, `generate_webhook_token`, `revoke_webhook_token` (send_message не логируется — spam, история уже в AgentMessage).
- 375 тестов проходят (было 371 → +4).

**Прогресс модуля 23 «ИИ Агенты (модульные)» — 2026-05-16, одна сессия:**

✅ **Фаза 0** — полностью закрыта (10+ коммитов):
- Agent + AgentMessage модели + миграции
- CRUD endpoints `/api/agents/*`
- Agent Builder (диалоговый конструктор → personal agent режим)
- LLM Router (4 провайдера, матрица task×complexity, fallback chain)
- Семантический кэш (hash-based, TTL, daily cleanup)
- PWA (Service Worker network-first + install banner)
- Адаптивная вёрстка моб (sidebar→drawer)

✅ **Фаза 1 (частично)** — после уточнения архитектуры юзером:
- Архитектурная правка: singleton-агент + AgentModule
- Memory Hub (Agent.profile_json) + Personality Layer (Agent.personality_json)
- Module Runtime (invoke_module) с подмешиванием Memory Hub + module_memory
- Прокачка модулей L0→L3 (по interaction_count + [LEARNED:] заметкам)
- Onboarding flow с приветствием при первом входе
- Quick-replies в опросах (chips-кнопки под сообщениями)
- Dicebear bottts SVG-аватары (16 preset в стиле сервиса)
- Раскрытие агента в sidebar → память + редактирование знаний inline
- «Модули» → «Агенты» переименование в UI (с русским склонением)
- Босс-режим reply (Че не дублирует работу модулей)
- Sidebar: Креаторы как подпункт ИИ Агентов
- 🔜 **Итерация 4** — Скилы (чекбоксы, price_delta)
- 🔜 **Итерация 5** — История + share/export
- 🔜 **Итерация 6** — Спрятать старый конструктор

---

## 🟠 Известные баги (с pre-launch)

- **Креаторы: «Сохранить» не закрывает модалку** (2026-05-15) — на бэке POST успешен, бренды создаются, но фронт не подхватывает. Был зафикшен в `ac2a920` (optimistic update + изолированные try) — но юзер сообщает что поведение не изменилось. Возможно браузерный кеш JS. **Action:** в новом чате попросить юзера показать DevTools console при попытке сохранить (там должны быть `[saveBrand]` логи), затем разобраться. В рамках v2 ИИ Агентов Креаторы переезжают — может решиться само через новый UI.

---

## ✅ Security: PrivacyGuard wiring закрыт (2026-05-18)

- **152-ФЗ защита**: `server/privacy_guard.py` теперь обёрнут вокруг `server/ai.py::generate_response()` — центральный entry point. Покрывает ВСЕ LLM-вызовы (chat/orchestra/proposal/sites/agents/chatbots/creators/knowledge/presentations).
  - PII (email/phone/INN/SNILS/CC/IBAN/SWIFT/банк-счёт) маскируется в токены `[[EMAIL_1]]` и т.д. до отправки в Anthropic/OpenAI/Google/Grok/Perplexity, ответ автоматически размаскируется.
  - Escape hatch: `extra={"_privacy_skip": True}` — для модулей, которым PII нужен сырьём (будущий парсер выписок банка).
  - Провайдеры image/video (kling, veo) — пропускаются (PII в промпте картинки бессмысленно).
  - Тесты: `tests/test_ai_privacy.py` (7 wiring-тестов) защищают от регрессии.

---

## ✅ Модуль «Креаторы» — MVP отгружен (2026-05-13)

6 итераций, в проде. См. [docs/modules/21-creators-roadmap.md](docs/modules/21-creators-roadmap.md).

**Что осталось у Креаторов (полировка, не блокеры):**
- Цены захардкожены → вынести в `pricing_config` (key prefix `creators.*`)
- Drag-n-drop постов по календарю (переносить дату мышкой)
- Push когда пост готов и канала нет («скопируй и опубликуй»)
- Bulk «подготовить все planned» одной кнопкой
- Перегенерировать только текст без картинки (отдельная кнопка)
- Unit-тесты для creators_planner/prepare/analyzer
- Аналитика опубликованных постов (просмотры/реакции через TG/VK APIs)
- YouTube OAuth + upload + Instagram Meta API (отложено — риск из РФ)
- Подписка 990 ₽/мес = до 30 постов (v2 после набора юзеров)

---

## 🔴 БЛОКЕРЫ для коммерческого запуска (только юзер может сделать)

### 1. РКН — регистрация оператора ПДн
- `https://pd.rkn.gov.ru/` → подать заявление (**152-ФЗ ст. 22**)
- Сервер в РФ → заявка проще
- **30 дней рассмотрения** — лучше начать раньше
- **ШТРАФ 60-300k₽** при работе без регистрации

### 2. ✅ ЮKassa live-shop (2026-05-18)
- Переключено: `YOOKASSA_SHOP_ID=1358774`, `YOOKASSA_SECRET_KEY=live_...` в `/root/AI-CHE/.env`
- Backup: `/root/AI-CHE/.env.backup-yookassa-20260518-094123`
- Webhook URL для ЛК ЮKassa (если ещё не прописан): `https://aiche.ru/payment/webhook`
  - События минимум: `payment.succeeded`, `payment.canceled`, `refund.succeeded`
- Smoke-тест: webhook возвращает `401 Signature required` без HMAC и `401 Invalid signature` при плохой подписи — защита от фальшивых webhook'ов работает.

### 3. ОФД для 54-ФЗ (**ШТРАФ 30k₽/чек если не подключено**)
- В ЛК ЮKassa → Настройки → Кассовый чек → подключить **Атол Онлайн** или **Контур.ОФД**

### 4. Ротировать Google API key
- Был захардкожен в `scripts/check_google_keys.py` (удалён). Скомпрометирован — ротировать через `https://aistudio.google.com/apikey`.

### 5. (Отложено) Data-retention cron
Включать когда наберётся живая база. Сейчас некого чистить (только админ). См. [18-privacy-compliance](docs/modules/18-privacy-compliance.md).

---

## 🟠 Задачи Claude — по модулям

### [05-chatbots](docs/modules/05-chatbots.md)
- **`views/icons.js`** (~2700 строк) — разбить на icons/labels/drag-drop модули
- **Splitting `chatbot_engine.py`** ✅ ЧАСТИЧНО — senders/voice/sandbox вынесены, осталось `_execute_node` (~1100 строк)

### [06-solutions](docs/modules/06-solutions.md)
- **Самопрогон 40 пилотов** — admin запускает каждый на синтетических кейсах, ловит мусорный output → тюнить промпты в `scripts/seed_v2_solutions.py`
- **Решения 31-40** используют `{input}` legacy — если output плохой, переписать на `{field.x}`
- **Каталог 40 пилотов в docs/** — генерировать `docs/solutions-catalog.md` из БД (нет SQL-доступа = не увидеть полный список)
- ⏸ _Отложено (нужны юзеры):_ A/B формы vs textarea, пересчёт цен по статистике real_cost, видео-демки

### [07-proposals](docs/modules/07-proposals.md) — 7/8 ✅ закрыто `f5bef9f`
1. ✅ `PROPOSAL_COST_KOP` через pricing_config — уже было (`_proposal_create_cost`/`_edit_cost`)
2. ✅ Email через `EMAIL_RE.match()` (`f5bef9f`)
3. ✅ Кириллица в PDF: `filename*=UTF-8''` (RFC 5987) в 3 точках
4. **Public proposal page → Jinja-template** — отложено, большой рефакторинг ~150 строк inline-HTML
5. ✅ `open_count` — новая колонка + atomic UPDATE при каждом open
6. ✅ `max_tokens` адаптивный (`6000 + len(price_text)//4`, cap 16000)
7. ✅ Snapshot-version race — subquery `id IN (top-10)` вместо offset
8. ✅ Запущен `sanitize_legacy_proposal_html.py` на проде (2/2 проектов санитизированы)

### [09-sites](docs/modules/09-sites.md) — 7/8 ✅ закрыто `89cb3c5`
1. ✅ `_strip_markdown_code_fence` dedupe в `/iterate` + `/edit-block`
2. ✅ Closure-bug в lambda → default-args фиксируют значения
3. ✅ Dead `/sites/code` endpoint удалён
4. ✅ Phase `generating_code` после reload — re-polling вместо пустого showDone
5. ✅ `copyCode(event)` явно через аргумент + Firefox/Safari fallback
6. ✅ ETA-hint в loader («обычно 2-4 минуты, прогресс сохраняется»)
7. ✅ a11y radiogroup + aria-labelledby + aria-describedby
8. **Sequential `project.id` → `public_token`** — отложено, большая миграция файловых путей

### [13-public-api](docs/modules/13-public-api.md)
- **Дубликат webhooks.py + crm.py dispatcher** (~150 строк копипасты) → уже частично вынесено в `_outbound.py`, доделать
- ✅ **Webhook flaky test** — уже исправлен в прошлом спринте (uuid в email/URL делает каждый прогон уникальным)

### [20-infra-deploy](docs/modules/20-infra-deploy.md)
- ✅ **scheduler.py 1379→349** разбит на `server/cron/{creators,data_retention,orchestrations,storage_billing,db_backup,maintenance,agents_modules}.py`
- ✅ **Tailwind CDN → build-step**: `agents-modular.html` переведена на `/styles.css` с fallback на CDN при отсутствии файла. Остальные страницы — постепенно (поиск по `cdn.tailwindcss.com`).
- ⚠ **starlette upgrade** (CVE-2024-47874 / CVE-2025-54121) — требует FastAPI ≥0.115 (breaking change). Делать со staging-тестированием отдельным спринтом

### [01-core-auth](docs/modules/01-core-auth.md)
- ✅ **JWT aud/iss strict verify** — авто-активация на 2026-06-10 (env-var `JWT_STRICT_AUD_ISS=true/false` для override), коммит `044d03e`

### [07-proposals](docs/modules/07-proposals.md)
- ✅ **Public proposal page → Jinja-template** — вынесено в `views/proposal_public.html`, ~200 строк inline-HTML удалено из main.py (`6d87fc9`)

### Общее (cross-module)
- **A11y на остальных страницах** (proposals/sites/chatbots/agents/admin/api/marketplace) — сделано только index.html
- ⏸ **chatbot_engine._execute_node split** (~1100 строк в runtime ботов) — высокий риск без полного покрытия unit-тестами
- ⏸ **views/icons.js 2700 строк split** — не критично, отложено

---

## 💡 Идеи продуктовых фич (отдельные спринты)

### [07-proposals](docs/modules/07-proposals.md)
- **«Напомнить клиенту» cron** — `sent_at > 3 дня` и `opened_at IS NULL` → авто-followup
- **Sticky-watermark «Подписано»** в PDF после подписи + QR-верификация
- **Шаблоны КП по нише** one-click (веб-студия / IT / ремонт)
- **A/B сравнение 3 presets** за одну цену
- **Auto-fill `client_email` из IMAP** — paste raw email → парсим поля
- **Видео-приветствие** через Veo (персонализация)
- **Calendar integration** — авто-создание встречи при ответе клиента

### [09-sites](docs/modules/09-sites.md)
- **Custom-домен через CNAME** — для B2B-юзеров
- **SEO-preview stage** (OG-теги, robots, sitemap) за +50 ₽
- **Шаблоны сайтов one-click** (5-10 готовых ТЗ: лендинг кофейни, портфолио фотографа, юр-услуги)
- **Auto-flag failed-generation** — если %failed за час >30% → email админу
- **Кнопка «Регенерировать»** с тем же ТЗ + другой моделью

### [10-agents-workflows](docs/modules/10-agents-workflows.md) / [11-knowledge-rag](docs/modules/11-knowledge-rag.md)
- **Bulk-генерация КП из CSV** — для агентств

### [12-marketplace](docs/modules/12-marketplace.md)
- **Marketplace withdrawal flow** — авторы выводят 70% на карту через ЮKassa (если решено вернуть marketplace)

---

## 🟡 Опционально для юзера

| # | Что | Действие |
|---|---|---|
| 1 | **Yandex Disk бэкапы** | Создать app-password в `https://id.yandex.ru/security/app-passwords` → `.env` `YANDEX_DISK_USER/PASSWORD/FOLDER` → restart |
| 2 | **Старый NL-сервер удалить** | Через 30 дней: панель Clouvider → Delete VM `194.104.9.219` |
| 3 | **Gemini API ключ** обновить если нужен Imagen 4 / Veo 3 / Gemini Flash | `https://aistudio.google.com/apikey` → `.env` `GOOGLE_API_KEYS` |
| 4 | **Wazzup24 (WhatsApp)** | Договор + API key → в карточке бота |
| 5 | **TG management-бот** | @BotFather → `TG_MGMT_BOT_TOKEN` + `TG_MGMT_BOT_USERNAME` |

---

## 📋 Памятка для Claude в новых сессиях

### Подключение к проду
```bash
HOME="C:\\Users\\Денис" ssh -i 'C:\\Users\\Денис\\.ssh\\id_ed25519' \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  root@193.187.92.147 "..."
```

### Запуск тестов
```bash
DEV_MODE=true APP_ENV=dev JWT_SECRET=test-jwt-secret-32-chars-long-yes \
ALLOWED_ORIGINS=http://localhost:8000 PYTHONIOENCODING=utf-8 \
python -m pytest tests/ --tb=line
# 299 passed expected на проде
```

### Деплой
```bash
git push origin claude/<branch>:main
ssh ... "cd /root/AI-CHE && git pull origin main && systemctl restart ai-che && systemctl is-active ai-che"
```

### Сиды решений после изменений
```bash
ssh ... "cd /root/AI-CHE && /root/AI-CHE/venv/bin/python scripts/seed_v2_solutions.py"
ssh ... "cd /root/AI-CHE && /root/AI-CHE/venv/bin/python scripts/seed_perplexity_solutions.py [--update]"
ssh ... "cd /root/AI-CHE && /root/AI-CHE/venv/bin/python scripts/upgrade_orchestra_perplexity.py [--force]"
ssh ... "cd /root/AI-CHE && /root/AI-CHE/venv/bin/python scripts/categorize_solutions.py"
ssh ... "cd /root/AI-CHE && /root/AI-CHE/venv/bin/python scripts/seed_orchestra_solutions.py"
```

### Полезное
- **Audit-log дамп**: `curl https://aiche.ru/admin/actions.txt?since_hours=72`
- Все остальные правила и доступы — в [CLAUDE.md](CLAUDE.md) (индекс) и соответствующих модулях.
