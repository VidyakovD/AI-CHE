# HANDOVER — что произошло, по модулям

> История спринтов. **Чтобы понять что было сделано — открой нужный модуль ниже.** Для деталей коммитов — `git log --oneline -100`.

**Свежее состояние:** 2026-05-15. Прод на `193.187.92.147`. **299 тестов проходят.**

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
