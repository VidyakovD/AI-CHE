# TODO — задачи в работе и на очереди

_Последнее обновление: 2026-05-04 (после спринтов «Multi-Agent Orchestra», «Глубокий ресёрч», «UX-полировка», «Production апгрейды», «WhatsApp + Web Push», «Compare runs», «Marketplace», «Public API»)_

---

## 🟢 Работа Claude закрыта (всё что мог в одиночку)

Из исходного списка идей, который юзер дал в чате — **закрыто всё кроме монетизации** (юзер попросил пропустить):

| # | Идея | Статус |
|---|---|---|
| 1 | Multi-Agent Solutions | ✅ 8 пилотов (`5a91f68`+`bcc4cf3`) |
| 2 | Глубокий ресёрч (file_extract / vision / browse) | ✅ |
| 3 | UX-полировка (re-run, templates, share, reactions) | ✅ |
| 4 | Сравнение N запусков | ✅ (`75e2462`) |
| 5.1 | Streaming output финального stage | ✅ |
| 5.2 | DOCX + Excel экспорты | ✅ оба |
| 5.3 | Inline-картинки (DALL-E stage) | ✅ |
| 5.4 | 👍/👎 + auto-flagging | ✅ |
| 6 | Новые типы Solutions | ✅ 8 пилотов всего |
| 7.1 | WhatsApp Wazzup24 | ✅ backend |
| 7.2 | Web Push VAPID | ✅ |
| 7.3 | Marketplace ботов | ✅ backend (нужен UI каталога) |
| 7.4 | Public API для SaaS | ✅ |
| 8 | Монетизация PRO | (юзер попросил не делать) |

---

## 🔴 БЛОКЕР функциональности — действия юзера

### 1. SMTP не работает на проде → юзеры не могут зарегистрироваться

На `/root/AI-CHE/.env` нет `SMTP_HOST`. Из-за этого:
- Юзер регистрируется → **не получает** verification-код → не может попасть в чат / КП / презентации
- Login alerts при новом IP не уходят
- Reset password не работает
- Email-уведомление при привязке TG management не уходит

**Решение** — выбери один российский провайдер:
- **Unisender Go** ~0.30 ₽/письмо, в реестре РКН
- **SendPulse** бесплатно до 12k/мес, РФ-инфра
- **Yandex 360 для бизнеса** от 249 ₽/мес, на твоём `aiche.ru`

Затем:
```bash
ssh root@194.104.9.219
cat >> /root/AI-CHE/.env <<'EOF'
SMTP_HOST=smtp.unisender.com
SMTP_PORT=587
SMTP_USER=<логин>
SMTP_PASS=<пароль>
SMTP_FROM=AI Студия Че <noreply@aiche.ru>
EOF
systemctl restart ai-che
```

---

## ⚠️ 152-ФЗ / 54-ФЗ — юр. процессы

См. полный документ [docs/compliance_ru.md](docs/compliance_ru.md).

| # | Что | Как |
|---|---|---|
| 2 | **🔑 Сохранить ключ шифрования бэкапов в 1Password** | Hex-ключ выведен в чате при настройке (значение в файле `/root/AI-CHE/.backup_encryption_key`). Опционально: положить в env `BACKUP_ENCRYPTION_KEY` и удалить файл. |
| 3 | **Подключить ОФД в ЛК ЮKassa** | https://yookassa.ru/my/ → Настройки → Кассовый чек. Без ОФД мои `receipt`-объекты никуда не идут. Настроить env `YOOKASSA_VAT_CODE` (1 для УСН, 4 для ОСН) + `YOOKASSA_TAX_SYSTEM_CODE`. |
| 4 | **Регистрация оператора ПДн в РКН** | https://pd.rkn.gov.ru/ |
| 5 | **Обновить политику конфиденциальности** | Указать обработчиков: ЮKassa, Anthropic, OpenAI, SMTP-провайдер, Yandex (если будет backup). Шаблон в `docs/compliance_ru.md`. |
| 6 | **Бэкапы вне РФ-сервера / миграция primary в РФ** | Минимум: ежедневная выгрузка `chat.db.YYYY-MM-DD.enc` в Yandex Object Storage (~1 ГБ/мес). Лучше: миграция на VPS в РФ (Selectel/Yandex Cloud/Reg.ru). |

---

## 🟡 Готовые фичи — ждут аккаунтов / договоров юзера

| # | Что | Действия |
|---|---|---|
| 7 | **Wazzup24 (WhatsApp)** | Заключить договор, получить API key + создать channel в их ЛК → задать в карточке бота `wazzup_api_key` + `wazzup_channel_id`. Webhook URL: `https://aiche.ru/webhook/wazzup/<bot_id>?secret=<HMAC>` (secret = `tg_webhook_secret(api_key)`) |
| 8 | **TG management-бот** | Создать через @BotFather → `TG_MGMT_BOT_TOKEN` + `TG_MGMT_BOT_USERNAME` в `.env` + `setWebhook` |
| 9 | **15 видео-туториалов** | Снять MP4 → положить в `views/static/tutorials/<slug>.mp4` (список в `docs/gif_tutorials.md`). После этого подключим lightbox |
| 10 | **Прод ЮKassa** | Сейчас тестовый shop. Заменить `YOOKASSA_SHOP_ID` + `YOOKASSA_SECRET_KEY` на live |
| 11 | **OAuth keys (Google/VK)** | `GOOGLE_CLIENT_ID/SECRET` + `VK_CLIENT_ID` в `.env` |
| 12 | **Web Push: подписаться** | В кабинете → Настройки → 🔔 «Подписаться» → разрешить браузеру (сервер уже настроен с VAPID) |
| 13 | **Ротировать `AIza…` ключ** | Был в старых journalctl до фильтра `_SecretFilter` |

---

## 🟢 Что Claude может делать в новых сессиях

### Marketplace UI (продолжение спринта)
- **Страница `/marketplace.html`** с карточками + фильтрами (категория, цена, рейтинг) + кнопкой «Установить» (платно через `/marketplace/listings/{id}/install`)
- **Кнопка «📤 Опубликовать в Marketplace»** в карточке бота на `/chatbots.html` → POST `/marketplace/listings`
- **«📂 Мои публикации»** в кабинете
- **Админ-страница** `/admin.html` → вкладка «Marketplace» → pending listings + approve/reject

### Документация Public API
- `docs/public-api.md` или Swagger UI с примерами curl
- Вкладка «🔌 Public API» в кабинете → создать токен + список своих + примеры curl

### UX-улучшения (мелочи)
- В `/chatbots.html` карточка бота показывает все 6 каналов (TG/VK/Avito/MAX/Widget/WhatsApp) с переключателями
- В `/proposals.html` показывать список push-подписчиков
- Кнопка «👍/👎» добавить и в legacy plain solutions (не только orchestra)

### Качество пилотов orchestra
- **Тестирование**: прогнать каждый из 8 пилотов на реальных кейсах юзера, найти слабые места в промптах
- При обнаружении 👎 — подкручивать orchestra_json соответствующего Solution через seed-скрипт
- Добавить fallback в parallel_browse: если URL вернул 404 — пробовать без trailing-slash или с www-префиксом

### Security (отложенное)
- **JWT `aud`/`iss` strict verification** — сейчас `verify_aud=False`. Нужен grace-period (накопить токены с aud, потом включить strict).
- **starlette upgrade** — pinned в FastAPI 0.111. CVE-2024-47874, CVE-2025-54121. Апгрейд до 0.115+ с проверкой breaking changes.
- **2FA админки (TOTP через pyotp)** — отдельный flow при логине admin@.
- **Idempotency через Redis** — сейчас in-process. Для multi-worker нужен общий стор.
- **`/admin/reencrypt-secrets`** endpoint для ротации `JWT_SECRET`.
- **systemd `User=aiche`** — отвязать сервис от root.
- **Cloudflare/CDN+WAF** — DDoS-защита + кэш статики.

### Marketplace расширения
- Реальный платёжный flow для marketplace (привязка к ЮKassa, не просто списание баланса)
- Withdrawal для авторов: `User.balance_kop` за установки можно вывести на карту через ЮKassa или платформенный механизм
- Категории listing'ов с UI-фильтрами

### Public API расширения
- Webhook'и для events (на API-токен можно подписаться: «дёрни мне URL когда КП открыли»)
- Endpoint `/api/v1/proposals/{id}/email` — отправить КП клиенту через API
- Endpoint `/api/v1/solutions/{id}/run-orchestra` — запуск бизнес-решений через API
- Endpoint `/api/v1/knowledge/{owner}/{owner_id}/upload` — загрузка KB-файла через API

### Большие фичи / новые направления
- **Электронная подпись КП** — клиент в `/p/{token}` ставит подпись (canvas) → подписанный PDF + audit-trail
- **Видео-приветствие в КП** — генерим короткое видео (Veo) на лету персонализированное под клиента
- **Voice-режим в чате с ИИ** — Whisper input + ElevenLabs TTS output (как ChatGPT Voice)
- **Calendar integration** — авто-создание встречи при ответе клиента «давайте созвонимся»
- **CRM-интеграции** — Bitrix24 / amoCRM webhooks (создание лида при `save_record`)

---

## ✅ Полный список закрытого за все эти спринты

### Спринты 2026-05-02 → 2026-05-04 (бизнес-оркестра)
- 8 orchestra-пилотов: Конкурентный анализ / SWOT / Контент-план / Аудит лендинга / Юр.договор / Аудит соцсети / Финансовый аудит / Холодная email
- Стейдж-типы: web_search / browse_url / parallel_browse / extract_urls / llm / synthesize / parallel_llm / file_extract / vision_describe / generate_image
- Streaming финального synthesize через AsyncAnthropic
- Templates / Re-run / Share / Reactions / DOCX / XLSX / PDF / Compare моделей
- Auto-flagging: 3+ 👎 за 7 дней → email админу
- WhatsApp канал через Wazzup24
- Web Push VAPID + UI subscribe/test + хуки на новую заявку и открытие КП
- Marketplace ботов (backend) + Public API + 70/30 revenue split

### Спринты ранее (security + compliance)
- AES-256-GCM шифрование бэкапов БД
- 54-ФЗ чек в YooKassa (vat_code + tax_system_code из env)
- Маркетинговое согласие отдельно от оферты + UI чекбокс
- Refresh-token rotation single-use + revoke_all_refresh_jtis
- Sites public_token (вместо int_id) + backfill миграция
- Storage-биллинг для RAG-файлов
- Security audit: VK webhook secret / SSRF / XSS / IDOR / TG-link rate-limit / 13 пунктов

### Базовые модули (полностью работают)
- Чат с AI (8 моделей)
- 30 plain Solutions (legacy) + 8 orchestra Solutions
- КП-конструктор + brands + price-lists + 4 шапки + JSON-first
- Презентации v2
- Сайты с WYSIWYG + sandbox-iframe + public_token
- Чат-боты (TG/VK/Avito/MAX/Widget/WhatsApp) + workflow + 7 шаблонов + RAG + semantic search цен
- AI-агенты с очередью + 25+ ролей в registry
- PWA + Desktop standalone + TG management + push
- QR-логин + lite-режим со смартфона + voice
- Marketplace ботов backend + Public API
- 164 теста проходят

---

## 📋 Заметки для следующей сессии Claude

### Запуск тестов
```bash
cd .claude/worktrees/festive-goldwasser-d084fe/
DEV_MODE=true APP_ENV=dev JWT_SECRET=test-jwt-secret-32-chars-long-yes \
ALLOWED_ORIGINS=http://localhost:8000 \
python -m pytest tests/ --tb=line
```

### Ресид orchestra-пилотов после изменений в `seed_orchestra_solutions.py`
```bash
ssh root@194.104.9.219 "cd /root/AI-CHE && /root/AI-CHE/venv/bin/python scripts/seed_orchestra_solutions.py"
```

### Деплой
```bash
git push origin claude/festive-goldwasser-d084fe:main
ssh root@194.104.9.219 "cd /root/AI-CHE && git pull origin main && systemctl restart ai-che"
```

### Полезные команды
- **Audit-log дамп**: `curl https://aiche.ru/admin/actions.txt?since_hours=72&limit=2000 -H "Authorization: Bearer <token>"`
- **JS syntax check после правок views/*.html**: см. `HANDOVER.md` секция «Полезные команды»
- **JS sanity import + миграции**: см. `HANDOVER.md`

### Соглашения
- **Все цены в БД** `pricing_config` — менять через `POST /admin/pricing` без редеплоя
- **Свои API-ключи юзера** — вкладка «Свои API» в кабинете
- **Прайс-листы для КП** — вкладка «📋 Прайсы» в `/proposals.html`
- **Прайс-лист бота** — кнопка `₽` в карточке бота в `/chatbots.html`
- **Native dialogs запрещены** — везде `aiAlert/aiConfirm/aiPrompt`
- **WYSIWYG-редактор** — стандарт для sites + proposals: `contenteditable=true` на body
- **Margin ×7 для презентаций** — внутри `presentation_builder`, в UI не показывается
- **Margin ×5 для orchestra-стадий** — `ai.improve_margin_pct=500`
- **JSON-first генерация** для КП и orchestra-стадий — AI возвращает данные, не HTML/markdown без валидации
- **Бэкапы шифруются AES-GCM** — ключ в `.backup_encryption_key` или env. Restore: `scripts/restore_backup.py`
- **Refresh-token single-use** — после `register_refresh_jti` jti должен быть в наборе
- **Опубликованные сайты** — URL `/sites/hosted/{public_token}/`, не int_id
- **Public API auth** — `Bearer ai_che_<prefix>_<secret>`, scope-проверка через `authenticate_token(required_scope=...)`
- **Web Push** — VAPID-ключи в `.env`: `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY_FILE=/root/AI-CHE/.vapid_private.pem`
- **WhatsApp** — secret webhook = `tg_webhook_secret(wazzup_api_key)`
- **Compare runs** — chat_id формат `cmp_<group>_<model>`, custom orchestra хранится в `run.context._compare_orchestra`
