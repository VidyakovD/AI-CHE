# HANDOVER — что произошло, по модулям

> История спринтов. **Чтобы понять что было сделано — открой нужный модуль ниже.** Для деталей коммитов — `git log --oneline -100`.

**Свежее состояние:** 2026-05-28. Прод на `193.187.92.147`. **HEAD=`15d2502`. Blue/green развёрнут.**

---

## 🔥 2026-05-26…28 — ГИГА-СЕССИЯ: 49 закрытых задач + true zero-downtime deploy

Три дня крупнейших изменений. Закатано 29 коммитов на main с `236020e` по `15d2502`.

### Архитектурные изменения

1. **Blue/green deploy** через nginx upstream — true zero-downtime (120/120 OK замерено).
   gunicorn × 2 инстанса (8000 GREEN + 8001 BLUE) + `scripts/deploy.sh`.
2. **Биллинг ИИ агента = real_cost × 3** (через `calc_agent_cost_kop`) вместо
   фикс 50/100 коп. Применено в 5 точках (chat/cron/manual/webhook/tg-relay).
3. **Скилы модулей (Итерация 4)** — `register_agent(skills=[{slug, name,
   price_delta_kop, tools, prompt_addon}])`. 10 скилов в 4 модулях.
4. **Settings_schema модулей** — UI рендерит форму вместо raw JSON.
5. **Custom-домены** для сайтов через CNAME + Let's Encrypt + certbot wrapper.
6. **VK community-бот** для Че (личный) + для чат-ботов (уже было).
7. **A/B стили КП** (sales/consultative/technical/default) — параметр ?style=
8. **Watermark «ПОДПИСАНО» + QR** в PDF КП → /p/{token}/verify.

### 5 новых модулей в AGENT_REGISTRY

- **🏋 Тренер** (`coach`) — программы тренировок + фиксация прогресса
- **🎯 Директолог Я.Директ** (`direct_ads`) — анализ/A-B/рекомендации (read-only)
- **🥗 Питание** (`nutrition`) — рацион + калории + [LEARNED:fact]
- **📰 Новостник** (`news_aggregator`) — сбор новостей + автопостинг TG
- **💙 ВК Реклама** (`vk_ads`) — анализ кампаний через ads API (нужен user-токен)

### Wave 4 security audit (10 фиксов)

P0 (3): validate_external_url создан, /api/v1 rate-limit, scope CSV strip.
P1 (3): MCP fail-closed, webhook HMAC sort_keys, follow_redirects=False.
P2 (3): bulk_prepare worker_lock, search_notes cap, FinanceTransaction int4-CAP.
Plus cron_min_interval_ok (5 мин против DoS на per-user cron).

### Bugfixes от юзера

- Perplexity всегда 3 ₽ → alt_model fallback в calculate_cost
- TG-бот ConnectError → убрал AI_HTTPS_PROXY fallback (Xray не маршрутизирует TG)
- Ctrl+Enter переносит → keypress handler с preventDefault
- Sites preview белый/наезд/фото-edit → 3 фикса
- Цветные эмодзи на главной → убраны
- Xray VLESS-сервер умер → юзер дал HTTP-прокси, AI_HTTPS_PROXY обновлён
- Чат-бот не появлялся на сайте → attach-bot сразу инжектит widget-script

### Прочие фичи

- **Embedding billing** при /knowledge/upload (1 ₽/МБ через pricing_config)
- **XLSX bloat guards** (100 sheets / 10K char per cell)
- **Auto-flag failed site generations** (cron monitor, email админу)
- **Регенерация сайта** другой моделью (Opus после Sonnet)
- **Bulk-генерация КП из CSV** (до 100 строк за раз)
- **Auto-fill КП из email** (LLM парсит → structured JSON)
- **Calendar integration в КП** (событие при подписи)
- **Cron Calendar sync** раз в 30 мин (cached_events_json)
- **Раздел «Сервисы»** с `/services/voice.html` (Whisper transcribe)
- **A11y skip-link** на 6 страницах (index/proposals/sites/chatbots/agents/admin)
- **Тесты**: 512 passed на dev (Py3.14 + bcrypt-broken), 4 skipped

### Файлы коммитов (укрупнённо)

```
172d8b0  security: wave 4 audit — 10 fixes
cb2278d  feat(knowledge)+security: embedding billing + XLSX
4cf201c  feat(agents): Итерация 4 — Скилы модулей
8f62cc7  feat(sites): кастомные домены CNAME + Let's Encrypt
2e0d973  fix(billing+ui): 7 багов от юзера
aa63cf3  feat(billing): ИИ-агент real_cost × 3
25a3706  feat(agents): питание-агент проактивный + 3 скила
d73326f  feat(services): раздел Сервисы + распознавание голоса
94c0ffc  feat(sites): чат-виджет на сайте — 2 режима
337a518  feat(sites): платный хостинг + SEO + sitemap/robots
93ea5d6  feat(agents): news_aggregator — новостник + автопостинг
b9bf48f  feat(agents): 🏋 Тренер — программы + фиксация прогресса
8d454ca  feat(billing): динамические цены = USD × курс ЦБ × ×3
e1b9fbe  feat(services): /services/voice.html страница
03513dd…e9dad93 (8 коммитов) feat(deploy): gunicorn + blue/green
ee69774  feat(agents): VK community-бот + Директолог Я.Директ
ca4e702  feat: backlog batch — calendar cron, bulk КП, autofill, a11y
62a1c16  feat: backlog batch — a11y + calendar в КП + settings-form + style
9761887  fix(personal-bot): убрать AI_HTTPS_PROXY fallback для TG
53c0d08  feat(sites): чат-бот авто-встраивается в сайт сразу после выбора
15d2502  feat(agents): 💙 ВК Реклама — новый модуль агента
```

### Что осталось (отложено по приоритету)

🟢 low: A/B preset видео-приветствие в КП через Veo, manifest.yaml refactor,
splitting chatbot_engine._execute_node, Multi-LLM Pipeline/Parallel/Verify
паттерны, A11y по proposals/sites/chatbots/agents/admin (skip-link сделан,
остальное — aria-label на иконки + label-for на inputs).

⏸ продуктовое решение: Marketplace withdrawal flow.

### Известные проблемы (НЕ блокеры)

- **Imagen/Veo** не работают — Google Cloud billing исчерпан (юзер пополняет)
- **VK ads** нужен user-токен (юзер получит на vkhost.github.io)
- **bcrypt 5 + Py3.14** на dev — 2 теста падают, известно

---

## 🔥 2026-05-20…22 — БОЛЬШАЯ СЕССИЯ: 26 закрытых задач (#1..#26)

Одна из крупнейших сессий проекта. Закрыт почти весь pre-launch polish +
2 крупные фичи + сильный security pass + начато Phase 2 модулей Loom.

### Закрытые задачи (укрупнённо)

**🔴 Bugfix-ы (4):**
- Помощник не знал про новые ИИ Агенты — обновлены `assistant_prompts.py` +
  Ctrl+K command-palette + welcome-hints. Старая `/agents.html` → 308 редирект
  на `/agents-modular.html`. Также `agents-v2.html` → редирект.
- Сайты «вылет в раздел кода при скролле» — sticky tab-bar + `!important`
  для display:none + persist last-viewed-tab в localStorage.
- «Бот пропал» в /chatbots.html — был дубликат аккаунтов Видякова в БД
  (id=1 vidyakov@obsidian.ai vs id=3 vidyakovd@gmail.com), бот перенесён
  через `UPDATE chatbots SET user_id=3 WHERE id=1`. На id=1 ещё лежат
  2 КП, 6 сайтов, 2 презентации, баланс 27 852 ₽ — оставлены.
- Кликабельные ссылки в чате — `_linkifyText` (escape + markdown
  `[text](url)` + bare https://...) в `index.html` и `agents-modular.html`,
  стили `.ai-link` с dashed-underline.

**🛡 Security pass (8):**
- SameSite=Strict для refresh+csrf cookies (access остался Lax для
  совместимости с ЮKassa/OAuth redirects)
- Brute-force защита `/link-code` — двухуровневая sliding-window
  (5/мин + 30/час) через `server.security.link_code_attempt_check`.
- 🔴 Idempotency на `/webhook/tg-mgmt` и `/webhook/max-mgmt` (re-delivery
  = double-charge → fixed через `_is_duplicate_update`).
- 🔴 Markdown-injection в MAX через LLM-output — `_escape_md` для всех
  спецсимволов (`* _ \` [ ] ( ) ~`). Защита от prompt-injected `[phish](url)`.
- Audit trail + HTML-escape email-alert при link/unlink (TG/MAX).
- Rate-limit на webhook + `/user/*-link/code` endpoints (новые правила в
  `server/security.py:RULES`).
- CSP/HSTS — выяснено что **уже настроено** в nginx (HSTS 2y + preload,
  CSP с whitelist cdn.tailwindcss.com, X-Frame-Options SAMEORIGIN).
- Security-review агентом TG/MAX relay — нашёл 2 Critical + 6 Medium,
  все закрыты. OK: SSRF, token leak, CSRF, webhook secret, brute-force,
  billing safety.

**🎨 Pre-launch polish (5):**
- 8 шаблонов сайтов (Кофейня / Юр.услуги / Фотограф / Ремонт / Барбершоп /
  Фитнес / Курсы / Автосервис) — клик на /sites.html → auto-fill ТЗ.
- 6 шаблонов КП по нише (Веб-студия / IT-подряд / Ремонт / SMM /
  Юр.услуги / Дизайн-студия) — auto-fill `extra_notes` инструкциями AI.
- Cron `proposals_followup_loop` (раз в час) — для КП где `sent_at > 3 дней`
  и `opened_at IS NULL` шлёт клиенту вежливое напоминание в тот же
  email-thread (через `In-Reply-To`).
- UI toggle `auto_followup_enabled` в карточке КП.
- Bulk-prepare в Креаторах — кнопка «🚀 Подготовить все planned (N)»,
  до 10 за раз, freemium first + платно, refund per-item на ошибке.
- Drag-n-drop постов в календаре Креаторов: `PATCH /items/{id}/reschedule`,
  HTML5 DnD, optimistic update + rollback при ошибке. Запрет drop на
  published + прошлые даты.

**🤖 ИИ Агенты Phase 1 закрытие + Phase 2 старт (8):**
- **Bootstrap-импорт постов TG/VK в copywriter (B-4)** — `creators_bootstrap.py`,
  VK `wall.get` API + парсинг `t.me/s/{username}` HTML preview. UI кнопка
  «📥 Изучить мои прошлые посты» в карточке copywriter. Сразу даёт L1-L2.
- **📧 Почта модуль** (Yandex IMAP + Gmail + Mail.ru) — отдельно от прошлой
  сессии. Подключение через app-password, fetch последних писем в context.
- **💰 Финансы модуль** — CSV-импорт банковских выписок (Tinkoff/Sber/Alpha/
  generic), keyword-категоризатор. UI drag-n-drop CSV.
- **Adaptive System Prompts** — типизированные `[LEARNED:]` маркеры +
  promotion в Memory Hub (`profile.facts`) когда LEARNED:global.
- **TG-чат с Че** (двусторонний) — `tg_che_relay.process_message` —
  любое сообщение в TG → ответ от Че + опционально модуля. Переиспользует
  всю backend-логику /api/agents/me/messages.
- **MAX-чат с Че** — симметрично TG, через MAX API. `_format_for_max` с
  markdown-escape (после security-fix).
- **Переделка TG/MAX под «свой бот»** — раньше был общий `@aiche_bot` через
  `TG_MGMT_BOT_TOKEN` админа в .env. Теперь **каждый юзер сам создаёт
  бот в @BotFather**, вставляет токен в `/agents-modular.html` → платформа
  валидирует через `getMe`, ставит webhook на `/webhook/personal-tg/<hash>`
  (hash = sha256(JWT+token)[:24]). Token хранится EncryptedString. Webhook
  routing по hash. White-label, нет SPoF. Аналогично для MAX.
- **UX-блок «📲 Где использовать Че»** в шапке `/agents-modular.html` —
  4 канала одновременно: Веб (current) / PWA install (working) / TG (свой
  бот) / MAX (свой бот) / Календари (Google+Yandex). PWA-installer через
  `beforeinstallprompt`.

**🚀 Крупные фичи (3):**
- **Real-аналитика опубликованных постов** в Креаторах. Cron каждые 6 часов
  обновляет VK metrics (wall.getById: views/likes/comments/reposts) +
  TG metrics (парсинг `t.me/<channel>/<msg_id>?embed=1` для views).
  Schema: `external_post_id/chat_id` + `stats_*` в ContentItem. UI: блок
  «📊 Метрики поста» в модалке + кнопка «↻ Обновить» (manual fetch).
- **Модули 🥗 Питание + 📝 Заметки** (Loom Phase 2) — простые AGENT_REGISTRY
  entries в категории «Личный ассистент». Питание = диетолог-консультант
  с safety-guardrails, Заметки = архив с kb_search (Knowledge Hub).
- **📅 Календарь модуль** (Loom Phase 2 завершение). Google OAuth
  (scope=calendar.events.readonly) + Yandex CalDAV (app-password) + ICS URL.
  Без зависимостей google-api-client/caldav — всё через httpx + минимальный
  ICS-парсер (`server/calendar_sync.py`). Context-injection в invoke_module:
  ближайшие 14 дней событий в system_prompt модуля. UI на agents-modular
  модалке с кнопками подключения и списком привязок.

### Закрыто блокеров pre-launch

- 🟢 **ЮKassa+ОФД (54-ФЗ)** — юзер сам подключил Evotor через ЛК ЮKassa,
  режим «Принимать платёж». В нашем backend receipt-данные уже шлются
  (`server/routes/payments.py:208`), `_send` расширен `text_body`/`in_reply_to`
  для multipart email + thread support.

### Что ОСТАЛОСЬ pre-launch (на юзера)

- 🔴 **РКН** — регистрация оператора ПДн (152-ФЗ ст.22), 30 дней
- 🔴 **Ротация Google API key** (был скомпрометирован в `scripts/check_google_keys.py`)
- 🔴 **Google OAuth verification** для Calendar scope — пока работает только
  для Test Users, добавь в Google Cloud Console если хочешь публичный.

### Файлы коммитов (укрупнённо)

```
f3ea976  fix(assistant+sites): 2 UX-бага
8b80c3f  fix(routing): /agents.html → 308 redirect
9027c85  feat(agents-modular): UX-блок «📲 Где использовать Че» + PWA
251f6f0  feat(agents+tg): двусторонний TG-чат с Че
1ab1349  feat(sites): шаблоны one-click для 8 ниш
92a6ff3  feat(proposals): cron auto-followup + 6 шаблонов КП
2988cb1  feat(proposals+creators): UI toggle followup + Bulk-prepare
a36b562  feat(creators): drag-n-drop постов в календаре
17aed98  feat(tg+max): переделка под «каждый юзер подключает свой бот»
0286704  feat(creators): real-аналитика опубликованных постов (TG/VK)
cdeed50  feat(agents): модули 🥗 Питание + 📝 Заметки
50b3bc3  feat(calendar): backend Calendar-модуля (Google OAuth + Yandex CalDAV)
d6266fc  feat(calendar): UI подключения календарей
```

Плюс security batch: `8603dce`, `5e04cd9` (фикс кнопок), `52ec50c` (linkify),
ранее в сессии `6078144 / 90fc11f / 483c27a / 1886290` (Bootstrap/Mail/Finance/Adaptive).

### Тесты

299 → **533 passed** (+234 за сессию). 6 skipped. Без регрессий.

### Архитектурные сдвиги

1. **TG/MAX боты** — модель «общий бот» → **«юзер подключает свой»**. Старый
   `tg_management.py` / `max_management.py` оставлены для `notify_user()` 
   push-уведомлений (использует legacy `user.tg_user_id`). Новый flow через
   `server/personal_bot_relay.py` + `users.personal_tg_bot_token` (encrypted)
   + `_token_hash` для webhook routing.

2. **Новая таблица** `user_calendar_connections` — 1 юзер ↔ N календарей
   (Google + Yandex + ICS), создаётся через `Base.metadata.create_all`.

3. **Module context injection** — `agent_builder._build_module_extra_context`
   подмешивает per-slug data в system_prompt модуля: mail → последние письма,
   finance → сводка транзакций, calendar → ближайшие события.

4. **OAuth state cache** для Google Calendar в RAM (`_OAUTH_STATES` dict),
   TTL 15 мин. Без persistence — простое решение для MVP.

---

## 🔥 2026-05-16 (вторая половина): АУДИТ Фазы 0-1 модульных агентов + cron-runtime + webhook-триггер

Аудит свеженакаченного раздела 23 (ИИ Агенты модульные) и закрытие 14 критических проблем + крупная фича.

### Аудит и фиксы (4 коммита)

**`fcb9f66` — 13 фиксов аудита Фазы 0-1:**
- 🔴 Биллинг подключён в /api/agents/me/messages (50 коп/сообщ + 100 коп/invoke_module через pricing_config, freemium 5 первых сообщений в онбординге, pre-check баланса)
- 🔴 Race на singleton-агенте: partial UNIQUE `uq_agent_active_per_user` + IntegrityError handling
- 🔴 Rate-limit на send_message: 20/мин, 200/час (in-memory sliding window)
- 🔴 PATCH /me/modules/{slug} убрал backdoor поле `level` (юзер мог поставить себе L4)
- 🔴 PrivacyGuard теперь wrappит build_reply_personal + invoke_module
- 🧹 Native dialogs (prompt/confirm/alert) → aiPrompt/aiConfirm/aiAlert
- 🧹 interaction_count++ только при ok-вызове модуля
- 🧹 Cache leak fix: per-user namespace в `_make_cache_key` для personal_agent / agent_builder / module:*
- 🧹 _extract_json с балансировкой скобок (учёт строк/escape)
- 🧹 _dump_meta cap на 8KB (raw/applied/errors режутся первыми)
- 🚀 /api/agents/me/full — 1 запрос вместо 3 для bootstrap
- 🚀 Локальные `/avatars/bottts/*.svg` (16 файлов) вместо api.dicebear.com
- 🔧 Категории в каталоге (Контент/Маркетинг/Документы/Аналитика/Тендеры/Автоматизация/Разработка)
- 📦 31 unit-test для router/cache/extract_json/levels/privacy/memory/meta/rate_limit

**`0a1318b` — cron-runtime для расписаний модулей + Tailwind build:**
- 🚀 `server/cron/agents_modules.py` — каждую минуту loop проверяет включённые модули с schedule_cron + agent.status='active', запускает invoke_module под user_id, списывает agents.module_invoke, сохраняет AgentMessage role=tool с meta.mode='cron_invoke'. Worker-lock защищает от двойного запуска.
- cron-парсер 5-полей "M H D M W" с поддержкой *, N, N-N, N/N
- Schema: agent_modules.last_cron_fired_at DATETIME
- UI: модалка «⏰ Расписание агента» с 5 пресетами + cron-выражение + задача
- 10 новых тестов TestCronParser
- 🎨 Tailwind CDN → /styles.css на agents-modular.html (build-инфра уже была)

**`74e5a33` — manual invoke + admin stats:**
- POST /api/agents/me/modules/{slug}/invoke — запустить модуль вручную сейчас с заданной задачей (та же логика что cron: биллинг, прокачка)
- UI: кнопка «▶ Запустить сейчас» в модалке расписания
- GET /admin/agents-stats?days=30 — распределение Agent.status / TOP модулей / levels / cron-active / транзакции / total_revenue_rub

**`be25027` — webhook-триггер для модулей:**
- 📨 POST /api/agents/me/modules/{slug}/webhook — генерирует unguessable токен (128 бит), хранит в custom_settings.webhook_token. URL копируется и втыкается в CRM/Zapier
- DELETE /me/modules/{slug}/webhook — ревок
- POST /api/agents/triggers/webhook/{token} — публичный fire endpoint. Без auth, защита через token. Тело запроса (≤32KB) попадает в задачу как «контекст события»
- UI: секция «📨 Webhook-триггер» в модалке расписания (генерация/копирование/ротация/revoke)

### Итог сессии

- 4 коммита, ~2000 строк (~1300 prod + ~700 tests/docs)
- 364 теста зелёные (299 → 354 → 364)
- Закрыты ВСЕ 16 пунктов аудита + 3 идеи реализованы (категории, /me/full, локальные SVG)
- Реализованы 2 крупные фичи которые в roadmap были «нет runtime»: cron-расписания модулей + webhook-триггеры
- Pre-launch блокеры из TODO_NEXT не трогали (РКН / ЮKassa live / Google ключ ротация — на стороне юзера)

---

## 🔥 2026-05-15: АУДИТ 40 ПИЛОТОВ + 2 КРИТИЧЕСКИХ БАГА ЗАФИКШЕНО

Полный самопрогон всех Solutions через `scripts/audit_solutions.py` вскрыл что orchestra-пилоты НЕ РАБОТАЛИ корректно в проде. Юзеры платили 50-200 ₽ и получали мусор.

**1.** `8f35bff` `fix(ai)` — MODEL_REGISTRY: добавлены self-reference ключи для `claude-sonnet-4-6` / `claude-haiku-4-5-20251001` / `claude-opus-4-1-20250805`. Без них `generate_response("claude-sonnet-4-6")` возвращал «Модель не найдена».

**2.** `e1c6ab7` `fix(orchestra)` — `_PLACEHOLDER_RE` regex изменён с `\{\{...\}\}` на `\{...\}`. Все seed-данные и документация всегда использовали single-brace `{field.x}`, но рантайм ловил только double-brace → placeholder-замена никогда не работала → Sonnet получал буквальный текст и писал «шаблонные переменные не заполнены, ниже демо-пример».

**3.** `15642ef` `test(solutions)` + `afc0315` `fix(audit)` — `scripts/audit_solutions.py` (470 строк): синтетический прогон всех Solution с правдоподобными значениями полей (по семантике label/name), polling до done/failed, quality_score, отчёт SUMMARY.md.

**Итог аудита (после фиксов):** 36/37 ✅ done, средний score 95/100, 32 пилота на 100/100. Полный отчёт — [docs/SOLUTIONS_AUDIT_2026-05-15.md](docs/SOLUTIONS_AUDIT_2026-05-15.md).

---

---

## 🆕 Спринт 2026-05-13: МОДУЛЬ «КРЕАТОРЫ» — MVP за 6 итераций

Полный новый продуктовый модуль для контент-планирования бизнеса/SMM. От нуля до prod за один спринт.

### По итерациям

1. **`0650245`** (it.1) — Фундамент: 5 моделей (`CreatorBrand`/`ContentCalendar`/`ContentItem`/`CreatorChannelConnection`/`CreatorAnalysisRun`), CRUD брендов до 10/юзер, страница `/creators.html` с модалкой профиля 8-полей (название/ниша/тон/продукт/аудитория/темы/стоп-слова/лого), ссылка NEW в sidebar главной.
2. **`f80165d`** (it.2) — Генерация контент-плана: `creators_planner.py` раскидывает 30-50 слотов по платформам/дням (TG 4/нед, VK 3/нед, YT 1/нед, IG 4/нед), Sonnet заполняет brief'ы. Календарный grid 7-колонок с цветами по типу контента, фильтр платформ чипами, модалка карточки поста с datetime-local/brief/type, cooldown 60 мин против абуза.
3. **`e3d16d7`** (it.3) — Подготовка постов (Шаг B): `creators_prepare.py` — для news Perplexity sonar-pro + Sonnet, для evergreen чистый Sonnet, type∈{image,reels} → DALL-E 3 опционально. Freemium 3 поста/бренд/мес → потом 15-30 ₽. POST `/items/{id}/prepare` + scheduler `creators_prepare_loop` (5 мин, лимит 5/тик, auto-refund при ошибке).
4. **`b7da00c`** (it.4) — TG автопостинг: `creators_publisher.py` + `verify_tg_channel` (getMe+getChat), CRUD каналов с EncryptedString(2048) для bot-token, `creators_publish_loop` (1 мин), кнопки тест/toggle/delete в UI, кнопка «📤 Опубликовать» в модалке ready-поста.
5. **`1d5b2b1`** (it.5) — VK автопостинг: `creators_vk.py` — `verify_vk_community`, 3-шаговый upload фото (getWallUploadServer → POST file → saveWallPhoto), `publish_to_vk_wall` с from_group=1. Модалка подключения с chip-выбором TG/VK и отдельными инструкциями.
6. **`276c614`** (it.6) — Анализ соцсетей: `creators_analyzer.py` — Perplexity (sonar-pro для own / sonar-reasoning-pro для competitor) → Sonnet структурированный отчёт. POST `/brands/{id}/analyze`, fix-price 150/200 ₽ с auto-refund. Блок с двумя кнопками + модалка ввода URL + модалка просмотра markdown-отчёта.

### Что работает на проде

- 21 endpoint под `/creators/*` (brands · calendar · items · prepare · publish · channels · analyze)
- Два scheduler-loop'а с worker_lock
- 5 новых таблиц (auto-created через create_all)
- UI с 5 модалками (brand · item · channel · analysis input · analysis result)
- TG автопостинг через @BotFather бота → канал-админ
- VK автопостинг через community access_token с правами wall+photos+manage
- Биллинг с freemium + auto-refund + Transaction log

### Что НЕ сделано в MVP (см. TODO_NEXT)

- Drag-n-drop постов по календарю
- Цены через `pricing_config` (сейчас захардкожены)
- Push при готовности поста (если канал не подключён)
- YouTube OAuth + upload + Instagram Meta API (рискованно из РФ)
- Bulk prepare для всех planned
- Аналитика опубликованных постов
- Тариф подписки 990 ₽/мес
- Unit-тесты для creators_planner/prepare/analyzer

### Тесты

299 passed на каждой итерации, без регрессий.

---

## Предыдущий спринт (2026-05-12/13) — РЕФАКТОРИНГ + DOLIBARR + САЙТ-РЕДАКТОР

### Что произошло одним абзацем

~50 коммитов за 2 дня. **13-пунктный рефакторинг из аудита** (декомпозиция chatbot_engine, views/shared.js, smoke-тесты, Tailwind build, pricing_config, Alembic, CI миграции, scripts/ cleanup, _pending_tasks, /iterate async, JWT дата, шрифты). **4 фичи из Dolibarr** (PrivacyGuard PII, AiRequestLog, Data-retention cron, MCP Server). **20+ коммитов про сайт-редактор** (code-fence strip, /iterate patch-based, edit-режим srcdoc+sandbox, edit-toolbar, autosave без артефактов, PWA kill-switch, balance-pill против TronLink). **Solutions v2 file-attachments** для 5 пилотов #31-35. **Workflow builder** — анти-паттерны + один триггер на граф. **UX** — вкладка «🎯 Мои запуски».

### По модулям

#### [05-chatbots](docs/modules/05-chatbots.md)
- Декомпозиция `chatbot_engine.py` 3122→2390 строк: `server/messaging/senders.py` + `voice.py` + `server/sandbox.py` (`56ce8d6`)
- Workflow-builder: анти-паттерны в SYSTEM_PROMPT + автоочистка orphan-нод (`6f590f0`)
- Один триггер на граф — мульти-канал → 3 отдельных бота (`0dd0efd`)

#### [06-solutions](docs/modules/06-solutions.md)
- Solutions v2 file-attachments: `type:'file'` в input_schema + seed для пилотов #31-35 (`d47decc`)
- `_abs_path` для `/uploads/*` как URL-path — закрыло «KB reject path» (`aa18470`)
- `async def` run_solution / continue_run — spawn() требует event loop (`51529d8`)
- Промпт «Проверка контрагента» (#36) переписан под ИП + не отказываться (`f57eb42`)
- Orchestra-пилоты через /run+/continue — пустой чат при «Проверке контрагента» (`3387075`)

#### [09-sites](docs/modules/09-sites.md)
- Длинная сага сайта-редактора (~20 коммитов). См. модуль 09 для пошаговой истории. Ключевые:
  - Code-fence strip ДО проверки «<» — закрыло ложные refund'ы 1990 ₽ (`3676540`)
  - `/iterate` → **patch-based** JSON-patches (5-10× дешевле) (`01b0f2d`)
  - Edit-режим: srcdoc + sandbox allow-same-origin БЕЗ inline-script (`1991f6e`)
  - Edit-toolbar: B/I/U/S + размер + цвета + align + list + link + undo/redo (`563d972`, `211fae6`)
  - Autosave не сохраняет contenteditable/__editmode_css/data-edit-id (`8665710`)
  - Edit-режим только по кнопке (`4f09d4a`, `f7c7bc5`)

#### [18-privacy-compliance](docs/modules/18-privacy-compliance.md)
- **PrivacyGuard** — PII-маскировка перед LLM (РФ-адаптация Dolibarr). `server/privacy_guard.py` + 22 теста (`a4d9c77`)
- **AiRequestLog** — таблица `ai_request_logs` + `/admin/ai-stats` + hook в `generate_response` (`0201f81`)
- **Data-retention cron** — анонимизация старых юзеров/КП (152-ФЗ ст. 5) (`ca8d8e5`)

#### [14-mcp-server](docs/modules/14-mcp-server.md)
- **MCP Server** — 10 tools + 3 resources, гайд `docs/mcp_setup.md`, 19 тестов (`d3deb70`, `f528d60`)

#### [00-overview](docs/modules/00-overview.md) / [20-infra-deploy](docs/modules/20-infra-deploy.md)
- **PWA kill-switch SW** — после длинной саги с залипанием юзеров (`7f27ee0`). SW регистрация из icons.js удалена
- Balance-pill: переименование `ai-balance-pill` → `aiche-stat-card` + `<a>` → `<button>` против TronLink/Polkadot инжектов
- **Alembic baseline** + workflow для миграций (`930cb85`)
- CI прогоняет LIGHTWEIGHT_MIGRATIONS + `alembic upgrade head` на чистой SQLite (`b7e2ff2`)
- Tailwind build-step (config + styles.css), CDN остался safe-fallback (`02e0e42`)
- scripts/ cleanup — удалены 9 dead-скриптов + scripts/README.md (`e5f3fdd`)
- `asyncio.create_task` сохранять ссылки в `_pending_tasks` (`36c6af7`)

#### [02-billing-payments](docs/modules/02-billing-payments.md)
- Хардкоды цен → pricing_config (`proposal.create`, `site.iter`) (`cb74200`)

#### [07-proposals](docs/modules/07-proposals.md)
- scripts/sanitize_legacy_proposal_html.py (`65fd341`)

#### [01-core-auth](docs/modules/01-core-auth.md)
- JWT aud/iss дата 2026-06-10 (`dd05348`)

#### [04-chat](docs/modules/04-chat.md) / общее
- views/shared.js — общие хелперы для 11 HTML (esc/fmtRub/aiFetch/humanizeError) (`b4aa243`)
- Smoke-тесты для pdf/docx/xlsx/scheduler/email/audit_log/agent_runner (+23 теста) (`b4aa243`)
- /iterate async-mode — фоновая правка без блокировки worker'а (`d85d58b`)
- Inter/Manrope шрифты удалены (~250KB) (`9640510`)

#### [19-admin](docs/modules/19-admin.md)
- Вкладка «🎯 Мои запуски» в кабинете — список всех бизнес-решений + фильтр + share (`a562b59`)
- runModal footer — главное действие + compact-row экспортов (`fb75bb9`)

### Что осталось юзеру сделать
- ⚠ Ротировать **Google API key** (utility-скрипт был с захардкоженным, удалён `b4aa243`-related)
- ⚠ При желании: `.env` → `DATA_RETENTION_DRY_RUN=true` чтобы включить 152-ФЗ-cron

---

## Предыдущие спринты — краткая сводка по модулям

### Спринт 2026-05-10 — v2-редизайн решений + Marketplace off
- [06-solutions](docs/modules/06-solutions.md): v2 input_schema + multi-stage pipeline на 10 первых решениях (`6d9d2b6`); runModal динамические поля (`d37e71d`); event delegation (`f53aaf2`, `eb9c54c`); бейдж категории + группировка (`ddcd820`)
- [00-overview](docs/modules/00-overview.md): убран Marketplace + Public API в ЛК (`829b1f8`)
- [01-core-auth](docs/modules/01-core-auth.md): strict password policy (`1d60bd0`); login modal text-select (`87e6942`); admin email lowercase (`1287592`)
- [02-billing-payments](docs/modules/02-billing-payments.md): pin `bcrypt<5.0` (`6d70a85`)
- **Security batches:** P0 timing+dup-route+2FA-bypass+env-fail-fast (`dc7eecf`); P1 refresh-revoke+SSRF+perplexity-billing+SVG+rate-limit (`d13cb7e`); P2 indexes+N+1+atomic-rating (`e2a6134`); P3 общий outbound + cleanup fonts (`1c3b5a1`); P4 тесты на security (`aa20867`); P5 aria + 12 CRM-тестов (`aea4140`)

### Спринт 2026-05-08/09 — Perplexity + UI решений + миграции
- [03-ai-core](docs/modules/03-ai-core.md): Perplexity tool в agent_runner + усиление 5 orchestra-пилотов (`c68b468`); лимиты ×2 max_tokens 8k→16k + recency (`00e4048`); sonar-small-chat → sonar (`858c222`); empty PROVIDER_HTTPS_PROXY = override "no proxy" (`7d3e31f`)
- [06-solutions](docs/modules/06-solutions.md): новый раздел бизнес-решений + 5 Perplexity-пилотов (`e2ad867`)
- [01-core-auth](docs/modules/01-core-auth.md): убрана регистрация через Google — только VK+email+QR (`55896d0`)
- [00-overview](docs/modules/00-overview.md): шрифты Inter+Manrope+Material Symbols локально (`22fbef2`); Google Fonts → bunny → локально (`b516ec8`)
- [07-proposals](docs/modules/07-proposals.md): RFC2047-encode кириллицу в From/Subject (Yandex 550 fix) (`5de8aab`); MAIL FROM bare email (`d3222b5`); port 465 SMTP_SSL (`ccaf5fd`)
- [20-infra-deploy](docs/modules/20-infra-deploy.md): Yandex Object Storage backup (`f87641d`); migrate sqlite→postgres preserve int 0/1 → bool (`437f5a5`); PostgreSQL backend support (`6886b28`)

### Спринт 2026-05-04/05 — миграция NL→RU + Dolibarr-features part 1
- [20-infra-deploy](docs/modules/20-infra-deploy.md): toolkit миграции + AI-прокси (`6e9fd0a`)
- [05-chatbots](docs/modules/05-chatbots.md): workflow-labels + drag-drop (`2784f5a`)
- [15-crm](docs/modules/15-crm.md): CRM-интеграции (`2784f5a`)
- [00-overview](docs/modules/00-overview.md): USER_GUIDE.md (`2784f5a`)
- [04-chat](docs/modules/04-chat.md): idempotency через DB + reencrypt-secrets (`5cf647b`)
- [06-solutions](docs/modules/06-solutions.md): cron-планировщик расписаний (`158a96a`)
- [04-chat](docs/modules/04-chat.md): voice Whisper + TTS endpoints (`0a1202b`)
- [01-core-auth](docs/modules/01-core-auth.md) / [10-agents](docs/modules/10-agents-workflows.md): 2FA админки (TOTP) + prompt-injection защита (`04ded59`)
- [07-proposals](docs/modules/07-proposals.md): электронная подпись КП с canvas + audit-trail (`0a8bdf7`)
- [13-public-api](docs/modules/13-public-api.md): Webhooks + UI + документация (`5619224`)
- [12-marketplace](docs/modules/12-marketplace.md): UI каталога + публикации + модерации (`8589cbd`)

### Старые спринты (краткая хроника)
- `1126e50` — XLSX/streaming/auto-flag + WhatsApp + Web Push
- `bcc4cf3` — orchestra-pro: re-run + templates + reactions + DOCX
- `5a91f68` — orchestra: глубокий ресерч (file_extract / vision / browse)
- `fa85629` — multi-agent orchestra v1
- `a2bffc0` — РФ-чеклист 152-ФЗ + 54-ФЗ + шифрование бэкапов
- `d90e2f1` — refresh-rotation single-use + sites public_token + RAG billing

---

## Полная история

Для деталей — `git log --oneline -100` (хронология коммитов) или `git show <hash>` (диффы).
