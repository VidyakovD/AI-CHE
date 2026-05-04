# HANDOVER — для нового AI-ассистента

Если ты впервые в этом проекте — после `CLAUDE.md` прочитай этот файл. Тут **состояние на 2026-05-04 после большой серии спринтов по бизнес-решениям, marketplace, public API + аудит-фикс сессия**.

---

## 🆕 Спринт «Public API: Webhooks + Docs» (2026-05-04 ну совсем-совсем поздно)

Закрыл недостающую часть Public API: Webhooks для получения событий + UI + документация. Теперь юзер может зарегистрировать свой URL и получать POST'ы при событиях (КП открыто клиентом / заявка из бота / отчёт готов / сайт готов / сайт упал).

### Backend

**Модель `ApiWebhook`** ([models.py:455](server/models.py:455)):
- `user_id`, `url`, `secret` (32-hex для HMAC), `events` (CSV из 6 вариантов), `description`
- `is_active`, `last_status`, `last_called_at`, `last_error`, `fail_count`, `total_calls`
- Создаётся через `Base.metadata.create_all` (новая таблица — миграция не нужна)

**Helper `server/webhooks.py`** (новый файл):
- `dispatch_event(user_id, event, data)` — public API: найти все ApiWebhook у юзера с этим событием → fire-and-forget POST
- `deliver_webhook(id, payload, sync)` — sync-вариант для test-эндпоинта
- HMAC-SHA256 подпись `X-Aiche-Signature: sha256=<hex>` от body bytes
- Timeout 10 сек. Auto-disable после 10 ошибок подряд.
- Логика fail_count: `delivered → 0`, `not delivered → +1`, `>=10 → is_active=False`

**Endpoints в `routes/public_api.py`** (4 новых):
- `POST /api-tokens/webhooks` — создать (возвращает secret один раз)
- `GET /api-tokens/webhooks` — список с метриками
- `DELETE /api-tokens/webhooks/{id}` — удалить
- `POST /api-tokens/webhooks/{id}/test` — отправить тестовый POST на URL

**Защита от SSRF** в `_validate_webhook_url`:
- Только `http://` / `https://`
- Блок `localhost`, `127.x`, `0.0.0.0`, `::1`, `10.x`, `172.16-31.x`, `192.168.x`, `169.254.x` (AWS-metadata)

**4 хука на события** (везде fire-and-forget, обёрнуты в try/except):
- `proposal.opened` — [main.py:566](main.py:566) при первом открытии `/p/{token}`
- `record.created` — [chatbot_engine.py:1497](server/chatbot_engine.py:1497) после `save_record` ноды
- `solution.done` — [solutions_orchestra.py:986](server/solutions_orchestra.py:986) после готового orchestra-отчёта
- `site.done` / `site.failed` — [routes/sites.py:702/745](server/routes/sites.py) при завершении генерации сайта

### Frontend

**Новая страница `views/api.html`** (3 вкладки):

1. **🔑 Токены** — управление ApiToken:
   - Список с prefix, статусом, scope-tags, last_used_at, requests_count
   - Модалка создания с чекбоксами scope (proposals/knowledge/solutions/bots)
   - При создании — отдельная модалка «⚠️ Сохраните секрет» с кнопкой «📋 Копировать»
   - Кнопка «Отозвать» с danger-confirm

2. **🔗 Webhooks** — управление ApiWebhook:
   - Список с URL, events-tags, last_status (зелёный/красный), last_called_at, fail_count
   - Модалка создания с URL, описанием, чекбоксами 6 событий
   - Кнопка «🧪 Тест» — отправляет POST с фиктивным `event=test.ping` payload, показывает результат как toast/alert
   - Кнопка «Удалить» с confirm
   - last_error показывается красным под строкой если есть

3. **📖 Документация** — статичная страница:
   - Авторизация (Bearer ai_che_xxx)
   - Список endpoints (`/api/v1/me`, `/api/v1/proposals/generate|{id}`)
   - Пример curl для генерации КП + JSON-ответ
   - Webhooks: payload-схема, список 6 событий, Python-snippet верификации `hmac.compare_digest(...)`
   - Лимиты и защита

**Навигация:**
- Sidebar в `index.html` — пункт «🔌 Public API» сразу после «Marketplace»
- Command palette (Ctrl+K) — пункт «Public API + Webhooks 🔌»

**`main.py`** — endpoint `serve_api_docs` для `/api.html`.

### Тесты
- 164/164 pytest passed
- JS sanity 10 файлов OK
- Preview e2e под залогиненым юзером:
  - `/api.html` грузится, все 3 таба + 3 модалки на месте, refreshTokens/refreshWebhooks функции определены
  - `POST /api-tokens/webhooks` → 200 + secret + webhook_id, появляется в списке
  - `POST /api-tokens/webhooks/{id}/test` → корректно вызывает HMAC POST на URL, возвращает `{delivered, status, error}` (404 от webhook.site/test-aiche — ожидаемо)
  - `DELETE /api-tokens/webhooks/{id}` → удаляется
  - Console errors = 0, server errors = 0

### Полный flow юзера:
1. Юзер открывает `/api.html` → вкладка «🔗 Webhooks» → «+ Новый webhook»
2. Вводит URL `https://my-crm.com/aiche-hook`, чекает `proposal.opened` + `record.created`
3. Backend сохраняет ApiWebhook + secret, показывает secret один раз
4. Юзер встраивает secret в свой обработчик (Python/Node/PHP) — верифицирует через `hmac.compare_digest`
5. Когда клиент открывает КП по public-link → backend вызывает `dispatch_event(user_id, "proposal.opened", {...})` → POST на URL юзера
6. Юзер видит в кабинете: last_status=200, fail_count=0, total_calls=1
7. Если URL упадёт 10 раз — webhook auto-disable, юзер увидит «отключён» статус

---

## 🆕 Спринт «Marketplace UI» (2026-05-04 совсем поздно ночью)

Backend Marketplace был готов с прошлого спринта (`75e2462`), но UI не было — каталог, публикация, модерация. Закрыл UI:

### `views/marketplace.html` (НОВЫЙ файл)
- 2 вкладки: «🛍 Каталог» + «📤 Мои публикации».
- **Каталог:** фильтры по категории (Продажи / Поддержка / Бронирование / Контент / HR / Другое) + чекбокс «Только бесплатные». Карточки с обложкой, рейтингом ★★★★☆, ценой, счётчиком установок. Skeleton-loaders на загрузке.
- **Detail-модалка:** обложка крупная, описание, метрики (📦 N установок, ⭐ X.Y), цена + кнопка «📥 Установить». Использует `aiCostHint` для preview списания. При попытке установить свой бот — кнопка disabled «Это ваш бот».
- **Мои публикации:** карточки с бэйджами «⏳ На модерации» / «✓ Опубликовано» / «📦 Снято» + кнопка «🗑 Снять».

### `chatbots.html` — кнопка «📤» в действиях бота
- Tooltip объясняет revenue-split 70%.
- Модалка `publishModal` с полями: Название, Описание, Категория (select), Цена (number), URL обложки. Все ограничения как в backend (≤100/2000/10 000 ₽).
- При успехе → `aiToast('Отправлено на модерацию!', 'success')`.

### `index.html` навигация
- Sidebar: новый пункт «🛍 Marketplace» между «Чат-боты» и «Создание сайтов».
- Tooltip объясняет «70% автору с каждой установки».
- В command palette (Ctrl+K) добавлена строчка «Marketplace ботов 🛍».

### `admin.html` — секция «Marketplace · Модерация»
- Сайдбар: пункт «🛍 Marketplace» рядом с Audit Log.
- Список pending-листингов (`/marketplace/admin/pending` requires admin): автор, цена, описание, превью system_prompt в `<details>`.
- Кнопки «✓ Одобрить» / «✗ Отклонить» вызывают правильные пути `/marketplace/admin/listings/{id}/approve|reject`.

### Регрессии найдены и исправлены до коммита
- В первой версии UI обращался на `/admin/listings/...` без префикса `/marketplace` — поправил, потому что роутер имеет `prefix="/marketplace"`.
- В первой версии UI ожидал `it.author_email` от backend, а backend отдаёт только `author_id` — поправил.
- Backend `admin/pending` возвращает `system_prompt_preview` (300 chars) и `has_workflow` (bool), не полные объекты — UI адаптирован.

### `main.py`
- Добавлен `serve_marketplace` endpoint для `/marketplace.html`.

### Тесты
- 164/164 passed (новый файл — pure UI, без backend-изменений)
- JS sanity — 9 файлов OK
- Preview-сверка с залогиненым юзером:
  - `/marketplace.html` грузится, title корректный, все DOM-элементы (catalogGrid / listingModal / 2 таба) на месте
  - `/marketplace/listings` → 200, `/marketplace/my-listings` → 200, `/marketplace/admin/pending` → 403 (юзер не админ — ожидаемо)
  - Console errors = 0, server errors = 0

---

## 🆕 Спринт «UX добавки — 6 фич» (2026-05-04 поздно ночью)

После предыдущего спринта (5 quick wins) юзер дал «делай что можешь». Сделал ещё 6 mid-priority улучшений в одном коммите.

### #1. Web Push на ключевые события
Хуки уже были на `record.created` и `proposal.opened` — добавил ещё на:
- **`site.generate_done`** ([routes/sites.py](server/routes/sites.py:691)): «Сайт «X» готов. Открыть превью или скачать ZIP»
- **`site.generate_failed`** ([routes/sites.py](server/routes/sites.py:715)): «Не удалось сгенерировать сайт. Деньги возвращены»
- **`solution.orchestra_done`** ([solutions_orchestra.py](server/solutions_orchestra.py:983)): «Готов отчёт: «Полный SWOT-анализ». Стоимость: 150 ₽»

### #2. Глобальные шорткаты Esc + Ctrl+K command palette
- **Esc** в `icons.js` — закрывает верхнюю открытую модалку (`.show` или `[role=dialog]`), не трогая ai-tour и ai-notif (у них свой обработчик). Не срабатывает в input/textarea (там Esc может быть нужен для blur).
- **Ctrl+K / Cmd+K** — открывает command palette `aiCmdPalette`: 9 статических пунктов навигации (главная, бизнес-решения, чат-боты, КП, презентации, сайты, агенты, токены, настройки) + динамические из `/user/recent-objects` (3 бота / 3 КП / 3 сайта). ↑↓ навигация, Enter открыть, Esc закрыть. Поиск по labels.

### #3. Toast helper + замена aiAlert на toast (16 мест)
- `window.aiToast(msg, type, opts)` в `icons.js` — non-blocking уведомление в правом нижнем углу с auto-fade. 4 типа: success/info/warn/error. Auto-fade 2.8с (5с для error). Click → закрыть досрочно.
- Mass-replace во всех views: `await aiAlert(<short_msg>, 'success'|'info')` → `aiToast(<msg>, 'success'|'info')` где `<msg>` — однострочное короткое сообщение без `\n`. **16 замен** в proposals (12), index (2), sites (1), presentations (1).
- Длинные `aiAlert` с `\n` НЕ тронуты — они блокирующие, юзер должен прочитать.

### #4. Workflow node labels — проверено, уже сделано
Решения уже имеют русские labels в `agents.html`: `trigger_tg → "Telegram"`, `output_tg → "Ответить в TG"`, `kb_search → "БЗ: поиск"`. Технические идентификаторы не показываются юзеру.

### #5. Skeleton loader helper + применение в proposals.html
- `window.aiSkeleton(preset, count)` в `icons.js`: 4 пресета — `lines` (3 серых строки разной длины), `cards` (N карточек), `proposal` (3 карточки с подсказкой «AI читает сайт клиента и собирает КП…»), `orchestra` («AI-агенты работают параллельно… 1-3 минуты»). Анимация shimmer-эффект 1.6s.
- В `generateProposal()` в proposals.html — пока идёт 15-30 сек генерация, в `#pResult` отрисовываются skeleton-карточки вместо белого экрана.
- Завершение → `aiToast('КП готово!', 'success')`.

### #6. Touch-targets fix для мобильных через CSS-инжект
- Universal `<style id="ai-touch-fix-style">` в `icons.js` — на устройствах с `pointer:coarse` (тач-экраны):
  - `text-[10px]` кнопки → `padding:6px 10px; font-size:11px`
  - `text-[11px]` кнопки → `padding:6px 10px`
  - Чекбоксы и radio → `transform: scale(1.15)`
  - Все `button/a[role=button]/.btn*` получают `::after` псевдо-расширение hit-area до min 44×44px (z-index:-1, не перекрывает соседей)
- Visual-размер не меняется на десктопе (только pointer:coarse). Соответствует Apple HIG / Material Design рекомендациям.

### Тесты
- 164/164 passed
- JS sanity-check 8 файлов — OK
- Preview-сверка через preview_eval с залогиненым юзером:
  - `aiToast` — 3 уведомления отрисовались в стеке
  - `aiSkeleton` — все 4 пресета возвращают валидный HTML
  - `aiCmdPalette.open()` — 9 пунктов меню, поиск работает
  - Console errors — 0

---

## 🆕 Спринт «Friendly UX — 5 quick wins» (2026-05-04 ночью)

Юзер запросил «предложения по friendly-дизайну для пользователей». Сделал 5 параллельных улучшений в одном спринте — всё через `views/icons.js` (загружается на каждой странице) + новые endpoints в `server/routes/user.py`.

### #1. Колокольчик уведомлений + dashboard «Недавнее»
- **Backend:** новые endpoints `/user/notifications/recent` (последние 30 событий за 14 дней + счётчик непрочитанных) и `/user/notifications/seen` (сбросить бейдж). Источник — `ActionLog` отфильтрованный по whitelist `_NOTIFY_USER_ACTIONS` (record.created, proposal.opened, solution.orchestra_done и т.д.). Маппинг action → emoji + русская фраза в `_NOTIFY_LABELS`.
- **Backend:** `/user/recent-objects` — последние 3 бота / 3 КП / 3 сайта.
- **Модель:** `User.notifications_last_seen_at` (DateTime nullable) + миграция.
- **Фронт (icons.js):** `_initNotificationsBell()` — floating-кнопка `🔔` в правом верхнем углу всех страниц, бейдж с числом непрочитанных, dropdown с 10 событиями. Пульсирующая анимация при unread > 0. Обновление каждые 60 сек. Esc / клик вне — закрытие.
- **index.html welcome:** под кнопками «Начать диалог / Бизнес-решения» — 6 quick-action чипов (`Пост в соцсети`, `Письмо клиенту`, `Скрипт звонка`, `Идея бизнеса`, `SWOT`, `Описание оффера`) + блок «Недавнее» с тремя секциями (боты/КП/сайты) — подгружается из `/user/recent-objects` если есть хоть один объект.

### #2. Auto-save черновиков (localStorage)
- **icons.js:** `window.aiDraft` — четыре метода: `save(key, data)`, `load(key, ttlMs=24h)`, `clear(key)`, `attach(formEl, collectFn)` (debounced 1.5s + auto-save при `beforeunload`), `confirmRestore(key)` (промпт «Найден черновик от 14:03 (5 мин назад). Восстановить?»).
- **proposals.html:** в `openCreate()` — проверка черновика → restore prompt → `attach`. После успешного `saveProject()` — `clear('proposal_new')`.
- TTL: 24 часа. NS: `aiche_draft_<key>` в localStorage.

### #3. Welcome-tour для новичков
- **icons.js:** `window.aiTour.maybeStart(steps)` — проверяет `/user/onboarding` и localStorage flag, если не видел — показывает 4-шаговый overlay (emoji + title + body + dots-progress). Кнопки «Пропустить» / «Дальше →» / «Понятно, поехали 🚀». Esc = пропустить.
- **Модель:** `User.onboarding_completed` (Boolean default 0) + миграция. Endpoints `/user/onboarding` (GET) и `/user/onboarding/complete` (POST).
- **index.html:** при `renderWelcome()` для залогиненого юзера — 4 шага: «Привет → Чат с моделями → Бизнес-решения → Боты/КП/Сайты».

### #4. Cost preview + sticky-balance
- **icons.js:** `window.aiBalance` — sticky-плашка `💰 730 ₽` рядом с колокольчиком в правом верхнем углу. Цвет зелёный/жёлтый/красный по уровню. Click → `/?tab=tokens`. Refresh каждые 60 сек или вручную через `aiBalance.refresh()`.
- **icons.js:** `window.aiCostHint(costKop)` — возвращает HTML «Спишется 50 ₽ · Баланс 730 ₽ → останется 680 ₽» или «Не хватает 220 ₽ — пополнить» (с кнопкой) если денег нет.
- **proposals.html:** под кнопкой «Сгенерировать (50 ₽)» — `<div id="pGenCostHint">` с aiCostHint. Обновляется через `updateGenButton(p)`.

### #5. humanizeError + массовая замена `aiAlert(e.message)`
- **icons.js:** расширил `aiFetchError` — теперь распознаёт «Ошибка 402 NNN», `Internal Server Error`, traceback'и Python и заменяет на дружеский текст. Алиас `window.humanizeError` для понятности. Новый `window.aiAlertError(e)` — shortcut: `catch(e){ aiAlertError(e); }` + автоматический `aiNeedTopup()` modal на 402.
- **Mass-replace:** во всех 7 user-facing views (admin/agents/chatbots/index/presentations/proposals/sites) заменил **50 вхождений** `aiAlert(e.message, 'error')` → `aiAlertError(e)`. PowerShell regex с UTF-8 без BOM, не сломал JS-синтаксис (sanity-check Node прошёл).

### Тесты
164/164 passed после всех правок. JS-syntax-check 8 файлов — OK. Локальный smoke (Python 3.14 bcrypt-conflict не даёт сделать login локально, но endpoint'ы зарегистрированы и возвращают 401 без auth — корректно).

---

## 🆕 Спринт «Аудит-фиксы» (2026-05-04 вечером)

Прошлая сессия запросила полный аудит проекта (тесты + security + новые идеи + баги). Найдено и закрыто 13 пунктов:

### P1 — функциональные/security баги
- **B1** `orchestra_start` не возвращал `run_id` — фронт получал `null` ([routes/solutions.py:391](server/routes/solutions.py:391)).
- **B2** Unreachable code в `orchestra_compare_get` после `return out` (мёртвый блок ссылался на отсутствующие переменные).
- **B3** Wazzup webhook secret из URL `?secret=` → теперь поддерживается header `X-Wazzup-Signature` (приоритетный) с обратной совместимостью query.
- **B4** Race в `register_refresh_jti`/`revoke_refresh_jti`: read-modify-write мог терять чужой jti при одновременных login → юзер ловил «refresh_reuse» 401. Введён `_atomic_jtis_update` с `with_for_update` (Postgres), re-fetch в текущей сессии (SQLite).

### P2 — серьёзные
- **B5** Image URLs (`logo_url`/`signature_url`/`cover_image_url`) теперь whitelist'ятся: только `http(s)://`, относительный `/` или `data:image/...`. `javascript:`/`vbscript:`/`data:text/html` отбрасываются.
- **B6** Marketplace: повторная установка одного **платного** листинга одним юзером блокируется (anti-pump схема против collusion-аккаунтов).
- **B7** CSRF middleware: убрал «Bearer >10 char пропускает CSRF». Теперь если есть cookie access_token — CSRF обязателен независимо от Authorization-header. Чистый Bearer без cookie → CSRF не требуется (правильное поведение для API-клиентов).
- **B9** `requests_count` Public API: теперь atomic `UPDATE ... = requests_count + 1` (раньше read-modify-write = race в multi-worker).
- **B10** `authenticate_token` теперь требует `is_verified` (раньше при unverify админом токен продолжал работать).

### P3 — мелочи
- **B11** Auto-flag dedup: 24ч cooldown теперь живёт в ActionLog, не in-memory dict (multi-worker корректно).
- **B12** `knowledge._abs_path`: defense-in-depth — путь обязан быть в `/uploads/` или `/backups/`, иначе ValueError. `extract_text` ловит и возвращает "".
- **B13** Добавлен `/healthz` endpoint (200 + `{"status":"ok"}`) — для мониторинга/балансировщика без БД-запросов.
- **B14** В `orchestra_start_compare` deepcopy(orch) вместо `json.loads(json.dumps(orch))`.

### Что НЕ закрыто этой сессией (документировано)
- **B8 Idempotency-Key** — оставил in-memory + добавил большой комментарий «корректно работает только при workers=1». Прод сейчас на 1 воркере — это OK. При масштабировании заменить на Redis или новую SQL-таблицу `IdempotencyRecord` с `UNIQUE(user_id, key)`.

### Тесты
164/164 passed после фиксов. Smoke на dev-сервере: `/healthz` 200, `/api/v1/me` без auth 401.

### Security ревью на этой же сессии — что хорошо защищено (для будущих сессий не дублировать)
- ✅ Public API: `hmac.compare_digest`, scope-проверка, секрет показывается только при создании
- ✅ Solutions: SSE/share/restage/docx/xlsx/reaction — все владелец-only через `run.user_id == user.id`
- ✅ `_validate_attachments`: `resolve+relative_to`, лимиты 25 МБ × 5
- ✅ SSRF в `tool_browse_url`: explicit redirect-revalidation + private CIDR блок
- ✅ Sites: 160-bit public_token + sandbox iframe в null origin
- ✅ Refresh-rotation single-use: reuse → revoke ALL
- ✅ Admin: 45 routes, 46 `require_admin` (consistent)

### Найденные новые идеи (приоритет — для отдельных спринтов)
1. **Marketplace UI** (приоритет №1, backend готов)
2. **Webhook'и для Public API** — закрыть SaaS-интеграции
3. **Электронная подпись КП** — canvas-подпись на `/p/{token}` + audit-trail
4. **CRM-интеграции** Bitrix24/amoCRM на `save_record` ноду
5. **Voice-режим в чате** (Whisper input + ElevenLabs/SaluteSpeech output)
6. **Rate-limit на orchestra-start** — сейчас юзер может в цикле сжечь весь баланс
7. **Cron-планировщик пользовательских orchestra** — превращает разовые покупки в подписку
8. **Prompt-инжекция защита** для tool_run_llm — оборачивать пользовательский input в `<user_data>` теги
9. **A/B-тест двух промптов** в одном Solution + сбор training-data из 👍/👎
10. **Exit-criteria для orchestra стадий** — `validate_output: "min_length:200"`/`json_schema:...` + retry × 1

---

## 🆕 Спринт «Сравнение моделей + Marketplace + Public API» (2026-05-04, `75e2462` + `963c365`)

### Сравнение моделей (compare runs)
- `POST /solutions/{id}/orchestra/start-compare {input, attachments, models[]}` запускает 2-3 параллельных runs одного Solution на разных моделях (whitelist: claude-sonnet/opus/haiku, gpt-4o).
- Каждый run хранит свою «кастомную» orchestra в `run.context._compare_orchestra` (deep-copy с переопределённой `default_model`). Runtime подхватывает custom orchestra если она есть в context.
- `chat_id` имеет префикс `cmp_<group>_<model>` для группировки.
- `GET /solutions/compare/{group}` → снимок состояния всех runs группы.
- UI: details «🔬 Сравнить модели» в orchestra-форме с чекбоксами + N-колоночный grid с прогрессом каждой модели + кнопки «Открыть отчёт» / «PDF» по каждой.

### Marketplace ботов
- `BotMarketplaceListing` (snapshot system_prompt + workflow_json + name + price_kop + cover_image_url + is_approved + installs_count + rating_sum/count)
- `BotMarketplaceInstall` (listing_id + installer_id + paid_kop + rating + review)
- 12 endpoints в `server/routes/marketplace.py`: публикация, каталог, my-listings, install (с 70/30 revenue split), review, admin pending/approve/reject.
- UI каталога ещё не сделан — только backend.

### Public API
- `ApiToken` (prefix 8-hex + sha256(secret) + scopes CSV + last_used_at + requests_count)
- Управление в кабинете: `mgmt_router` /api-tokens (POST/GET/DELETE)
- Public endpoints: `api_router` /api/v1/me, /api/v1/proposals/generate, /api/v1/proposals/{id}
- Auth: `Authorization: Bearer ai_che_<prefix>_<secret>` через `authenticate_token(request, db, required_scope=...)` с hmac.compare_digest на sha256.
- CSRF middleware пропускает Bearer >10 символов — наш токен ~56 символов работает.

### Race-condition fix
Multi-worker uvicorn: 2 процесса вызывают `Base.metadata.create_all` одновременно после новых таблиц → один проходит, второй падает с «table already exists». Catch'им и игнорируем — это норма.

### Тесты
164/164 (+6 новых: compare endpoints, marketplace endpoints+models, api_token hash, api_token authenticate_invalid).

---

## 🆕 Спринт «Production апгрейды + WhatsApp + Web Push» (2026-05-03, `1126e50` + `d2e5e4c`)

### XLSX-экспорт финального отчёта
- `server/xlsx_builder.py` — markdown→XLSX с разнесением каждой таблицы на отдельный лист; первый лист «Отчёт» с резюме (3-5 первых параграфов).
- `GET /solutions/runs/{id}/xlsx` → blob-скачивание + кнопка 📊 XLSX в UI.

### Streaming output финального synthesize-stage'а
- `_llm_call_stream_anthropic` через `AsyncAnthropic.messages.stream` — токены идут по мере генерации.
- Throttle нотификаций 600мс (не флудить SSE).
- Активируется автоматически для финального synthesize если model=claude*. Иначе fallback на non-stream.
- Юзер видит как пишется итоговый отчёт live, не ждёт минуту до конца.

### Stage type `generate_image` (DALL-E)
- `_run_generate_image` через `tool_generate_image` → возвращает `![](url)` markdown.
- Synthesizer может вставить inline-картинку — markdown_to_pdf через xhtml2pdf автоматически рендерит.

### Auto-flagging низкого качества
- При 3+ 👎 на одно решение за 7 дней → email админу через `_send` + audit-лог `solution.auto_flagged`.
- Дедуп по solution_id раз в 24ч (in-memory).

### WhatsApp канал через Wazzup24
- `ChatBot.wazzup_api_key` (encrypted) + `wazzup_channel_id`
- `send_whatsapp(api_key, channel_id, chat_id, text)` в `chatbot_engine.py` — POST в `https://api.wazzup24.com/v3/message`
- Webhook `POST /webhook/wazzup/{bot_id}` с HMAC-secret в URL, парсит `messages[]`, фильтрует inbound + matching channelId, зовёт `handle_message(platform="whatsapp")`.
- Все 7 шаблонов работают через WhatsApp без изменений.

### Web Push API через VAPID
- `PushSubscription` (endpoint + p256dh + auth + user_agent)
- `server/push.py: push_to_user` через pywebpush, удаление 410/404 endpoint'ов
- 6 endpoints `/user/push/{vapid-public, subscribe, unsubscribe, status, test}`
- UI: блок «🔔 Push-уведомления» в кабинете → Настройки
- Хуки: новая заявка из бота (`save_record` нода) → push владельцу; клиент открыл КП по `/p/{token}` → push.
- VAPID-ключи на проде: `VAPID_PUBLIC_KEY=BMWIjDEoiE4DrybHWJ5q3NOsSR5Q...`, `VAPID_PRIVATE_KEY_FILE=/root/AI-CHE/.vapid_private.pem` (0o400).

### requirements.txt
- + `python-docx==1.1.2`
- + `pywebpush==2.0.0`

### Тесты
158/158 (+5 новых: XLSX builder, generate_image dispatch, new endpoints registered, send_whatsapp helper).

---

## 🆕 Спринт «UX-полировка orchestra + 3 новых решения» (2026-05-03, `bcc4cf3`)

### Re-run отдельного stage'а
- `solutions_orchestra.restage(run_id, stage_id, extra_instruction)` — сбрасывает target stage и все следующие, перезапускает их с extra_instruction в user_prompt target'а, обновляет final_output.
- `POST /solutions/runs/{id}/restage {stage_id, extra_instruction}` — endpoint.
- UI: «↻ перегенерировать» рядом с каждой готовой стадией → prompt → запуск.

### Templates: сохранить запуск как шаблон
- `SolutionRunTemplate` (user_id + solution_id + name + user_input + attachments_json)
- 4 endpoints: save-template, list, delete, run-template
- UI: блок «📂 Запустить из шаблона» в orchestra-форме с чипами + ✕ удаления.

### Reactions 👍/👎/💡
- `SolutionRun.user_mark` + `user_comment` + миграции
- `POST /solutions/runs/{id}/reaction {mark, comment}` — `up/down/idea/clear`
- UI: ряд кнопок под отчётом + prompt комментария при 👎/💡

### DOCX-экспорт
- `server/docx_builder.py` — markdown→DOCX через python-docx (heading H1-H4, **bold**, lists, tables с шапкой, разделители)
- `GET /solutions/runs/{id}/docx` → blob-скачивание

### 3 новых orchestra-решения
- **Аудит лендинга** (250 ₽, 7 stages): extract_urls + parallel_browse + опц. vision-скриншот → 3 параллельных аналитика (UX/SEO/CRO) → план + ГОТОВЫЕ ТЕКСТЫ (3 варианта H1, CTA, блок «Почему мы»)
- **Юридическая проверка договора** (350 ₽, 7 stages): file_extract DOCX/PDF → структуризация (стороны, предмет, сроки) с цитатами → web_search применимых норм → 3 параллельных юриста (риски/существенные/императивные нормы) → ПРОТОКОЛ РАЗНОГЛАСИЙ со ссылками на статьи ГК РФ + текст для отправки контрагенту
- **Конкурентный анализ ниши** (углублён): теперь реально качает 5 сайтов через parallel_browse, анализирует реальный текст с их страниц.

### Расширение runtime новыми stage-типами
- `file_extract` (PDF/DOCX/XLSX/CSV/TXT через `knowledge.extract_text`, селектор attachments)
- `vision_describe` (картинка → Claude Haiku через `describe_image_via_claude`)
- `extract_urls` (regex)
- `parallel_browse` (asyncio.gather с N URL'ов)

### Attachments в Solution Run
- `SolutionRun.attachments_json` (JSON массив {file_url, name, mime, kind, size})
- В `Solution.orchestra_json` поле `requires_attachments[]` — описание для UI: kind/label/accept/required/hint
- API `POST /orchestra/start` принимает `attachments[]` с валидацией (path-traversal защита, лимит 25 МБ × 5)
- UI: блок «📎 Дополнительные данные» в orchestra-модалке, file-input + статус загрузки + required-валидация

### Тесты: 154 → 155 → 158 → 164 (последовательно по спринтам)

---

## 🆕 Спринт «Multi-Agent Orchestra v1» (2026-05-02, `fa85629`)

Концепт: Solution.orchestra_json содержит JSON-граф stage'ов. Stage'и идут по порядку; внутри parallel_llm — N веток через asyncio.gather. Промпты ссылаются на предыдущие stage'и через `{{<id>.output}}` / `{{<id>.outputs[i]}}`.

### Runtime (`server/solutions_orchestra.py`)
- 5 stage-типов: web_search / browse_url / llm / synthesize / parallel_llm
- Биллинг: каждый llm-stage списывается по реальным токенам × margin (×5 из ai.improve_margin_pct). web_search/browse_url бесплатны.
- Стриминг: subscribe_run/unsubscribe_run с asyncio.Queue для SSE-подписчиков. Throttle нотификаций.
- Функция _calc_cost_kop: формула real ≈ in*0.08 + out*0.30 коп / 1k токенов (Sonnet baseline) × margin_pct.

### 3 пилотных решения
- **Конкурентный анализ ниши** (150 ₽): web_search → 5 параллельных deep-аналитиков → стратег с картой рынка
- **Полный SWOT-анализ** (150 ₽): web_search контекст → 4 параллельных квадранта (S/W/O/T) → Opus-стратег с TOWS + ТОП-3 приоритетами
- **Контент-план на месяц** (200 ₽): trend-scout → 3 параллельных копирайтера (VK/TG/Insta) → планировщик с единым календарём

### API (`server/routes/solutions.py`)
- `POST /solutions/{id}/orchestra/start { input } → run_id` (фон-таска через asyncio.create_task)
- `GET /solutions/runs/{id}` → снимок stages_state + final_output
- `GET /solutions/runs/{id}/stream` → SSE live-progress
- `POST /solutions/runs/{id}/share` → public_token + share_url
- `GET /s/{public_token}` → публичный PDF/markdown без auth

### UI (`views/index.html`)
- Бэйдж «✨ PRO · Multi-Agent» на orchestra-карточках в `/index.html` (виден сразу когда открываешь «Бизнес-решения»)
- Цена «до X ₽» для orchestra
- Live-progress: список stages с иконками ⏸ ⏳ ✅ ❌ + стоимость каждого
- SSE-стрим (cookie auth) с fallback на 1.5s polling
- Кнопки «📄 Скачать PDF» и «🔗 Поделиться»

### Модель
- `Solution.orchestra_json` (TEXT) + миграция
- `SolutionRun.stages_state` + `final_output` + `pdf_path` + `total_cost_kop` + `user_input` + `public_token`

### Тесты: 154/154 (+6 новых для orchestra)

---

## Спринт «РФ Compliance» (2026-05-02, `a2bffc0`)

### Что закрыто кодом
- **AES-256-GCM шифрование бэкапов БД** (`scheduler._db_backup_tick`): chat.db шифруется перед записью в `/backups/*.enc`. Plaintext `.tmp` удаляется сразу. Ключ из env `BACKUP_ENCRYPTION_KEY` или auto-generated файла `.backup_encryption_key` (0o400). Утилита расшифровки — `scripts/restore_backup.py`.
- **Чек 54-ФЗ в ЮKassa**: payment_subject="service" + payment_mode="full_payment" + tax_system_code (env, default 2=УСН доходы) + vat_code (env, default 1=Без НДС). Юзер настраивает через `YOOKASSA_VAT_CODE` и `YOOKASSA_TAX_SYSTEM_CODE`.
- **Маркетинговое согласие отдельно от оферты**: `User.marketing_consent` + `marketing_consent_at`, миграция, чекбокс в форме регистрации (НЕ предзаполнен), toggle в кабинете → Настройки → 📬 Маркетинговая рассылка.
- **Из payment-логов убраны суммы**: `payment.webhook` и `payment.confirm` логируют только `payment_id` (суммы остаются в Transaction).
- Документ `docs/compliance_ru.md`.

### Что юзер обязан сделать руками
1. **🔴 БЛОКЕР: подключить SMTP** — на проде SMTP_HOST не задан, юзеры не получают verification-код. Решение: Unisender / SendPulse / Yandex 360.
2. **Сохранить ключ шифрования бэкапов** в 1Password (значение в чате, плюс файл `/root/AI-CHE/.backup_encryption_key`).
3. **Подключить ОФД** в ЛК ЮKassa (Атол Онлайн / Контур.ОФД).
4. **Регистрация в РКН** как оператор ПДн (https://pd.rkn.gov.ru/).
5. **Обновить политику конфиденциальности**: ЮKassa, Anthropic, OpenAI, SMTP-провайдер, Yandex.
6. **Бэкапы вне РФ-сервера** или миграция primary в РФ.

---

## Спринт «Refresh single-use + sites public_token + RAG billing» (2026-05-02, `d90e2f1`)

### A. Refresh-token rotation single-use
- `User.refresh_jtis` (JSON-список до 10 активных jti, multi-device)
- `create_refresh_token(user_id, email, jti=None)` принимает jti
- `register_refresh_jti` / `revoke_refresh_jti` / `revoke_all_refresh_jtis` / `is_refresh_jti_active` в `auth.py`
- `/auth/login`, `/verify-email`, `/reset-password`, `/oauth/exchange`, `/qr-login/poll` — все вызывают `register_refresh_jti` после issue
- `/auth/refresh` — проверяет jti в наборе. **Reuse-detection** (jti уже использован) = security-incident → `revoke_all_refresh_jtis` + audit-лог critical + 401
- `/auth/logout` — revoke текущий jti
- `/auth/reset-password` — revoke ВСЕ refresh-сессии после смены пароля
- Grace-period для legacy токенов без jti

### B. Sites public_token (вместо int_id)
- `SiteProject.public_token` (~160 bit `secrets.token_urlsafe(20)`)
- Backfill в `apply_lightweight_migrations`: для уже опубликованных сайтов с `hosted_path != NULL` генерим token и переписываем hosted_path. Старые URL `/sites/hosted/{int_id}/` после деплоя возвращают 404 — юзер должен зайти в `/sites.html` и взять новую ссылку.
- Endpoint `/sites/hosted/{public_token}/{full_path:path}` валидирует token (16-64 alnum/-_), lookup → физическая папка `_sites_host_base/<project.id>/`. Sandbox-обёртка для HTML с null-origin сохранена.
- Убран `app.mount('/sites/hosted', StaticFiles(...))` из `main.py` (он обходил sandbox + token-проверку).

### C. Storage-биллинг для RAG-файлов
- `KnowledgeFile.last_billed_at` + миграция
- Лимиты в `knowledge.py`: 50 файлов × 50 МБ × 2 ГБ/юзер
- `_storage_billing_tick` в `scheduler.py` — UNION SUM(StoredAsset) + SUM(KnowledgeFile) за один проход, общий лимит 100 МБ chunks на user
- Просрочка >7д: `KnowledgeFile.enabled=False` (не в RAG, файл цел)
- Просрочка >37д: hard-delete файла + строки + чанков (cascade)

---

## 🆕 Спринт «Security audit» (2026-04-30 → 2026-05-02, `cc5afa5`)

Юзер запросил security-review через subagent + ручной чеклист. Найдено + зафикшено 13 пунктов:

### P1 (критичные)
- VK webhook без secret → требуем `vk_secret` обязательным + `compare_digest`
- SSRF в agent `tool_browse_url`: добавлен `_host_resolves_to_private`, scheme whitelist, revalidate редиректов
- SSRF в `presentation_builder._add_remote_image` — то же
- `/knowledge/search` лимит длины `q` (1000 симв) — иначе abuse OpenAI embeddings
- RAG лимиты сначала ужесточены до 20×20×500МБ (потом подняты обратно с биллингом)
- `is_verified` check в `brief-assist`, `/voice/parse`

### P2 (XSS / IDOR / info-leak)
- bleach-санитизация `generated_html` КП в legacy fallback + `edit_section` + `save-html`
- `/agent/{id}/ws` + `/stream` — owner-check (cookie `access_token` или `?token=`)
- `/auth/login` не возвращает `user_id` для unverified
- `/resend-verify` принимает email + не палит существование
- TG-link: rate-limit (10/10мин на tg_user_id) + email-alert при привязке/перепривязке

### P3 (hardening)
- `/p/{token}` PDF — `relative_to(uploads_root)`
- YooKassa race — validated false-positive

### Что отложено отдельным спринтом
- ⏸ JWT `aud`/`iss` strict verify — нужен grace-period
- ⏸ starlette upgrade — pinned в FastAPI 0.111

---

---

## 🆕 Спринт «РФ Compliance» (2026-05-02, последний, `a2bffc0`)

Юзер: пришёл chek-list 152-ФЗ + 54-ФЗ + российская инфра — что есть, что нужно сделать.

### Что закрыто кодом
- **AES-256-GCM шифрование бэкапов БД** ([scheduler.py](server/scheduler.py)): `_db_backup_tick` теперь шифрует chat.db перед записью в `/backups/*.enc`. Plaintext `.tmp` удаляется сразу. Ключ из env `BACKUP_ENCRYPTION_KEY` или auto-generated файла `.backup_encryption_key` (права 0o400, .gitignore'd). Утилита расшифровки — [scripts/restore_backup.py](scripts/restore_backup.py).
- **Чек 54-ФЗ в ЮKassa** ([routes/payments.py](server/routes/payments.py)): `payment_subject="service"` + `payment_mode="full_payment"` + `tax_system_code` (из env, default 2=УСН доходы) + `vat_code` (из env, default 1=Без НДС). Юзер настраивает через `YOOKASSA_VAT_CODE` и `YOOKASSA_TAX_SYSTEM_CODE`.
- **Маркетинговое согласие отдельно от оферты** ([models.py](server/models.py), [routes/auth.py](server/routes/auth.py), [routes/user.py](server/routes/user.py), [views/index.html](views/index.html)): `User.marketing_consent` + `marketing_consent_at`, миграция, чекбокс в форме регистрации (НЕ предзаполнен), toggle в кабинете → Настройки → 📬 Маркетинговая рассылка.
- **Из payment-логов убраны суммы**: `payment.webhook` и `payment.confirm` логируют только `payment_id` (суммы остаются в Transaction).
- **Документ для юзера**: [docs/compliance_ru.md](docs/compliance_ru.md) — что закрыто и что юзер делает руками.

### Что юзер обязан сделать руками (см. [docs/compliance_ru.md](docs/compliance_ru.md) и `TODO_NEXT.md`)
1. **🔴 БЛОКЕР: подключить SMTP** — на проде SMTP вообще не настроен, юзеры не получают verification-код. Решение: Unisender / SendPulse / Yandex 360.
2. **Сохранить ключ шифрования бэкапов** в 1Password (или положить в env `BACKUP_ENCRYPTION_KEY`).
3. **Подключить ОФД** в ЛК ЮKassa (Атол Онлайн / Контур.ОФД).
4. **Регистрация в РКН** как оператор ПДн (https://pd.rkn.gov.ru/).
5. **Обновить политику конфиденциальности**: ЮKassa, Anthropic, OpenAI, SMTP-провайдер как обработчики.
6. **Бэкапы вне РФ-сервера** или миграция primary в РФ (Selectel/Yandex Cloud/Reg.ru).

---

## 🆕 Спринт «Refresh single-use + sites public_token + RAG billing» (2026-05-02, `d90e2f1`)

### A. Refresh-token rotation single-use (P1.2 закрыт)
- `User.refresh_jtis` (JSON-список до 10 активных jti, multi-device)
- `create_refresh_token(user_id, email, jti=None)` — теперь принимает jti
- `register_refresh_jti(db, user, jti)` / `revoke_refresh_jti(db, user, jti)` / `revoke_all_refresh_jtis(db, user)` / `is_refresh_jti_active(user, jti)` — в [auth.py](server/auth.py)
- `/auth/login`, `/auth/verify-email`, `/auth/reset-password`, `/auth/oauth/exchange`, `/qr-login/poll` — все вызывают `register_refresh_jti` после issue
- `/auth/refresh` — проверяет jti в наборе. **Reuse-detection** (jti уже использован) = security-incident → `revoke_all_refresh_jtis` + audit-лог critical + 401
- `/auth/logout` — revoke текущий jti server-side
- `/auth/reset-password` — revoke ВСЕ refresh-сессии после смены пароля
- **Grace-period** для legacy токенов без jti / без registered jti — пропускаем проверку (плавная миграция)

### B. Sites public_token (P1.4 закрыт)
- `SiteProject.public_token` (~160 bit `secrets.token_urlsafe(20)`)
- Backfill в `apply_lightweight_migrations`: для уже опубликованных сайтов с `hosted_path != NULL` генерим token и переписываем hosted_path. Старые URL `/sites/hosted/{int_id}/` после деплоя возвращают 404 — юзер должен зайти в `/sites.html` и взять новую ссылку.
- `_ensure_public_token(p)` helper в [routes/sites.py](server/routes/sites.py) — caller commits
- Endpoint `/sites/hosted/{public_token}/{full_path:path}` валидирует token (16-64 alnum/-_), lookup → физическая папка `_sites_host_base/<project.id>/`. Sandbox-обёртка для HTML с null-origin сохранена.
- **Убран `app.mount('/sites/hosted', StaticFiles(...))`** из [main.py](main.py) — он обходил sandbox + token-проверку.

### C. Storage-биллинг для RAG-файлов
- `KnowledgeFile.last_billed_at` + миграция
- Лимиты в [knowledge.py](server/knowledge.py): 50 файлов × 50 МБ × 2 ГБ/юзер (раз теперь платный — было 20×20×500МБ без билинга)
- `_storage_billing_tick` в [scheduler.py](server/scheduler.py) — UNION SUM(StoredAsset) + SUM(KnowledgeFile) за один проход, общий лимит 100 МБ chunks на user
- Просрочка >7д: `KnowledgeFile.enabled=False` (не участвует в RAG, файл цел)
- Просрочка >37д: hard-delete файла + строки + чанков (cascade)
- Audit: `knowledge.disabled` event с reason="no_balance_7d"

---

## 🆕 Спринт «Security audit» (2026-04-30 → 2026-05-02, `cc5afa5`)

Юзер запросил security-review через subagent + ручной чеклист. Найдено + зафикшено 13 пунктов:

### P1 (критичные)
- VK webhook без secret → требуем `vk_secret` обязательным + `compare_digest`
- SSRF в agent `tool_browse_url`: добавлен `_host_resolves_to_private`, scheme whitelist, revalidate редиректов
- SSRF в `presentation_builder._add_remote_image` — то же
- `/knowledge/search` лимит длины `q` (1000 симв) — иначе abuse OpenAI embeddings
- RAG лимиты сначала ужесточены до 20×20×500МБ (потом подняты обратно с биллингом)
- `is_verified` check в `brief-assist`, `/voice/parse`

### P2 (XSS / IDOR / info-leak)
- bleach-санитизация `generated_html` КП в legacy fallback + `edit_section` + `save-html` (защита от self-XSS в WYSIWYG-iframe `allow-scripts allow-same-origin`)
- `/agent/{id}/ws` + `/stream` — owner-check (cookie `access_token` или `?token=`)
- `/auth/login` не возвращает `user_id` для unverified (enumeration)
- `/resend-verify` принимает email + не палит существование (всегда 200 «если есть и не подтверждён — выслан»)
- TG-link: rate-limit (10/10мин на tg_user_id) + email-alert при успешной привязке/перепривязке

### P3 (hardening)
- `/p/{token}` PDF — `relative_to(uploads_root)` (defense-in-depth)
- YooKassa race — validated false-positive (rollback откатывает credit_atomic)

### Что отложено отдельным спринтом
- ⏸ JWT `aud`/`iss` strict verify — нужен grace-period
- ⏸ starlette upgrade — pinned в FastAPI 0.111

---

## Спринт «Презентации v2» (2026-04-28)

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

**Состояние security:** прошли 3 спринта security/compliance — refresh single-use rotation, sites public_token, RAG storage billing, AES-GCM шифрование бэкапов, 54-ФЗ чеки, маркетинговое согласие. См. `TODO_NEXT.md`.

## ❗ Что не работает на проде (нужны действия юзера)
1. **🔴 SMTP не настроен** — юзеры не получают verification-код (нужен Unisender/SendPulse/Yandex 360)
2. **🔴 ОФД не подключён в ЛК ЮKassa** — мои `receipt`-объекты никуда не идут
3. **🔴 Регистрация в РКН** — оператор ПДн (152-ФЗ ст. 22)
4. **🔴 Прод в Нидерландах** — нужна миграция в РФ или хотя бы Yandex backup для ПДн
5. **TG management-бот не запущен** — нужно создать через @BotFather + `setWebhook`

## Что НЕ сделано (но понятно как — отдельные спринты)
1. **OAuth Google/VK** — код готов, ждёт `GOOGLE_CLIENT_ID`/`VK_CLIENT_ID` в env
2. **Прод ЮKassa** — сейчас тестовый shop
3. **Web Push API через VAPID** — sw.js push-handler уже есть, нужны subscription endpoint + ключи
4. **starlette апгрейд** — pinned в FastAPI 0.111, CVE-2024-47874
5. **2FA для админки** — TOTP через pyotp
6. **JWT aud/iss strict verify** — сейчас `verify_aud=False`, нужен grace-period
7. **Cloudflare/CDN+WAF** — сейчас прямой запрос в NL без DDoS-защиты
8. **WhatsApp канал через Wazzup24** — самый востребованный из «🔮 Скоро»

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
