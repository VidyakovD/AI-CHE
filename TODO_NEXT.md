# TODO — задачи в работе и на очереди

_Последнее обновление: 2026-05-05 после миграции в РФ_

---

## 🔴 Срочные действия юзера (для запуска)

### 1. SSL-сертификат после DNS

**Триггер:** когда `nslookup aiche.ru` покажет `193.187.92.147`.

```bash
ssh root@193.187.92.147

# 1. Получить сертификат через certbot (у нас уже установлен)
certbot --nginx -d aiche.ru -d www.aiche.ru \
    --email vidyakov@obsidian.ai --agree-tos --no-eff-email

# 2. Заменить nginx конфиг на SSL-вариант
sed 's|__DOMAIN__|aiche.ru|g' /root/AI-CHE/deploy/nginx-ssl.conf > /etc/nginx/sites-available/aiche.ru
nginx -t && systemctl reload nginx

# 3. Проверить
curl -I https://aiche.ru/healthz
```

certbot настроит автообновление через cron — больше делать ничего не нужно.

### 2. Выключить старый сервер (через 24-48ч после новой работы)

```bash
ssh root@194.104.9.219
systemctl stop ai-che
systemctl disable ai-che
# VM можно держать выключенной 30 дней как backup, потом удалить через панель Clouvider
```

---

## 🔴 БЛОКЕРЫ для коммерческого запуска (только юзер)

### 1. SMTP не работает на проде → юзеры не могут зарегистрироваться

На `/root/AI-CHE/.env` нет `SMTP_HOST`. Из-за этого:
- Юзер регистрируется → не получает verification-код
- Login alerts при новом IP не уходят
- Reset password не работает

**Решение** — выбрать российский провайдер:
- **Unisender Go** ~0.30 ₽/письмо, в реестре РКН
- **SendPulse** бесплатно до 12k/мес, РФ-инфра
- **Yandex 360 для бизнеса** от 249 ₽/мес, на твоём `aiche.ru`

```bash
ssh root@193.187.92.147
cat >> /root/AI-CHE/.env <<'EOF'
SMTP_HOST=smtp.unisender.com
SMTP_PORT=587
SMTP_USER=<логин>
SMTP_PASS=<пароль>
SMTP_FROM=AI Студия Че <noreply@aiche.ru>
EOF
systemctl restart ai-che
```

### 2. Прод-shop ЮKassa (сейчас тестовый)

Заявка на `https://yookassa.ru/my/` → переключить shop на live. После одобрения 1-3 дня:
- Заменить `YOOKASSA_SHOP_ID` и `YOOKASSA_SECRET_KEY` в `.env`
- `systemctl restart ai-che`

### 3. ОФД для 54-ФЗ чеков

В ЛК ЮKassa → Настройки → Кассовый чек → подключить **Атол Онлайн** или **Контур.ОФД**. Без этого `receipt`-объекты в наших платежах никуда не идут.

Также проверить env:
- `YOOKASSA_VAT_CODE=1` (Без НДС) или `4` (НДС 20%)
- `YOOKASSA_TAX_SYSTEM_CODE=2` (УСН доходы)

### 4. РКН — регистрация оператора ПДн

`https://pd.rkn.gov.ru/` → подать заявление. **152-ФЗ ст. 22**. После переезда в РФ — проще.

### 5. Бэкапы в Yandex Object Storage

Сейчас бэкапы лежат локально на `/root/AI-CHE/backups/*.enc`. Для compliance ст. 18 152-ФЗ нужна копия в **РФ-облаке**:

```bash
apt install -y awscli
aws configure  # endpoint https://storage.yandexcloud.net, region ru-central1

cat > /etc/cron.daily/aiche-yandex-backup <<'EOF'
#!/bin/bash
DATE=$(date +%Y-%m-%d)
LATEST=$(ls -1t /root/AI-CHE/backups/chat.db.*.enc | head -1)
[ -n "$LATEST" ] && aws s3 cp "$LATEST" "s3://aiche-backups/db/$DATE.enc" \
    --endpoint-url https://storage.yandexcloud.net
EOF
chmod +x /etc/cron.daily/aiche-yandex-backup
```

Стоимость: ~30 ₽/мес за 100 ГБ.

---

## 🟡 Готовые фичи — ждут аккаунтов / договоров юзера

| # | Что | Действия |
|---|---|---|
| 1 | **Wazzup24 (WhatsApp)** | Договор + API key → задать `wazzup_api_key` + `wazzup_channel_id` в карточке бота. Webhook URL: `https://aiche.ru/webhook/wazzup/<bot_id>?secret=<HMAC>` |
| 2 | **TG management-бот** | Создать через @BotFather → `TG_MGMT_BOT_TOKEN` + `TG_MGMT_BOT_USERNAME` в `.env` + `setWebhook` |
| 3 | **OAuth keys (Google/VK)** | `GOOGLE_CLIENT_ID/SECRET` + `VK_CLIENT_ID` в `.env`. Регистрация требует юр-лица + верификации домена |
| 4 | **Web Push: подписаться** | В кабинете → Настройки → 🔔 «Подписаться» → разрешить браузеру |
| 5 | **Видео-туториалы** | Снять MP4 → положить в `views/static/tutorials/<slug>.mp4` |

---

## 🟢 Что Claude может делать в новых сессиях

### Большие фичи
- **A/B-тест промптов в orchestra** — дополнение к compare моделей. UI: 2 промпт-варианта в одном Solution → юзер выбирает 👍 → собираем training-data
- **Bulk-генерация КП из CSV** — для агентств: загрузил список клиентов → AI пакетно генерит 50 КП
- **AI-улучшение существующих КП** — кнопка «сделай убедительнее» под каждым блоком
- **Электронная подпись на презентациях / договорах** — расширение нашего ESign на любой PDF
- **Voice-режим в orchestra** — диктовать input голосом

### Технический долг
- **starlette upgrade** — pinned в FastAPI 0.111. CVE-2024-47874 / CVE-2025-54121. Нужен staging для проверки breaking changes
- **JWT aud/iss strict verify** — сейчас `verify_aud=False`. Нужен grace-period 30 дней (накопить токены с aud, потом включить strict)
- **systemd User=aiche** — отвязать сервис от root. Риск падения, нужно вручную проверить chown файлов
- **Webhook'и для Public API: больше событий** — `solution.started`, `bot.message_in`, `presentation.done`. Уже есть инфраструктура, добавить event keys в whitelist
- **Endpoint /api/v1/solutions/{id}/run-orchestra** — запуск orchestra через API
- **Endpoint /api/v1/knowledge/{owner}/{owner_id}/upload** — KB через API

### Новые большие проекты
- **Видео-приветствие в КП** — генерим короткое видео (Veo) персонализированное под клиента
- **Calendar integration** — авто-создание встречи при ответе клиента «давайте созвонимся»
- **PostgreSQL миграция с SQLite** — для масштабирования > 5000 юзеров. День работы
- **Redis для idempotency и кэшей** — multi-worker scaling
- **Cloudflare/CDN+WAF** — DDoS-защита + кэш статики

### Marketplace расширения
- Реальный платёжный flow для marketplace (привязка к ЮKassa, не просто баланс)
- Withdrawal для авторов: `User.balance_kop` за установки → вывод на карту через ЮKassa
- Категории listing'ов с UI-фильтрами (есть в backend, доработать UI)

### Качество пилотов orchestra
- Прогнать каждый из 8 пилотов на реальных кейсах юзера, найти слабые места
- При обнаружении 👎 — подкручивать `orchestra_json` через seed-скрипт
- **Exit-criteria** для stages — `validate_output: "min_length:200"` или `json_schema:...` + retry × 1
- **Fallback в parallel_browse** — если URL вернул 404 → пробовать без trailing-slash или с www-префиксом

---

## ✅ Полный список закрытого за все спринты последних суток

### Спринты 2026-05-05
- **Миграция в РФ** — 193.187.92.147 (Москва, HOSTKEY) + AI-прокси toolkit (`6e9fd0a`)

### Спринты 2026-05-04
- **CRM-интеграции** Bitrix24/amoCRM/generic webhook + `USER_GUIDE.md` 1100 строк (`2784f5a`)
- **Drag-n-drop файлов** + понятные названия workflow-нод (`2784f5a`)
- **Idempotency через DB** (multi-worker safety) + 18 новых тестов + reencrypt-secrets endpoint (`5cf647b`)
- **Cron-расписания orchestra** — превращает покупки в подписку (`158a96a`)
- **Bug fixes:** balance pill дублировал ЛК + кнопки бота без подписей (`984d16b`)
- **Voice-режим:** Whisper + 6 TTS-голосов (`0a1202b`)
- **2FA админки + prompt-injection защита** (`04ded59`)
- **Электронная подпись КП** с canvas + audit-trail (`0a8bdf7`)
- **Public API: Webhooks + полная документация** (`5619224`)
- **Marketplace UI** (каталог + публикация + админ-модерация) (`8589cbd`)
- **6 mid-priority UX** (push на события / Esc+Ctrl+K / toast / skeleton / touch-targets) (`58c2f62`)
- **5 quick-wins UX** (колокольчик / auto-save / welcome-tour / cost-preview / humanizeError) (`d8ffb61`)
- **Bug fixes UX:** `/auth/me` + balance_kopecks + Internal Server Error generic (`a2c52b8` + `55f191d`)
- **13 P1/P2/P3 багов** из полного аудита проекта (`6f02d1e`)

### Базовые модули (полностью работают)
- Чат с AI (8 моделей + Whisper + TTS)
- 30 plain Solutions + 8 orchestra Solutions с глубоким ресёрчем
- КП-конструктор + бренды + прайсы + 4 шапки + JSON-first + **электронная подпись**
- Презентации v2
- Сайты с WYSIWYG + sandbox-iframe + public_token
- Чат-боты (TG/VK/Avito/MAX/Widget/WhatsApp) + workflow + 7 шаблонов + RAG + semantic search цен
- AI-агенты с очередью + 25+ ролей в registry + prompt-injection защита
- PWA + Desktop standalone + TG management + push
- QR-логин + lite-режим со смартфона + voice
- Marketplace ботов с 70/30 revenue split
- Public API + Webhooks + CRM-интеграции
- Cron-расписания orchestra
- 2FA админки (TOTP)
- 182 теста проходят

---

## 📋 Заметки для следующей сессии Claude

### Подключение к проду

**Через SSH-ключ:**
```bash
HOME="C:\\Users\\Денис" ssh -i 'C:\\Users\\Денис\\.ssh\\id_ed25519' \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  root@193.187.92.147 "..."
```

**Если ключ не работает** (вдруг провайдер пересоздал VM):
- Пароль root в `/root/.aiche-server-password` (chmod 400) — но через SSH без ключа не достать
- Использовать paramiko через Python с паролем
- Старый IP на NL (если ещё работает): `194.104.9.219`

### Запуск тестов
```bash
DEV_MODE=true APP_ENV=dev JWT_SECRET=test-jwt-secret-32-chars-long-yes \
ALLOWED_ORIGINS=http://localhost:8000 PYTHONIOENCODING=utf-8 \
python -m pytest tests/ --tb=line
# 182 passed expected
```

### Деплой
```bash
git push origin claude/<branch>:main
HOME="C:\\Users\\Денис" ssh -i "C:\\Users\\Денис\\.ssh\\id_ed25519" \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  root@193.187.92.147 \
  "cd /root/AI-CHE && git pull origin main && systemctl restart ai-che && sleep 6 && systemctl is-active ai-che"
```

### Полезные команды
- **Audit-log дамп**: `curl https://aiche.ru/admin/actions.txt?since_hours=72&limit=2000 -H "Authorization: Bearer <token>"`
- **JS syntax check после правок views/*.html**:
  ```bash
  node -e "const fs=require('fs');for(const f of ['views/index.html','views/proposals.html','views/sites.html','views/chatbots.html','views/presentations.html','views/agents.html','views/admin.html','views/marketplace.html','views/api.html','views/icons.js']){const src=fs.readFileSync(f,'utf8');if(f.endsWith('.js')){try{new Function(src);console.log(f+': OK');}catch(e){console.log(f+': '+e.message);}}else{const m=src.match(/<script>([\s\S]*?)<\/script>/g)||[];let any=true;for(let i=0;i<m.length;i++){try{new Function(m[i].replace(/^<script>|<\/script>\$/g,''));}catch(e){console.log(f+' #'+i+': '+e.message);any=false;}}if(any)console.log(f+': OK');}}"
  ```

### Ресид orchestra-пилотов после изменений
```bash
ssh root@193.187.92.147 "cd /root/AI-CHE && /root/AI-CHE/venv/bin/python scripts/seed_orchestra_solutions.py"
```

### Соглашения
- **Все цены в БД** `pricing_config` — менять через `POST /admin/pricing` без редеплоя
- **Свои API-ключи юзера** — вкладка «Свои API» в кабинете
- **Прайс-листы для КП** — вкладка «📋 Прайсы» в `/proposals.html`
- **Прайс-лист бота** — кнопка `₽` в карточке бота в `/chatbots.html`
- **Native dialogs запрещены** — везде `aiAlert/aiConfirm/aiPrompt/aiToast/aiAlertError`
- **WYSIWYG-редактор** — стандарт для sites + proposals: `contenteditable=true` на body
- **Margin ×7 для презентаций** — внутри `presentation_builder`, в UI не показывается
- **Margin ×5 для orchestra-стадий** — `ai.improve_margin_pct=500`
- **JSON-first генерация** для КП и orchestra-стадий
- **Бэкапы шифруются AES-GCM** — ключ в `.backup_encryption_key` или env. Restore: `scripts/restore_backup.py`
- **Refresh-token single-use** — после `register_refresh_jti` jti должен быть в наборе. Race-safe `_atomic_jtis_update`.
- **Опубликованные сайты** — URL `/sites/hosted/{public_token}/`, не int_id
- **Public API auth** — `Bearer ai_che_<prefix>_<secret>`, scope-проверка через `authenticate_token(required_scope=...)` + is_verified
- **Web Push** — VAPID-ключи в `.env`: `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY_FILE=/root/AI-CHE/.vapid_private.pem`
- **WhatsApp** — secret webhook = `tg_webhook_secret(wazzup_api_key)`
- **Compare runs** — chat_id формат `cmp_<group>_<model>`, custom orchestra хранится в `run.context._compare_orchestra`
- **AI-прокси** — `AI_HTTPS_PROXY` общий fallback, или `<PROVIDER>_HTTPS_PROXY` специфичный (см. `_ai_proxy()`)
- **Idempotency** — DB-table `IdempotencyRecord` с UNIQUE(user_id, key), TTL 5 мин, cleanup в scheduler
- **Multi-worker race** — `requests_count` → atomic UPDATE, refresh_jtis → with_for_update + re-fetch
