# Журнал тестирования и багов

> Юзер тестирует функции в проде/локально, пишет сюда что работает и где сломалось.
> Claude (в следующих сессиях) обрабатывает Open-баги по мере появления.
>
> **Новые записи — СВЕРХУ.** Статусы: 🔴 Open → 🔧 In progress → ✅ Fixed → 🟡 Won't fix.
>
> Где смотреть глубже:
> - Серверные ошибки на проде: `journalctl -u ai-che --since '1 hour ago' | grep -iE 'error|trace|exception'`
> - Аудит действий в БД: таблица `action_logs` (или `/admin/actions.txt?since_hours=24`)
> - Frontend: DevTools → Console + Network → отправь Claude скрин/копию

---

## 🔴 Open

_(пусто — записывай сюда то что нашёл при тестировании)_

---

## 🔧 In progress

_(пусто)_

---

## ✅ Fixed

### 2026-05-23 — wave 2 deep audit (HEAD `cb36d08`)

После того как закрыли P0/P1 первой волны, прогнали ещё 3 explore-агента
по schedules/mcp/voice, knowledge/sites/marketplace, proposals/public_api.

**Закатанные фиксы (cb36d08):**
- **PDF DoS** (knowledge.py:_extract_pdf) — лимит 500 страниц. Защита от
  10000-страничного PDF съедающего RAM (MAX_TOTAL_TEXT_CHARS обрезал текст
  только ПОСЛЕ парсинга всех страниц).
- **DOCX XXE fail-closed** (knowledge.py:_extract_docx) — раньше при
  отсутствии defusedxml fallback на unsafe `xml.etree.ElementTree`. Теперь
  RuntimeError. defusedxml уже в requirements, но защита defence-in-depth.
- **MCP per-tool scope** (mcp.py + public_api.py) — токен с scope=solutions
  больше НЕ может вызвать list_chatbots/create_proposal через MCP.
  `_TOOL_REQUIRED_SCOPE` карта; legacy токены без CSV-scopes не затронуты.

**Закатано в 69cf76b (этой же сессии):**
- SDK bump: openai 1.30→1.109, anthropic 0.26→0.104, httpx 0.27→0.28
- CI-страж `tools/check_worker_locks.py` — фейлит pipeline при регрессии
  TTL-паттерна (ловит синтетический race ttl=55 vs sleep=60).

**False positives (агенты ошиблись):**
- HMAC compare_digest — все 6 точек сравнения secret используют timing-safe
- ProposalSignature replay — `proposal_id UNIQUE` уже на месте
- Sites /iterate patch whitelist — schema де-факто только find/replace
- ApiWebhook.secret plaintext — уже закатан в 5c7176e
- /me API scope bypass — work-as-designed, нет sensitive data
- Orchestra schedule race — `worker_lock("orchestra_schedules")` защищает
- public_token guessable — 144 bit entropy, не bruteforce'able

**Отложено (требует продуктового решения):**
- Embedding billing bypass: knowledge upload не списывает оценочный embedding-cost.
  Юзер может загрузить 50 файлов × 2MB = 100MB → ~$2 на наши деньги, бесплатно.
  Нужен cost-estimator + UX «спишется N коп за индексацию».
- Cron parser DoS `*/1 * * * *`: every-minute fire на per-user cron. Low-risk
  pre-launch (4 юзера). Стоит добавить MIN_INTERVAL_MINUTES=5 в server/scheduler.py.
- XLSX cell count unbounded: 100 листов × 5000 строк допустимо, но без cap
  на cell content. Low-risk.

### 2026-05-23 — auto-test после коммита 5c7176e

Прогнан Claude'ом сразу после деплоя. Проверено что доступно без login/payment-credentials.

- **[OK] SSH smoke endpoints на проде** — 12 публичных HTML отдают 200,
  auth-only возвращают 401, POST без body = 422 (Pydantic validation).
  Время отклика главной 8ms, 366KB. Сервис `active`.
- **[OK] Schema sanity (74 таблицы PG)** — все critical column types корректны:
  `users.tokens_balance=INTEGER` (копейки целые), `api_webhooks.secret=VARCHAR`,
  `push_subscriptions.auth=VARCHAR`, `users.totp_secret=VARCHAR`.
  UNIQUE на месте: `uq_transactions_yookassa_id`, `uq_promo_uses_code_user`,
  `uq_subscriptions_yookassa_id`, `uq_idempotency_user_key`.
  Счётчики: 4 user / 115 tx / 4 brand / 130 audit_log / 0 webhook+push (pre-launch).
  Alembic version=`506fa9eb9a82`.
- **[OK] Cron loops alive** — после рестарта 15:18:52 UTC все 4 uvicorn-worker
  стартанули Scheduler + API-key health-check + Agents-modules cron + IMAP loop.
  Никаких exceptions.
- **[OK] EncryptedString roundtrip** (локальный pytest) —
  ApiWebhook.secret и PushSubscription.auth пишутся как `enc:v1:gAAAAABq...` в БД,
  читаются через ORM как plaintext. Legacy plaintext (без префикса) тоже
  читается без изменений (backward compat).
- **[OK] Live проверка iframe sandbox** на /presentations.html —
  `sandbox="allow-scripts"` (без allow-same-origin) как и должно быть.
- **[OK] 6 новых страниц рендерятся правильно** — finance/calendar/notes/
  creators/agents-modular/presentations. Все 200, корректные `<title>`,
  тёмная тема, icons.js подключён. На главной 0 JS-ошибок в console.

---

## 🟡 Won't fix / known limitations

### bcrypt+Python 3.14 на dev-машине — 500 на auth endpoints локально
- **Симптом**: POST /auth/register, /auth/login возвращают 500 локально.
  Лог: `ValueError: password cannot be longer than 72 bytes` от
  `passlib.detect_wrap_bug`.
- **Причина**: bcrypt 5.x несовместим с passlib 1.7.4. На Py3.14
  passlib делает probe с >72-byte паролем который bcrypt 5.x отвергает.
- **Прод не затронут**: Python 3.10, requirements.txt пинит `bcrypt<5.0`.
- **Влияние на dev-тестирование**: невозможно auth flow проверить локально через
  curl/preview. Обходной путь — `pip install 'bcrypt<5.0'` явно в dev venv,
  или dev на Py3.12 / Py3.13.
- **Документировано** в memory project_state_may23.md.

### API-key health-check: Google (Imagen+Veo) — credits depleted
- **Идентифицирован**: `api_keys.id=7, provider='google', label='Imagen + Veo (prod)'`
- **Status**: `error`, `last_error = "HTTP 429: Your prepayment credits are depleted"`
- **Влияние**: модели Imagen/Veo (генерация картинок/видео) не работают на проде
- **Что делать тебе**: пополнить Google Cloud билинг и дождаться restore квоты
  ИЛИ удалить запись из api_keys если Imagen больше не используешь.

### Frontend redirect unauth юзеров на главную
- При попытке открыть `/finance.html`, `/calendar.html` etc без auth-cookie
  frontend сразу делает navigate на `/`. Это **корректно** (защита UI),
  но мешает прокликать страницы без логина.
- Не баг, **work-as-designed**.

---

## Шаблон записи

```
### YYYY-MM-DD HH:MM — <модуль/раздел>
- **Что делал**: открыл /finance.html, нажал «Добавить транзакцию», ввёл сумма 500 ₽
- **Ожидал**: транзакция появится в списке, баланс обновится
- **Результат**: 500 ошибка, в Console: TypeError: Cannot read properties of null
- **Воспроизводимость**: всегда / раз через раз / 1 из 10
- **Скрин/лог**: (приложи в чат)
- **Статус**: 🔴 Open
- **Контекст**: браузер, юзер ID, прод/dev
```

## Что просить у Claude когда придёшь с багом

1. **Один баг — один блок** в формате выше.
2. Если есть Network-payload — копируй request URL + body + response.
3. Если регрессия (раньше работало) — укажи когда сломалось.
4. Тяжёлые сценарии (баги в биллинге, потеря данных) — помечай 🔥 в начале блока.

## Чекист функционала для прохода

> Используй как стартовый набор. Отмечай ✅ если работает, 🔴 если сломалось.

### Auth & профиль
- [ ] Регистрация нового юзера
- [ ] Подтверждение email (письмо приходит, link работает)
- [ ] Вход + JWT cookie
- [ ] 2FA setup → enable → login через 2FA
- [ ] VK OAuth login
- [ ] QR-логин (мобилка → десктоп)
- [ ] Refresh-token rotation
- [ ] Logout

### Чат с AI (5 провайдеров)
- [ ] Чат с GPT
- [ ] Чат с Claude
- [ ] Чат с Grok
- [ ] Чат с Perplexity (с цитатами)
- [ ] Чат с Imagen/Veo (генерация картинок/видео)
- [ ] Голос → транскрипция → ответ
- [ ] Маски (системные промпты): персонажи, переключение

### ИИ Агенты (модульный оркестратор Че)
- [ ] Подключить модуль из каталога (любой)
- [ ] Изменить настройки модуля (PATCH)
- [ ] Запустить модуль вручную (invoke)
- [ ] Отключить модуль (DELETE)
- [ ] Cron-расписание модуля (подождать tick)
- [ ] Прокачка L0 → L1 (после N взаимодействий)

### Модули с UI-страницами
- [ ] /creators.html — добавить бренд, контент-план, prepare, autopost TG
- [ ] /finance.html — добавить транзакцию, CSV-импорт
- [ ] /calendar.html — добавить событие, Google OAuth, Yandex CalDAV
- [ ] /notes.html — добавить заметку, поиск (RAG)

### Биллинг
- [ ] Топап через ЮKassa (тестовый платёж)
- [ ] Списания за AI-вызовы (баланс уменьшается)
- [ ] Списания за модули (cron-tick → транзакция)
- [ ] /storage биллинг (knowledge base, файлы)
- [ ] История транзакций

### Чат-боты (6 каналов)
- [ ] Создать бота (любой шаблон)
- [ ] TG webhook (привязать токен, /start работает)
- [ ] VK longpoll
- [ ] Avito, Max, Wazzup — хотя бы попытка подключения
- [ ] Web-виджет на сайте

### Сайты, КП, Презентации
- [ ] Сгенерировать сайт (sandbox-iframe edit mode)
- [ ] Опубликовать сайт (hosted_path)
- [ ] КП с e-подписью (proposal_public.html)
- [ ] Презентация (HTML preview + PDF + PPTX)

### Public API + MCP
- [ ] Создать API-токен (POST /apikeys, scope)
- [ ] Bearer-вызов к public endpoint
- [ ] Webhook receiver (HMAC signature валидный)
- [ ] MCP JSON-RPC от Claude Desktop

### Админка
- [ ] /admin/stats
- [ ] /admin/users (list, ban/unban)
- [ ] /admin/adjust-balance (через TOTP)
- [ ] /admin/reencrypt-secrets (после ротации JWT_SECRET)
- [ ] /admin/actions.txt (live audit log)

### Push, Email, Notifications
- [ ] Web Push подписка (VAPID)
- [ ] Email уведомление (низкий баланс, новая заявка)
- [ ] In-app notifications (/user/notifications/recent)
