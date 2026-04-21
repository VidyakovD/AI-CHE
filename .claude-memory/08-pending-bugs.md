# 08. Что не сделано, известные баги, TODO

## 🔴 Блокеры от владельца (ждут внешних ключей)

1. **Google OAuth** — нужны `GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET`
   - Получить: console.cloud.google.com → создать проект → OAuth consent screen → Credentials → OAuth client ID → Web
   - Redirect URI: `https://aiche.ru/auth/oauth/google/callback`
   - Код готов (routes/oauth.py), кнопки в UI есть

2. **VK OAuth** — нужны `VK_CLIENT_ID` + `VK_CLIENT_SECRET`
   - Получить: dev.vk.com → создать → Веб → redirect URI: `https://aiche.ru/auth/oauth/vk/callback`
   - Код готов

3. **Боевая ЮKassa** — нужны live `YOOKASSA_SHOP_ID` + `YOOKASSA_SECRET_KEY`
   - Сейчас: test_eYtcLG_... (тестовый)
   - После замены — всё должно работать (HMAC проверка, атомарные платежи)

4. **Рабочие Claude/Anthropic ключи**
   - Текущие 3 ключа awstore.cloud — регулярно TLS-ошибка `TLSV1_ALERT_INTERNAL_ERROR`
   - Либо свежие awstore, либо прямой Anthropic + убрать `ANTHROPIC_BASE_URL`
   - Для юзеров критично: Claude-ноды в воркфлоу и бизнес-промпты используют Claude

## 🟡 Техдолг, можно сделать самостоятельно

### Keywords агентов — морфология русского

`server/agents/registry.py` — keywords часто не матчат окончания слов:
- `смета` не ловит «смету» / «сметой»  
- `маржа` не ловит «маржу»
- Исправлено: left word-boundary (был substring bug «фер» → оферте)

Решение: пройтись по 22 агентам, заменить на корни:
- `смета` → `смет`
- `маржа` → `марж`
- `договор` (уже OK)
- и т.д.

### XSS-аудит в HTML

`esc()` helper определён во всех HTML-файлах, но полный обход **26+ мест** `innerHTML` с user-input не сделан. Возможные риски в:
- `views/sites.html:369` (есть esc, но надо проверить)
- `views/chatbots.html:209`
- `views/index.html:784`
- Ещё 20+ мест

Отдельная инвентаризация — аудит + патчи.

### Alembic миграции

Сейчас `server/db.py::LIGHTWEIGHT_MIGRATIONS` покрывает только `ALTER TABLE ADD COLUMN`. Сложные миграции (drop column, rename, data migration) — вручную на проде.

Внедрить Alembic:
1. `alembic init migrations`
2. `env.py` с нашими моделями (autogenerate)
3. `alembic stamp head` на проде (пометить текущую схему как baseline)
4. Новые изменения через `alembic revision --autogenerate`

**Риск**: stamp на проде без тестирования на staging может побить БД. Делать осторожно.

### Автобэкап БД

Сейчас только ручные `chat.db.backup-*` перед миграциями. Нужен:
- cron-скрипт каждую ночь → backup на Я.Диск (токен уже есть в env `YAD_TOKEN`)
- Ротация 30 дней

### Avito HMAC

Webhook `/webhook/avito/{id}` принимает любые запросы (с rate-limit 120/мин). У Avito Messenger нет публичной HMAC-подписи, поэтому:
- Либо использовать `Authorization: Bearer <client_secret>` если Avito поддерживает
- Либо фильтровать по IP Avito (если они публикуют)

### Мониторинг

- Нет Sentry/логов в файл (только journalctl)
- Health-check API-ключей есть (раз в час), но не все провайдеры тестируются
- Нет uptime-мониторинга извне

### Sites on separate subdomain

`/sites/hosted/*` сейчас через sandbox iframe (origin=null внутри). Правильнее:
- DNS: `*.aiche-sites.ru` → тот же nginx
- Nginx: отдельный vhost для `sites.aiche.ru`
- FastAPI: при запросе на другой domain — отдавать HTML напрямую (origin не aiche.ru → кража токенов невозможна)

Сейчас работает через sandbox-iframe (хуже UX но защищено). Требует DNS/nginx изменений.

## 🟢 UX улучшения (из моих аудитов)

См. подробности в `git log --grep="ux"`. Уже сделано:
- Loading spinners на main flow
- Friendly errors («Сервис временно недоступен» → конкретика)
- Dashboard расходов в ЛК
- CSV экспорт
- Low-balance email
- Реферальная статистика
- Мобильный гамбургер-меню (только в index.html, другие — не адаптивны)

**Не сделано:**
- Полная mobile-адаптация chatbots/agents/sites/admin (сейчас только в index)
- Welcome-тур для новых юзеров
- Демо-режим без логина (первое сообщение в чате)
- Экспорт транзакций в PDF (CSV есть)
- A/B тестирование промптов

## 🐛 Известные баги (не критичные)

1. **Anthropic proxy нестабилен** — периодический TLS error. Не наш баг, но влияет на Claude-запросы.
2. **`.env` удаляется при `git reset --hard`** на проде — нужен backup + restore в deploy pipeline (реализовано).
3. **При wfcRun в конструкторе** показывается только результат финального узла, не всего графа — логика есть но UX хромает.
4. **При смене JWT_SECRET** все сессии слетают. После моего коммита `key-versioning` — LEGACY_JWT_SECRETS позволяет миграцию без потери IMAP-паролей, но JWT-токены юзеров всё равно инвалидируются (это нормальное поведение).
5. **CSV injection** — добавил `_csv_safe` для опасных символов, но не для всех CSV (только `/user/transactions.csv`). Нужно применить во всех CSV-экспортах если появятся новые.
6. **`wfc-node resize` с масштабом канваса** — учтён scale, но при резком zoom может слегка сбоить.

## 📌 Последние найденные и исправленные баги

| Коммит | Баг | Фикс |
|---|---|---|
| `762c5c4` | Бот молчит в TG — orchestrator single-downstream отключал все ветки | auto-select единственного + guard на None choice |
| `d1083db` | Вкладка Агенты не кликается — JS syntax error | replace_all сломал single-line string, стало multi-line |
| `79157e9` | Orchestrator keyword «фер» матчил «оферте» → estimator вместо lawyer | left word-boundary regex |
| `5c224db` | Белые квадраты, ? popover не виден, нода не растёт вниз | убран `overflow:auto` + свой resize-handle |
| `a7bd783` | Rate-limit login прошёл 12/12 (multi-worker bypass) | SQLite WAL shared store |
| `a40b080` | CSV injection, IDOR в agent/status, OAuth ATO, SSRF, XXE, etc. | Комплексный security hardening |

## 📊 Приоритеты на следующую сессию (если пользователь спросит «что дальше»)

1. **Ревокировать скомпрометированные ключи** — OpenAI/Anthropic/Google в истории git. Владелец должен это сделать в их панелях.
2. **Google/VK OAuth** — как только ключи придут, протестировать flow
3. **Alembic** — сделать staging-тест перед внедрением
4. **Keywords агентов** — быстрый win, чистка окончаний (1 час)
5. **Автобэкап БД на Я.Диск** — есть токен, нужен cron
6. **Mobile-адаптация** остальных страниц

## 🗒 Пользовательские запросы, которые всплывают

Периодически возникают:
- «Бот не отвечает в TG» → проверять `api_keys.status`, логи workflow, новый токен у @BotFather
- «Cколько это стоит?» → открыть https://aiche.ru/admin.html → Прайс / или БД `model_pricing`
- «Добавь новый модуль» → копировать template route + view + LIGHTWEIGHT_MIGRATIONS для таблицы

## Пустые/неполные места в коде

Поиском:
```bash
grep -rn "TODO\|FIXME\|XXX\|HACK" server/ views/ --include="*.py" --include="*.html"
```

Ключевые:
- `routes/payments.py`: `return_url` defaults на APP_URL — ОК, но логика refund неполная
- `chatbot_engine.py`: некоторые ноды (`yd_upload`, `kb_search`) минимальная обработка ошибок
- `views/*.html`: 26+ `innerHTML` без систематического esc (аудит не завершён)
