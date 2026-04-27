# HANDOVER — для нового AI-ассистента

Если ты впервые в этом проекте — после `CLAUDE.md` прочитай этот файл. Тут **состояние на 2026-04-27 после большого ребилда тарифов и фич**.

## Спринт «Bot pricing rework + Price-list + MAX fix» (2026-04-27)

### Bot pricing — пересмотр тарифов (`pricing.py` + `624ed9a`, `823ef92`)

Юзер пересмотрел тарификацию. Сейчас:

| Действие | Цена | Где списывается |
|---|---|---|
| **Создание с нуля Canvas** | бесплатно | `POST /chatbots` — без `deduct` |
| **Из шаблона** | бесплатно | `POST /chatbots/from-template/{slug}` |
| **AI-конструктор** | **≥ 1000 ₽** | `bot.ai_create_min`. cost = max(min, real_tokens × margin) |
| **AI-доработка / правки** | **real × 5, без фикс** | `bot.ai_improve_min=0`, `ai.improve_margin_pct=500` |
| **Реальные диалоги бота** | **real × 3** | `ai.reply_margin_pct=300` |
| **Edit-block в сайте** | **real × 5** | переписан с фикс 5 ₽ на real × 5 |
| **Storage файлов** | 50 ₽/мес за 100 МБ | `storage.per_100mb_month`, дневное списание в scheduler |

**Все цены в БД** (`pricing_config`), меняются через `POST /admin/pricing` без редеплоя. См. `server/pricing.py` DEFAULTS.

### Свои API-ключи юзера (`cdd735d`, `624ed9a`)

Юзер может в кабинете → вкладка «Свои API» подключить свой OpenAI/Claude/Gemini/Grok ключ:
- Хранится `EncryptedString` через HKDF от JWT_SECRET
- При AI-вызове бота → `_load_user_api_keys(user_id)` загружает в ctx
- AI-ноды → `_user_key_for_model(ctx, model_id)` → если есть, `_call_ai_with_fallback(...)` использует его
- При ошибке user-key (401/quota/«временно недоступен») — fallback на наш ключ
- Скидка: `ai.user_key_discount_pct=20` — юзер платит **20%** от обычной цены (за инфраструктуру)

### Прайс-лист бота с semantic vector search (`868840f`, `40e4e12`)

Новая модель `BotPriceItem` (bot_id, name, price_kop, price_text, category, description, sort_order, **embedding_json** — TEXT с JSON-сериализованным 1536-dim вектором).

UX:
- Кнопка `₽` на карточке бота → модалка «Прайс-лист бота»
- Inline-таблица + импорт CSV (`/chatbots/{id}/price/import-csv`) с auto-detect разделителя и русскими колонками
- Кнопка «Векторы» — пересчитать embeddings (если импорт был без OpenAI ключа)

Технически (важное!):
- Embeddings через `text-embedding-3-small` ($0.02/1M токенов = ~0.0002 ₽ за 200 позиций batch'ом)
- При **POST/PATCH** — single embedding sync (`update_price_item_embedding`)
- При **CSV import** — `batch_update_price_embeddings` (1 API-call вместо N)
- При **вопросе клиента**:
  1. `_price_keyword_in_text()` — детектит триггер («сколько», «цена», «прайс», «руб», «₽», ...) — БЕЗ триггера прайс не подключается → не удорожаем обычные диалоги
  2. `_cached_query_embedding()` — embedding запроса с TTL 10 мин
  3. Cosine similarity ко всем позициям → top-15 при threshold 0.30
  4. Inject компактным форматом в system_prompt
  5. Fallback на substring search если OpenAI недоступен

### MAX полный fix (`bb18a4f`, `7bfa9cc`, `d88077b`, `d81a0b5`)

Долгий debug — каскад из 3 багов:

1. **MAX API deprecation** — `?access_token=` больше не работает, требует `Authorization` header
2. **MAX ожидает Authorization БЕЗ префикса `Bearer`** — нестандартное поведение, их error message обманчивая. Live-тест: `Bearer xxx` → 401, `xxx` → 200 OK
3. **JWT_SECRET race** — `auth.py` импортировался раньше `ai.py` (где был `load_dotenv()`), брался ключ из `server/.jwt_secret` файла; при следующем рестарте — из `.env`. Шифрованные `max_token` не расшифровывались.

Фиксы:
- `_max_headers(token)` возвращает `{"Authorization": token}` (без Bearer)
- `auth.py` сам делает `load_dotenv()` в начале файла
- `secrets_crypto._all_fernets()` пробует и env-ключ, и файловый
- `auth.decode_token` пробует все доступные ключи (env + file + LEGACY) — чтобы старые сессии не отвалились при смене источника

### UI / UX правки (`5d6c76b`, `7d314be`, `929cd00`, `f49c18b`, `3d5f6cf`)

- **Брендовые иконки каналов и AI** — canonical SVG из simple-icons.org (CC0): Telegram, VK, OpenAI, Claude, Gemini, Grok, Perplexity. + кастомные для MAX, Avito, Imagen, Veo, Kling. `views/icons.js` + `getModelBrandIcon(model_id)` helper.
- **Карточка бота** — кнопки переехали в `flex-wrap-row` под названием, кнопка ❌ Удалить рядом с именем, имя через `truncate` не сжимается
- **Кнопка «🔄 Обновить»** в карточке active-бота (`redeployBot()`) — применяет свежие настройки без Pause/Start
- **Узкое превью** в /sites.html — `min-height:70vh` на `.preview-wrap` + iframe (фикс цепочки flex)
- **Mini-browser** в превью сайта — toolbar `← → ↻ 🏠` + адресная строка. Управление через postMessage в injected runtime (sandbox без allow-same-origin не пускает читать iframe.history)
- **Кастомный scrollbar** — тонкий 6px, оранжевый `rgba(255,140,66,0.25)`
- **Help-блоки `<details>`** «Где взять токен?» — пошаговые инструкции для TG/VK/Avito/MAX/виджет
- **Roadmap «🔮 Скоро»** — 8 РФ-каналов (WhatsApp/OK/Viber/JivoSite/Битрикс/AmoCRM/Email/SMS) с голосованием через `POST /user/feature-vote` → audit_log

### Security audit (после большого спринта) (`ddc0040`)

Прошёл повторный аудит. Закрыто 4 P0 + 7 P1:
- **CSRF bypass через `Authorization: Bearer ` (пустой токен)** — теперь `len > 10`
- **CSV-export `?_h=<token>` в URL** — переписан на blob через fetch с Authorization
- **Sites injected JS** — `${editMode}` без JSON.stringify → потенциал XSS если переменная станет string
- **Avito webhook без auth** — добавлен `?secret=` через `tg_webhook_secret(avito_client_id)`
- **Storage billing race** — `last_billed_at=now` при upload + `created_at < cutoff` в архивации
- **`update_price` без верхней границы** — лимит 100M коп
- **target=_blank без rel=noopener** — 7 мест автопатчем
- **iframe sandbox `allow-popups`** — убран
- **postMessage `'*'` → `window.__parentOrigin`**

---

## Спринт «Sites bugs + Bot constructor + Storage» (2026-04-26 второй заход)

### Sites — баги + улучшения (afc3425)
- Узкое превью: дефолт модалки 90vw → 96vw, padding tab-pane 4 → 2.
  Если на широком экране (>1280px) сохранена ширина <70% экрана — сбрасываем
  localStorage (юзер мог случайно ужать).
- **Auto-save**: добавлен флаг `_unsavedChanges` + debounce 1.5с.
  Любое изменение в textarea / postMessage от iframe → автосейв.
  Перед closeDetail / downloadSite / downloadZip — синхронный saveCode.
  beforeunload показывает alert если есть unsaved.
  После замены картинки — мгновенный saveCode.
- **GPT enhance переписан**: теперь промпт на 1500-3500 слов с разделами
  (бизнес-контекст, тон/стиль, цветовая палитра HEX с ролями, 10 секций
  обязательной структуры, UX-фишки, технические требования, картинки).
  Premium tier (Opus) использует gpt-4o (не mini) для enhance.

### Bot constructor (dcf7d5c)
- **MAX webhook security (P0)**: `?secret=...` URL-параметр через `tg_webhook_secret`,
  compare_digest проверка, 401 без secret. Раньше любой мог POST'ить и сжигать баланс.
- **MAX idempotency**: in-memory dedup по message_id, TTL 1 час, авточистка.
- **Двухэтапный pipeline GPT → Claude в workflow_builder**:
  GPT-4o-mini сначала структурирует сырое описание клиента в детальное ТЗ
  (платформа, цель, триггер, сценарий, поля формы, ветки, фичи).
  Claude по такому ТЗ строит граф → лучше качество, дешевле итог.
- **Library WORKFLOW_BLOCKS**: готовые snippet'ы (lead_capture, booking,
  faq_rag, sales_warmup, quiz_funnel, broadcast). `_select_relevant_blocks`
  по ключевым словам передаёт релевантные в enhance prompt — Claude
  собирает из проверенных паттернов вместо генерации с нуля.
- **Френдли entry-point** в `views/chatbots.html`: empty-state hero с градиентом,
  3 пути с цветными бейджами (Старт/AI/Pro), quick-start чипы (запись, заявки,
  FAQ, продажи), подсказка про @BotFather.
- MAX bug: пустой ответ AI больше не отправляется.

### Storage assets + self-hosting export + MAX P1 (124f15b)
- **Self-hosting export**: GET /chatbots/{id}/export?format=zip
  Скачивает ZIP с bot.json + README.md. Токены НЕ включены (защита от утечки).
  README объясняет как импортировать в другую инсталляцию или поднять движок.
  Audit-log: bot.export.
- **Storage assets** (лидмагниты PDF/картинки/видео):
  - Новая модель StoredAsset (user_id, bot_id, path, public_token, size_bytes)
  - GET /assets, /assets/usage, POST /assets/upload, DELETE /assets/{id}
  - GET /assets/public/{token} — публичная скачка для бота
  - pricing-key `storage.per_100mb_month` = 50 ₽/мес за 100 МБ
  - scheduler: storage_billing_loop списывает дневную ставку (rate/30)
    с округлением вверх до 100 МБ. Если баланса нет — пропускаем.
- **MAX P1**:
  - send_max при 401/403 → _disable_max_bot_for_token() помечает все
    боты с этим токеном как max_webhook_set=False + audit-log.
  - setup_max_webhook валидирует HTTPS перед регистрацией.
  - Не логируем full exception (был утечной точкой) — только тип.

---

## Спринт «Security audit + harden» (2026-04-26)

Прошёл полный аудит. Закрыты ВСЕ топ-7 P0 + 13 P1 + 3 P2. 76/76 тестов зелёные.

**P0 (критично):**
- `code_python` sandbox — whitelist AST-узлов вместо blacklist + wallclock timeout (signal.SIGALRM) + лимиты на длину/число узлов/литералы. Запрет ClassDef, Lambda, While, `**` оператор. По умолчанию выключен (`ENABLE_PYTHON_SANDBOX=true`).
- `http_request` нода — двойной DNS-резолв (все A-записи), расширенный CIDR блок-лист (169.254/16, 100.64/10, fd00::/8, ::ffff:0:0/96 и др.), no-redirect + 1MB лимит ответа + revalidate Location при 3xx.
- YooKassa webhook — **HARD-FAIL** если `YOOKASSA_SECRET_KEY` не задан (раньше silent accept). Override для тестов: `ALLOW_UNVERIFIED_WEBHOOK=true` или `DEV_MODE=true`.
- OAuth Google/VK — `state`-параметр через новую таблицу `oauth_states` (TTL 10 мин). Защита от login CSRF.
- Iframe preview сайта — `sandbox="allow-scripts allow-modals allow-popups allow-forms"` (без allow-same-origin → AI-HTML не имеет доступа к нашему cookie/localStorage). `addEventListener('message')` теперь проверяет `e.source === frame.contentWindow` + allowlist `e.data.type`.
- Sites — refund при failure фоновой генерации через идемпотентный `_refund_site_generation` (gen_status="refunded" гарантирует не-дубль).
- ai.py — `_sanitize_error()` + автоматический `_SecretFilter` на logger. Маскирует `sk-*`, `Bearer *`, `Authorization=*`, `AIza*`, прокси-URL с креденшалами, `?key=...`.

**P1 (высокий):**
- `_use_verify_token` — атомарный `UPDATE WHERE used=False` (раньше SELECT-then-UPDATE с race).
- `deduct_atomic` — exponential backoff с jitter (8 попыток, 5ms→500ms).
- `_get_api_keys` — через `db_session()` контекст-менеджер (rollback safety).
- Veo polling — общий wallclock-cap `VEO_POLL_TIMEOUT_SEC` (по умолчанию 360с).
- `/message` — поддержка `Idempotency-Key` header (cache 5 мин). Двойной клик / network retry → один и тот же ответ без двойного списания.
- Scheduler — SQL pre-filter `workflow_json LIKE '%trigger_schedule%'` (раньше грузил все боты с полным workflow JSON каждые 30с).
- CORS — fail-fast если `DEV_MODE=true` И `APP_ENV=production` одновременно.
- `BuyTokenRequest.return_url` — валидация против `APP_URL.host` (защита от open-redirect фишинга).
- `_notify_admin` — текст ошибки санитизируется перед отправкой в Telegram.
- systemd unit — hardening: `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`, `ProtectHome`, `RestrictAddressFamilies`, `SystemCallFilter`, `MemoryDenyWriteExecute`. Restart=on-failure + StartLimitBurst.
- CI workflow `.github/workflows/ci.yml` — pytest + ruff + pip-audit на каждый PR.
- Тесты — добавлены 4 теста на YooKassa HMAC: missing/malformed/wrong sig/hard-fail без secret.
- Все textarea во views получают `maxlength=50000` через автопатч в `icons.js` + MutationObserver.
- Sites polling — AbortController + exponential backoff (4→8→16→30с) + abort на `beforeunload` + abort при смене проекта.

**P2:**
- `/admin/users` — offset/limit/search pagination (раньше hardcoded 200).
- Audit retention 3 эшелона: обычный info — 30 дней, `auth.*`/`payment.*`/`record.*` info — 365 дней (forensic), warn/error — 90 дней.
- DB backup — `PRAGMA integrity_check` после каждого. Corrupted backup удаляется + ERROR в логе.
- Новая модель `OAuthState` (создаётся через `Base.metadata.create_all` на старте).

**Defer (отложено отдельным спринтом):**
- JWT в `localStorage` → httpOnly cookie + CSRF-токен. Большая миграция backend+frontend, нужно отдельным заходом с тестами.



## Кто юзер и что делаем
- Юзер — Денис, владелец `aiche.ru`. **B2B AI-платформа** для предпринимателей.
- Стек: FastAPI + SQLite + JS SPA. Прод в Нидерландах (Clouvider).
- Юзер — не программист. Общаемся по-русски, понятно, без терминов где можно. Делаешь — катаешь сразу на прод (`git push origin main` → `ssh ... git pull && systemctl restart ai-che`).

## Текущая «фаза» проекта
Платформа умеет:
1. **Чат с AI** (GPT-4o, Claude Sonnet/Opus, Perplexity, Grok, Imagen, Veo) — баланс в копейках, списание per-token + per-request
2. **Бизнес-решения** — 30 готовых промптов с фикс-ценой 50/100 ₽, выдача PDF
3. **Чат-боты для бизнеса** в TG/MAX/VK/Avito/widget — с **6 готовыми шаблонами** (запись/лиды/FAQ/продажи/квиз/контент)
4. **Конструктор сайтов** — фоновая генерация, два tier (Sonnet 1500₽ / Opus 1990₽)
5. **КП и презентации** — 50/100 ₽ с PDF
6. **AI-агенты** с очередью

Финансы: Welcome 50₽, реферал 10%. Платежи через ЮKassa (тестовый shop). Деплой ручной.

## Что было сделано в последних сессиях

### Спринт 1: «Аудит безопасности и багов»
Вышел отчёт с 1 CRIT, 4 HIGH, 7 MEDIUM. Все CRIT/HIGH закрыты:
- Welcome / referral бонусы — atomic gates через новые поля `User.welcome_bonus_claimed_at`, `User.referral_signup_bonus_paid_at` + UNIQUE-индекс на `transactions.yookassa_payment_id`
- Widget XSS — `bot.name` через `json.dumps` + `textContent` вместо innerHTML
- Worker_lock fail-CLOSED (был fail-OPEN — мог дублировать задачи)
- TG webhook требует `X-Telegram-Bot-Api-Secret-Token`
- HKDF вместо sha256 для Fernet-ключа в `secrets_crypto`
- SVG-аплоады — фильтр `<script>`/`onload=`
- Виджет injection в HTML — `rfind("</body>")` + fallback на `</html>`
- Виджет WebSocket — Origin-whitelist через `ChatBot.widget_allowed_origins`

### Спринт 2: «Конструктор бизнес-ботов»
- Новая таблица **`bot_records`** (lead/booking/order/quiz/ticket/subscriber)
- Новая нода **`save_record`** в workflow
- 5 новых нод для богатого UX: `request_contact`, `request_location`, `output_photo`, `edit_message`, `chat_action_typing`
- `output_max_buttons` (mirror `output_tg_buttons` для MAX)
- Авто-`setMyCommands` при деплое TG-бота (`/start /help /menu`)
- Conversation memory переехала из in-memory dict в **`bot_conversation_turns`** (переживает рестарт, multi-worker safe)

### Спринт 3: «6 шаблонов ботов + UX-обвязка»
- `server/bot_templates.py` — 6 шаблонов (lead_capture, sales_warmup, faq_support, booking, quiz_funnel, content_broadcast)
- Каждый шаблон имеет `customizable` поля (название компании, услуги, и т.д.) → подставляется в workflow через `{{key}}`
- Endpoint `POST /chatbots/from-template/{slug}` + публичный `GET /chatbots/templates`
- На `/chatbots.html`: трёхпутный chooser «Шаблон / AI-конструктор / С нуля»
- В карточке бота 4 новых кнопки: 👁 Превью · 📊 Аналитика · 📋 Записи · ✨ Доработать через AI
  - **Превью** — мини-чат с ботом в песочнице (не списывает, не сохраняет records)
  - **Аналитика** — диалоги/входящие/конверсия + SVG bar chart + топ-вопросы
  - **Записи** — таблица заявок с фильтрами + CSV-экспорт
  - **AI-доработка** — юзер пишет «сделай тон строже» → workflow_builder правит граф

### Спринт 4: «Imagen 4 + Veo 3 через прокси»
- Google AI Studio блочит наш ASN — добавлен `GOOGLE_HTTPS_PROXY` env
- Imagen 4 (fast/std/ultra) с правильными именами моделей. `negativePrompt` deprecated → пихаем в основной prompt
- Veo 2/3.0/3.0-fast/3.1 с асинхронным polling (predictLongRunning)
- Per-model capabilities: audio только Veo3, i2v только Veo3.x, neg только Veo2 — иначе 400
- На `/index.html`: Nano как отдельный таб, Veo с селектом «Качество модели» + «Картинка→видео» режим
- Видео в чате как `<video controls>` + кнопка «Скачать»
- Авто-детект `.mp4`/`.webm` URL → addVideoMsg
- Click-outside сворачивает панель Veo/Nano параметров
- **Auto-refund** в `/message` если ai-видео/картинка вернулись с ошибкой

### Спринт 5: «Сайты — фоновая генерация + 2 tier»
- Synchronous → **async** через `asyncio.create_task` + polling-эндпоинт `/generation-status`
- Pre-process ТЗ через **GPT-4o-mini** (расширяет до 800-1500 слов с цветами/тоном/секциями)
- Anthropic SDK timeout 60→**600 сек**
- Auto-continue до 3 (Sonnet) или **6** (Opus) turns с промежуточным сохранением
- Frontend polling 4 сек × до 10 мин с живым прогрессом «Готово 47 KB, дописываю (4/6)…»
- Два tier: **🟢 Стандарт (Sonnet) 1500₽** или **🟣 Премиум (Opus) 1990₽**
- Радио-кнопки с описанием, динамическая цена на «Создать сайт»

### Спринт 6: «Audit log для AI-ассистента» (текущий)
- Таблица `action_logs` + `server.audit_log.log_action()` helper
- Логирование в register/verify/payment/AI-вызовах/sites/bot CRUD
- Эндпоинты `/admin/actions`, `/admin/actions.txt`, `/admin/actions.jsonl`
- Cleanup в scheduler (info > 30 дней, error > 90)
- Этот HANDOVER.md и обновлённый CLAUDE.md

## Что НЕ сделано (но понятно как)
1. **OAuth Google/VK** — код готов, ждёт `GOOGLE_CLIENT_ID`/`VK_CLIENT_ID` в env
2. **Прод ЮKassa** — сейчас тестовый shop, нужен live shop_id+secret
3. **Bot constructor live в TG/MAX** — endpoint и шаблон workflow есть (`bot_constructor` нода), но не задеплоено как самостоятельный бот в `@aiche_bot_builder`
4. **OAuth-привязка клиента-салона** к платформе — сейчас бот-конструктор создаёт ботов под аккаунтом своего владельца, не под клиента
5. **Mobile responsiveness** — Tailwind есть, но реальную проверку на 375px не делали
6. **DRY рефактор `server/ai.py`** — частично сделан (`try_with_keys`, `_openai_compatible_response`), но 14 *_response функций ещё дублируются

## Что обычно ломается
1. **Google AI Studio 429 «prepayment depleted»** — у юзера закончились кредиты в Google Cloud билле. Решение: пополнить на ai.studio. Auto-refund в `/message` уже работает — деньги клиента не списываются.
2. **Veo 3.0 fast 503 «Deadline expired»** — квота. Backend делает fallback на 3.1 → 3.0 → 2.0 автоматически.
3. **Anthropic 60 сек timeout** — было до спринта 5, сейчас 600.
4. **Backup в git** — `backups/` теперь в `.gitignore`, но если кто-то добавит файлы — `git add -A` зацепит. Использовать `git add -A ':!backups/'`.

## Как разобраться в новой задаче от юзера
1. **Прочитай эти файлы.** Серьёзно — здесь ВСЁ актуально.
2. **Запроси audit log за нужный период:**
   ```
   curl https://aiche.ru/admin/actions.txt?since_hours=72&only_errors=true \
        -H "Authorization: Bearer <admin token>"
   ```
   Юзер тебе пришлёт.
3. **`git log --oneline -20`** — что трогали недавно.
4. **`grep -rn` или Grep tool** — где живёт фича.
5. Если фича большая — сначала **отвечай планом**, потом делай. Юзер любит «расскажи как сделаешь, потом катай».
6. После деплоя — **подтверди работу** живым curl или скриншотом из preview.

## Стиль работы юзера
- «Делай по порядку», «делай на свое усмотрение» = можешь катать сразу на прод без согласования каждого шага
- «Точечно поправим» = не бойся ошибиться, лучше быстрее
- Любит чёткие списки с эмодзи 🟢🟣 (но не везде — для статусов и tier'ов)
- Не любит вопросы из серии «А что предпочитаете?» — лучше прими решение сам и обоснуй коротко
- При проблеме — пришлёт скриншот, по нему ориентируйся

## Канал коммуникации с юзером в этом чате
- Все правки — сразу `git push origin claude/sleepy-johnson-115434:main` → `ssh ... git pull && systemctl restart ai-che`
- Отчёт после каждого блока — короткий список что сделано + ссылки на файлы (file:line)
- Если что-то новое — сразу обновляй `CLAUDE.md` и (для крупного спринта) `HANDOVER.md`

## Полезные команды
```bash
# Запуск local dev
DEV_MODE=true JWT_SECRET=dev-secret python -m uvicorn main:app --reload --port 8001

# Тесты
python -m pytest tests/

# SSH прод
ssh -i 'C:\Users\Денис\.ssh\id_ed25519' root@194.104.9.219

# Logs прод
journalctl -u ai-che -n 100 --no-pager

# Audit log dump (admin)
curl -H "Authorization: Bearer <admin token>" \
     "https://aiche.ru/admin/actions.txt?since_hours=72&limit=2000"

# Apply migrations + sanity import (local)
DEV_MODE=true python -c "import sys;sys.path.insert(0,'.');from server.db import Base,engine,apply_lightweight_migrations;from server import models;Base.metadata.create_all(bind=engine);apply_lightweight_migrations();import main;print('routes:',len(main.app.routes))"
```
