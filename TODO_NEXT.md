# TODO_NEXT — задачи на очереди, по модулям

> Что делать в новых сессиях. **Структура по модулям** — открой нужный `.md` в `docs/modules/` для контекста перед работой.

_Последнее обновление: 2026-05-13._

> ℹ️ **Pre-launch stage:** база на проде пустая, только админ. Перфоманс/N+1/A-B-тесты/аналитика usage преждевременны. Data-retention включать пока некого чистить. Приоритет — качество фич, UX, security к моменту запуска, новый модуль «Креаторы».

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

### 2. ЮKassa: тестовый shop → live
- `https://yookassa.ru/my/` → заявка на live-shop
- 1-3 дня одобрения
- После одобрения:
  ```bash
  sed -i 's/^YOOKASSA_SHOP_ID=.*/YOOKASSA_SHOP_ID=<live>/' /root/AI-CHE/.env
  sed -i 's/^YOOKASSA_SECRET_KEY=.*/YOOKASSA_SECRET_KEY=<live>/' /root/AI-CHE/.env
  systemctl restart ai-che
  ```

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

### [07-proposals](docs/modules/07-proposals.md)
1. **`PROPOSAL_COST_KOP=5000` хардкод** ([server/routes/proposals.py:29](server/routes/proposals.py:29)) → читать `pricing.get("proposal.create", 5000)`
2. **Email-валидация формальная** ([server/routes/proposals.py:920](server/routes/proposals.py:920)) — `"@" in to` → использовать `EMAIL_RE` из `server/security.py`
3. **Кириллица в filename PDF** — `Content-Disposition` без `filename*=UTF-8''…`
4. **Public proposal page как inline f-string** ([main.py:759-907](main.py:759)) — 150 строк → вынести в `views/proposal_public.html` + Jinja
5. **`open_count` счётчик** — фиксировать каждый GET, не только первое открытие
6. **`max_tokens=6000` хардкод** ([server/proposal_builder.py:1155](server/proposal_builder.py:1155)) → `6000 + len(price_text)//4`
7. **Snapshot-version race** ([server/routes/proposals.py:496](server/routes/proposals.py:496)) — `id NOT IN (top-10)` вместо offset
8. **WYSIWYG iframe `allow-scripts`** — батч-санитайз старых записей (скрипт `sanitize_legacy_proposal_html.py` создан, запустить если ещё нет на проде)

### [09-sites](docs/modules/09-sites.md)
1. **`_strip_markdown_code_fence` дублируется** в `/iterate` и `/edit-block` → вызывать функцию
2. **Closure-bug в lambda** ([server/routes/sites.py:619, :666](server/routes/sites.py)) — `prompt`/`model_id` захватываются по ссылке
3. **`/sites/code` мёртвый endpoint** — `site_decode_code` не используется → удалить
4. **Phase `generating_code` после reload** — `openProject` уходит в showDone с пустым codeEditor → перезапустить polling при `gen_status='running'`
5. **`copyCode()` ломается без `event`** — глобальный `event.target` только Chrome
6. **Нет ETA в loader-е генерации** — добавить «обычно 2-4 минуты»
7. **a11y на radio quality-option** — `role="radiogroup"` + `aria-describedby`
8. **Sequential `project.id` в физпути** — переехать на `public_token`

### [13-public-api](docs/modules/13-public-api.md)
- **Дубликат webhooks.py + crm.py dispatcher** (~150 строк копипасты) → уже частично вынесено в `_outbound.py`, доделать
- **Webhook flaky test** `TestApiWebhook::test_create_returns_secret_once` — починить через uuid в URL

### [20-infra-deploy](docs/modules/20-infra-deploy.md)
- **scheduler.py 1221 строк** — кандидат на split в `server/cron/<name>.py`
- **Tailwind CDN → build-step**: инфра готова (`02e0e42`), убрать CDN после 1 недели визуального тестирования (target ~2026-05-18)
- **starlette upgrade** (CVE-2024-47874 / CVE-2025-54121) — нужен staging для проверки breaking changes

### [01-core-auth](docs/modules/01-core-auth.md)
- **JWT aud/iss strict verify** — план:
  - T=2026-05-11: все НОВЫЕ токены содержат `aud=aiche.ru`, `iss=aiche.ru`
  - **T=2026-06-10**: refresh-token TTL=30 дней → legacy токены истекут → безопасно включить `verify_aud=True, verify_iss=True` в `server/auth.py:236`
  - Откат: `verify_aud=False` обратно, юзеры релогинятся

### Общее (cross-module)
- **A11y на остальных страницах** (proposals/sites/chatbots/agents/admin/api/marketplace) — сделано только index.html

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
