# TODO — задачи в работе и на очереди

_Последнее обновление: 2026-05-10 после спринта v2-редизайна решений_

---

## 🚧 В РАБОТЕ — продолжать в следующей сессии

### v2-редизайн бизнес-решений (10 из 40 готово)

**Концепция v2** (см. CLAUDE.md → раздел «Solutions v2»):
- `Solution.input_schema_json` — массив явных полей `[{name,label,type,required,hint,placeholder,options}]`
- `Solution.orchestra_json` с stage'ами и подстановкой `{field.name}` / `{stage_id.output}`
- Каждое решение использует подходящий микс провайдеров: Perplexity для свежих фактов, Sonnet для анализа, Haiku для черновиков, GPT-4o для полировки.

**Готово (id 1-10):**
1. ✅ Полный SWOT-анализ — 6 полей + Perplexity research → 4 parallel SWOT → TOWS-синтез
2. ✅ 90-дневный план запуска — 6 полей + Perplexity bench + Sonnet
3. ✅ Конкурентный анализ — 4 поля + Perplexity deep + Sonnet
4. ✅ Скрипт холодного звонка — 4 поля + Perplexity объекций + Sonnet + GPT-4o
5. ✅ Симулятор переговоров — 5 полей + Sonnet (роль)
6. ✅ КП — 7 полей + Sonnet draft + GPT-4o polish
7. ✅ Контент-план месяц — 5 полей + Perplexity тренды + Sonnet calendar
8. ✅ Email-цепочка 7 писем — 6 полей + Haiku struct + Sonnet тексты
9. ✅ Заголовки лендинга — 5 полей + Perplexity bench + Sonnet 6-формул
10. ✅ Реклама все форматы — 6 полей + Sonnet под платформы

**Осталось переделать (id 11-40):**
11-13. HR (Вакансия / Мотивация / Онбординг)
14-16. Финансы (Unit-экономика / Финдиагностика / Скрытые расходы)
17-19. Стратегия (Бизнес-процессы / ИИ-автоматизация / новый источник дохода)
20-22. Продажи (ICP / Жалобы / Программа лояльности)
23-25. Стратегия+Research (ICE Score / Тренды / Тренды рынка)
26-30. Sales/HR (Партнёр / Питч инвестору / Идеальный день / Делегирование / Симулятор инвестора)
31-35. Orchestra deep (Аудит лендинга / Юр.договор / Аудит соц / Финаудит / Холодные email)
36-40. Perplexity-фикс-цена (Контрагент / Брифинг / Юр-новости / Аудит цен / Поиск инвесторов)

**Шаблон работы для следующей сессии:**
1. Открой `scripts/seed_v2_solutions.py`, скопируй структуру одного из готовых (например #4 Скрипт холодного звонка) как образец
2. Для каждого нового решения:
   - Открой `Solution.title` + `Solution.description` через `psql` или admin UI чтобы понять что делает
   - Спроектируй input_schema (3-7 полей с типами text/textarea/select/number)
   - Спроектируй orchestra-pipeline: где Perplexity, где Sonnet, где GPT-4o
   - Промпты в стиле «ты — консультант уровня X за Y ₽, выдай Z»
   - Финальный stage обязательно `stream: true` для прогресса в UI
3. Запустить тест: `ssh ... 'cd /root/AI-CHE && /root/AI-CHE/venv/bin/python -m dotenv -f .env run /root/AI-CHE/venv/bin/python scripts/seed_v2_solutions.py'`
4. Открыть UI на проде, кликнуть, проверить что форма выглядит ОК и pipeline даёт качественный результат

**Замечания:**
- Цены НЕ трогаем в скрипте (юзер хочет тестировать качество перед пересчётом)
- Для multi-step interactive (как «Симулятор переговоров») делать одношаговый orchestra — следующие реплики идут через обычный чат
- Для perplexity-фикс-цена пилотов (36-40) — оставить как есть, они уже хорошо собраны (см. `seed_perplexity_solutions.py`)

### После v2-редизайна
- Пересчёт цен: посмотреть real_cost после 5-10 запусков каждого, выставить tier (100/250/500/990 ₽ или индивидуально)
- Документация UX: видео/гифки для каждого пилота
- A/B тест нового UX vs старого

---

## ✅ ЗАКРЫТО (накопительно за все сессии 2026-05-05/10)

### v2-редизайн (2026-05-10, текущая сессия)
- ✅ **Solution.input_schema_json** — новое поле + миграция (LIGHTWEIGHT_MIGRATIONS)
- ✅ **API `/solutions/{id}`** возвращает `input_schema` в `_sol_dict`
- ✅ **Backend orchestra**: `_resolve_placeholder` поддерживает `{field.name}` и `{name}`. `run_orchestra` парсит JSON-input как dict, кладёт в `ctx.fields`
- ✅ **Backend plain steps**: `_execute_step` парсит JSON-input, раскладывает в ctx как отдельные ключи
- ✅ **Frontend `_renderRunInputFields`**: приоритет 1 = v2 input_schema, приоритет 2 = парсер hint, fallback = textarea
- ✅ **Frontend `_collectRunInput`**: для v2 возвращает JSON-stringified dict, валидирует required
- ✅ **`launchSolution`**: если есть input_schema → всегда `_launchSolutionChain` (минуя prompt-editor и orchestra-textarea)
- ✅ **scripts/seed_v2_solutions.py** — заполнил 10 первых решений
- ✅ **Запущено на проде**, миграция применилась автоматически на restart

### Спринт UX/инфра (2026-05-10, текущая сессия)
- ✅ **Логин-модалка**: фикс закрытия при выделении текста (`mousedown+click` double-check на overlay)
- ✅ **API ключи**: `_test_key` теперь использует `_openai_client_kwargs(provider)` с прокси для всех (OpenAI/Anthropic/Google/Grok/Kling)
- ✅ **Email-алерты ключей**: `_last_alerted_broken_ids` hydrate из БД при старте — больше не приходит email на каждый restart
- ✅ **chat.db legacy**: переименован в `.legacy-archived-20260510`, fail-fast на `DATABASE_URL` в `server/db.py`
- ✅ **bcrypt 4.0.1 pin** в requirements.txt (passlib 1.7.4 несовместим с bcrypt 5.x)
- ✅ **Marketplace soft-removed**: feature-flag `MARKETPLACE_ENABLED` (default off), все write-эндпоинты 410, UI-ссылки убраны
- ✅ **Public API → ЛК**: новый таб «🔌 API & интеграции» в cabinetModal через iframe `/api.html?embed=1`
- ✅ **Strict password policy**: 4 класса символов обязательны (lowercase + uppercase + digit + special)
- ✅ **Lowercase admin email** при создании (создание_admin.py + UPDATE на проде)
- ✅ **Бизнес-решения UI**: бейдж категории на карточке + группировка по категориям при «Все» + чистка legacy-префиксов
- ✅ **Карточка решения click**: переход с inline-onclick на data-* + event delegation
- ✅ **launchSolution duplicate**: убран дубль функции, объединён orchestra+plain flow
- ✅ **runModal redesign**: тёмный header, footer прижат, скроллится body
- ✅ **Hint-парсер для legacy решений**: «Укажи: A, B, C» → N полей вместо textarea (для решений где нет input_schema)
- ✅ **Xray VLESS-client на проде**: AI-прокси через `127.0.0.1:10809` → VLESS Reality → `31.169.126.79`. OpenAI/Anthropic/Grok заработали (раньше 403 country)
- ✅ **Admin VidyakovD@gmail.com** создан, пароль `28371988`, в ADMIN_EMAILS

### Инфра
- ✅ **DNS** пропагирован: `aiche.ru` + `www.aiche.ru` → `193.187.92.147`
- ✅ **SSL Let's Encrypt** (expires 2026-08-03), auto-renew через `certbot.timer`
- ✅ **Production nginx-ssl.conf**: HSTS preload (2 года), CSP, redirect HTTP→HTTPS
- ✅ **fail2ban** починен (`backend = systemd`)
- ✅ **PostgreSQL 14** на проде (392 строки в 57 таблиц мигрированы из SQLite). Backup через `pg_dump --format=custom` + AES-256-GCM. Старый `chat.db.before-pg-migration-*` сохранён как safety net
- ✅ **Старый NL-сервер** `194.104.9.219` выключен (`systemctl stop ai-che && disable`)

### SMTP / Email
- ✅ **Yandex 360** для домена `aiche.ru` подтверждён (MX, SPF, DKIM, верификация)
- ✅ **HOSTKEY разблокировал SMTP-порты** 25/465/587 после тикета
- ✅ **Регистрация через email работает**: SMTP=`smtp.yandex.ru:465`, ящик `info@aiche.ru` с app-password
- ✅ **3 бага в email_service.py починены:**
  - Поддержка порта 465 (SMTP_SSL вместо SMTP+STARTTLS) через `_open_smtp()`
  - MAIL FROM = bare email через `parseaddr()` (Yandex отвергал display name)
  - RFC2047-encode кириллицы в From/Subject через `_encode_address_header()` + `email.header.Header`

### Безопасность
- ✅ **Race-fixes:** marketplace anti-pump (partial UNIQUE), webhooks/CRM atomic fail_count, signature IntegrityError → 409
- ✅ **Anti-DoS:** OpenAI timeout=30/60s, rate-limit `/mobile/voice/tts` и `/admin/2fa/`
- ✅ **Google OAuth убран** (продуктовое решение): осталось VK + email + QR. Старые `/auth/oauth/google/start` отдают 410 Gone
- ✅ **A11y:** aria-label на close-кнопках в index.html

### AI / Perplexity
- ✅ **Perplexity ключ обновлён** (`PERPLEXITY_API_KEYS`), модель `sonar-small-chat` → `sonar` (старая снята)
- ✅ **Прокси для Perplexity отключён** через override `PERPLEXITY_HTTPS_PROXY=` (пустая строка). Логика `_ai_proxy()` обновлена: пустая = "не использовать прокси"
- ✅ **5 новых Perplexity-пилотов на фикс-цене:**
  - 🏢 Проверка контрагента — 150 ₽ (real cost ~3.7 ₽, маржа ×40)
  - 👤 Брифинг перед встречей — 100 ₽ (~3.9 ₽, ×26)
  - ⚖️ Юр-новости в нише — 200 ₽ (~4 ₽, ×51)
  - 💰 Аудит цен конкурентов — 150 ₽ (~5 ₽, ×30)
  - 🦾 Поиск инвесторов и партнёров — 300 ₽ (~5.4 ₽, ×56)
- ✅ **5 orchestra-пилотов усилены** через `perplexity_research` stage (тэг `perplexity-deep`):
  - Конкурентный анализ ниши (заменён web_search на perplexity, recency=year)
  - Полный SWOT-анализ (recency=year)
  - Контент-план для соцсетей (recency=month)
  - Юридическая проверка договора (новая стадия «свежая судебная практика», year)
  - Холодная email-рассылка (новая стадия «контекст получателей», month)
- ✅ **`tool_perplexity_research` в agent_runner**: 3 пресета (quick/standard/deep), биллинг real_cost × margin × 5
- ✅ **Лимиты Perplexity ×2:** max_tokens 8000 → 16000, search_context=high везде
- ✅ **Защита от перерасхода:** fix-price ДО вызова, audit-warn при cost > 70% от fix-price, курс 95 ₽/$ как буфер

### UI бизнес-решений (полная реорганизация)
- ✅ Поиск по названию/описанию/тегам
- ✅ 8 chip-фильтров с counters: ✨ Все · 🔬 Ресёрч · 📈 Маркетинг · 💼 Продажи · 📊 Стратегия · ⚖️ Юр · 💰 Финансы · 👥 HR
- ✅ Featured-секция «⭐ Топ новинки» с 3 hot-пилотами
- ✅ Бейджи на карточках: 🔥 ХИТ · 🆕 NEW · 💎 DEEP · 🤖 PRO
- ✅ Цветные рамки по типу (фуксия для Perplexity, оранжевая для orchestra)
- ✅ **40 пилотов распределены по 7 категориям** через `scripts/categorize_solutions.py`
- ✅ Расширена модель `Solution`: +subcategory +tags +is_featured +short_summary
- ✅ Endpoint `/solutions/list` возвращает новые поля + `input_hint`

### Шрифты
- ✅ **Golos Text** (российский от Yandex/КБ Симон-Глюк) — захостили локально в `views/fonts/`
- ✅ Material Symbols тоже локально
- ✅ Единый `views/fonts.css` подключён через `<link>` во всех 14 HTML
- ✅ В `main.py` mount `/fonts` через StaticFiles
- ✅ Никаких внешних CDN (ни Google ни Bunny)

### Тесты
- ✅ **187 passed, 1 flaky (TestApiWebhook), 2 skipped**
- ✅ +5 новых на race-fixes, +2 на marketplace anti-pump, +1 на webhook atomic fail_count

---

## 🔴 БЛОКЕРЫ для коммерческого запуска — ТОЛЬКО ЮЗЕР МОЖЕТ

### 1. Yandex Disk бэкапы (5 минут)

Код готов в `server/scheduler.py:_upload_backup_to_yandex_disk()` — после AES-GCM шифрования файл заливается на Yandex Disk через WebDAV. Активируется когда заданы env-переменные.

**Что нужно от юзера:**
1. Открыть `https://id.yandex.ru/security/app-passwords` под аккаунтом владельца Disk
2. Создать пароль приложения типа **«Файлы (WebDAV)»** → название `aiche-backup`
3. Прислать в `.env`:
   ```
   YANDEX_DISK_USER=vidyakovd88@aiche.ru
   YANDEX_DISK_PASSWORD=<16-символьный пароль>
   YANDEX_DISK_FOLDER=aiche-backups  # опционально, default такой же
   ```
4. `systemctl restart ai-che`. Следующий ночной backup-tick автоматически зальёт.

### 2. ЮKassa: тестовый shop → live

`https://yookassa.ru/my/` → заявка на live-shop. 1-3 дня одобрения. Потом:
```bash
sed -i 's/^YOOKASSA_SHOP_ID=.*/YOOKASSA_SHOP_ID=<новый_live>/' /root/AI-CHE/.env
sed -i 's/^YOOKASSA_SECRET_KEY=.*/YOOKASSA_SECRET_KEY=<новый_live>/' /root/AI-CHE/.env
systemctl restart ai-che
```

### 3. ОФД для 54-ФЗ чеков (ШТРАФ 30k₽/чек если не подключено!)

В ЛК ЮKassa → Настройки → Кассовый чек → подключить **Атол Онлайн** или **Контур.ОФД**.

Добавить в `.env` (если ещё нет):
```
YOOKASSA_VAT_CODE=1            # без НДС (УСН) или 4 (НДС 20%)
YOOKASSA_TAX_SYSTEM_CODE=2     # УСН доходы
```

### 4. РКН — регистрация оператора ПДн (ШТРАФ 60-300k₽!)

`https://pd.rkn.gov.ru/` → подать заявление. **152-ФЗ ст. 22**. Сервер в РФ — заявка проще. **30 дней рассмотрения** — лучше начать раньше.

---

## 🟡 Опционально (не блокеры)

| # | Что | Действие |
|---|---|---|
| 1 | **Старый NL-сервер физически удалить** | Через 30 дней: панель Clouvider → Delete VM (`194.104.9.219`) |
| 2 | **Gemini API ключ** обновить если нужен Imagen 4 / Veo 3 / Gemini Flash | `https://aistudio.google.com/apikey` → новый ключ → в `.env` `GOOGLE_API_KEYS` |
| 3 | **Wazzup24 (WhatsApp)** | Договор + API key → в карточке бота |
| 4 | **TG management-бот** | @BotFather → `TG_MGMT_BOT_TOKEN` + `TG_MGMT_BOT_USERNAME` в `.env` |
| 5 | **Yandex Object Storage** | Альтернатива Yandex Disk для бэкапов через S3-совместимое API. Код тоже готов в `server/scheduler.py:_upload_backup_to_yc_s3()` |

---

## 🟢 Что Claude может делать в новых сессиях

### Высокий приоритет (улучшения готовых модулей)
- **Тестирование 5 усиленных orchestra-пилотов** на реальных кейсах через UI и замер cost
- **A/B сравнение** старой и новой версии «Конкурентного анализа» — какая версия даёт лучше output
- **A11y на остальных страницах** (proposals/sites/chatbots/agents/admin/api/marketplace/...) — я сделал только index.html
- **starlette upgrade** (CVE-2024-47874 / CVE-2025-54121, нужен staging для проверки breaking changes)
- **Дубликат webhooks.py + crm.py dispatcher** (~150 строк копипасты) → вынести в общий модуль

### Новые продуктовые фичи
- **Видео-приветствие в КП** через Veo (персонализация)
- **Calendar integration** — авто-создание встречи при ответе клиента
- **Marketplace withdrawal flow** — авторы выводят 70% на карту через ЮKassa
- **Bulk-генерация КП из CSV** — для агентств
- **A/B-тест промптов в orchestra** — собирать training-data

### Технический долг
- **Splitting `chatbot_engine.py`** (3122 строки) и `views/icons.js` (~3700 строк)
- **Tailwind CDN → build-step** (~400ms экономия page load)
- **Удалить unused Inter/Manrope woff2** в `views/fonts/` (~250 KB лишних — теперь используется Golos Text)
- **JWT aud/iss strict verify** (сейчас `verify_aud=False`, нужен 30-дневный grace period)
- **Webhook flaky test** `TestApiWebhook::test_create_returns_secret_once` — починить через uuid в URL

### Когда Yandex Disk credentials придут от юзера
- Прописать `YANDEX_DISK_USER/PASSWORD/FOLDER` в `.env`
- Форсирнуть `_db_backup_tick()` через Python interactive (или подождать 24 часа)
- Проверить что в `disk:/aiche-backups/` появился `chat.db.YYYY-MM-DD.enc`

---

## 📋 Заметки для Claude в новых сессиях

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
# 186 passed expected, 1 flaky (TestApiWebhook), 2 skipped
```

### Деплой
```bash
git push origin claude/<branch>:main
HOME="C:\\Users\\Денис" ssh -i "C:\\Users\\Денис\\.ssh\\id_ed25519" \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  root@193.187.92.147 \
  "cd /root/AI-CHE && git pull origin main && systemctl restart ai-che && sleep 6 && systemctl is-active ai-che"
```

### Сид-скрипты (запускать на проде вручную после изменений)
```bash
# 5 Perplexity-пилотов (бизнес-решения)
ssh ... "cd /root/AI-CHE && /root/AI-CHE/venv/bin/python scripts/seed_perplexity_solutions.py [--update]"

# Усиление 5 orchestra-пилотов через perplexity_research stage
ssh ... "cd /root/AI-CHE && /root/AI-CHE/venv/bin/python scripts/upgrade_orchestra_perplexity.py [--force]"

# Распределить новые Solution по subcategory (если добавляем новые)
ssh ... "cd /root/AI-CHE && /root/AI-CHE/venv/bin/python scripts/categorize_solutions.py"

# Базовые orchestra-пилоты (если они когда-то слетят)
ssh ... "cd /root/AI-CHE && /root/AI-CHE/venv/bin/python scripts/seed_orchestra_solutions.py"
```

### Полезные команды
- **Audit-log дамп**: `curl https://aiche.ru/admin/actions.txt?since_hours=72&limit=2000 -H "Authorization: Bearer <token>"`
- **JS syntax check**:
  ```bash
  node -e "const fs=require('fs');for(const f of ['views/index.html','views/proposals.html','views/sites.html','views/chatbots.html','views/presentations.html','views/agents.html','views/admin.html','views/marketplace.html','views/api.html','views/icons.js']){const src=fs.readFileSync(f,'utf8');if(f.endsWith('.js')){try{new Function(src);console.log(f+': OK');}catch(e){console.log(f+': '+e.message);}}else{const m=src.match(/<script>([\s\S]*?)<\/script>/g)||[];let any=true;for(let i=0;i<m.length;i++){try{new Function(m[i].replace(/^<script>|<\/script>\$/g,''));}catch(e){console.log(f+' #'+i+': '+e.message);any=false;}}if(any)console.log(f+': OK');}}"
  ```
- **Реальный тест Perplexity-пилотов из admin**: запустить любой через UI и в `solution_runs` в БД проверить `attachments_json.stages.research.actual_cost_kop`

### Соглашения (КРИТИЧНО для новых сессий)
- **Все цены в БД** `pricing_config` — менять через `POST /admin/pricing` без редеплоя
- **Свои API-ключи юзера** — вкладка «Свои API» в кабинете
- **Прайс-листы для КП** — вкладка «📋 Прайсы» в `/proposals.html`
- **Native dialogs запрещены** — везде `aiAlert/aiConfirm/aiPrompt/aiToast/aiAlertError`
- **Margin ×7 для презентаций** — внутри `presentation_builder`, в UI не показывается
- **Margin ×5 для orchestra-стадий** — `ai.improve_margin_pct=500`
- **JSON-first генерация** для КП и orchestra-стадий
- **Бэкапы шифруются AES-GCM** — ключ в `.backup_encryption_key` или env. Restore: `scripts/restore_backup.py` или `scripts/restore_from_yandex_disk.py`
- **Refresh-token single-use** — после `register_refresh_jti` jti должен быть в наборе. Race-safe `_atomic_jtis_update`
- **Опубликованные сайты** — URL `/sites/hosted/{public_token}/`, не int_id
- **Public API auth** — `Bearer ai_che_<prefix>_<secret>`, scope-проверка через `authenticate_token(required_scope=...)` + is_verified
- **Web Push** — VAPID-ключи в `.env`: `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY_FILE=/root/AI-CHE/.vapid_private.pem`
- **WhatsApp** — secret webhook = `tg_webhook_secret(wazzup_api_key)`
- **AI-прокси** — `AI_HTTPS_PROXY` общий fallback, или `<PROVIDER>_HTTPS_PROXY` специфичный (пустая строка = override "не использовать прокси")
- **Idempotency** — DB-table `IdempotencyRecord` с UNIQUE(user_id, key), TTL 5 мин, cleanup в scheduler
- **Multi-worker race** — `requests_count` → atomic UPDATE, refresh_jtis → with_for_update + re-fetch
- **Шрифт Golos Text** — единый `views/fonts.css`, никаких CDN
- **Perplexity модели** — только `sonar` / `sonar-pro` / `sonar-reasoning-pro`. `sonar-small-chat` снят, не использовать
- **Perplexity билинг** — fix-price для пилотов, real × margin для tool в agent_runner. Курс 95 ₽/$
- **Perplexity прокси** — `PERPLEXITY_HTTPS_PROXY=` (пустая) = direct. Перплексити доступен напрямую с РФ
- **OAuth** — только VK + email + QR. Google убран из whitelist'а
- **SMTP** — Yandex 360 `smtp.yandex.ru:465`, ящик `info@aiche.ru`. Кириллица в From/Subject обязательно через RFC2047 encode
- **БД на проде** — PostgreSQL (`postgresql://aiche:...@localhost:5432/aiche`). Для dev — SQLite
