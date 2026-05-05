# 🚀 Миграция AI Студии Че на новый сервер

Гайд для переноса с **194.104.9.219** (NL, Clouvider) на **193.187.92.147** (RU, Moscow, HOSTKEY).

**Время простоя:** ~5-10 минут (только пока DNS обновится).

---

## ⏱ Что нужно знать заранее

- **Старый сервер:** `194.104.9.219` (Нидерланды) — работает, доступ есть
- **Новый сервер:** `193.187.92.147` (Москва, HOSTKEY) — RU IP, Ubuntu (предположим)
- **Локация Москва** = AI-провайдеры (OpenAI, Anthropic, Google, Perplexity) **заблокируют** → нужен EU-прокси
- **EU-прокси:** дополнительный VPS (Hetzner/DigitalOcean) за €4-5/мес
- **Бэкап БД:** автоматически создаётся скриптом перед импортом
- **Откат:** все старые данные сохраняются в `/tmp/aiche-rollback-*` на 30 дней

---

## 🗺 Полный план миграции

### Этап 1. Подготовка (не критично по времени, можно делать заранее)

#### 1.1. Доступ к новому серверу

На вашей локальной машине:
```bash
# Скопируйте свой публичный SSH-ключ на новый сервер
ssh-copy-id root@193.187.92.147
# (введите пароль один раз — потом будет ходить по ключу)

# Проверка
ssh root@193.187.92.147 "echo OK; uname -a"
```

#### 1.2. EU-прокси для AI-вызовов

**Hetzner CX22** (Германия) — €4.51/мес:

```bash
# 1. Зарегистрируйтесь на console.hetzner.cloud
# 2. Создайте сервер CX22 (Ubuntu 24.04, Falkenstein DE)
# 3. SSH к новому серверу:
ssh root@<HETZNER_IP>

# 4. Установите squid:
apt update && apt install -y squid apache2-utils

# 5. Создайте пароль для прокси:
htpasswd -c /etc/squid/passwd aiche
# Введите случайный пароль 32+ символа, СОХРАНИТЕ его

# 6. Конфиг squid (/etc/squid/squid.conf):
cat > /etc/squid/squid.conf <<'EOF'
http_port 3128
auth_param basic program /usr/lib/squid/basic_ncsa_auth /etc/squid/passwd
auth_param basic realm AI-Studio-Che-Proxy
acl authenticated proxy_auth REQUIRED
http_access allow authenticated
http_access deny all
# Только AI-домены
acl ai_domains dstdomain api.openai.com api.anthropic.com generativelanguage.googleapis.com api.perplexity.ai api.x.ai
http_access allow authenticated ai_domains
# Логи
access_log /var/log/squid/access.log squid
EOF

# 7. UFW
ufw allow 22/tcp
ufw allow 3128/tcp comment 'Squid AI proxy'
ufw --force enable

# 8. Запуск
systemctl restart squid && systemctl enable squid
```

**Проверка с локальной машины:**
```bash
curl -x "http://aiche:<PASSWORD>@<HETZNER_IP>:3128" https://api.openai.com/v1/models
# Должен вернуть JSON с моделями (если есть OpenAI ключ)
```

**ИТОГ ЭТАПА 1:**
- Hetzner-прокси работает
- URL: `http://aiche:<PASS>@<HETZNER_IP>:3128`
- Сохраните в безопасном месте

---

### Этап 2. Настройка нового сервера

#### 2.1. Подключение и базовая настройка

```bash
ssh root@193.187.92.147

# Обновить систему
apt update && apt upgrade -y

# Часовой пояс — Москва
timedatectl set-timezone Europe/Moscow

# Проверка
hostnamectl
```

#### 2.2. Запуск установочного скрипта

```bash
# Клонируем репозиторий и запускаем setup
cd /tmp
git clone https://github.com/VidyakovD/AI-CHE.git
cd AI-CHE

# Запуск установки (~3-5 минут)
bash scripts/migrate_setup.sh
```

Что произойдёт:
- ✅ Установятся пакеты: Python, nginx, certbot, ufw, fail2ban, шрифты
- ✅ Клонируется репо в `/root/AI-CHE`
- ✅ Создастся venv + `pip install -r requirements.txt`
- ✅ UFW: 22/80/443 open, остальное закрыто
- ✅ fail2ban на SSH (5 попыток за 10 мин = бан 1 час)
- ✅ systemd unit `ai-che.service` (НЕ запущен пока — ждёт .env)
- ✅ nginx config заглушка для `aiche.ru`

После завершения сервис **не запускается** автоматически — ждёт перенос данных и `.env`.

---

### Этап 3. Перенос данных со старого сервера

#### 3.1. Экспорт со старого

```bash
ssh root@194.104.9.219

# Запуск скрипта экспорта (останавливает ai-che на ~10 сек,
# потом возобновляет — для консистентности БД)
cd /root/AI-CHE && bash scripts/migrate_export.sh

# Скрипт скажет путь к архиву, например:
#   /tmp/aiche-migrate-20260505-091200.tar.gz  (~10-100 МБ)
```

#### 3.2. Передача файла на новый сервер

С вашей локальной машины (через ssh туннель, чтобы не настраивать прямой ssh между серверами):

```bash
# Скачиваем архив со старого сервера
scp root@194.104.9.219:/tmp/aiche-migrate-20260505-091200.tar.gz ./

# Закидываем на новый
scp aiche-migrate-20260505-091200.tar.gz root@193.187.92.147:/root/

# Удаляем локальную копию (содержит .env с секретами!)
rm aiche-migrate-20260505-091200.tar.gz
```

ИЛИ напрямую (если ssh-ключ от старого добавлен на новый):

```bash
ssh root@194.104.9.219 "scp /tmp/aiche-migrate-*.tar.gz root@193.187.92.147:/root/"
```

#### 3.3. Импорт на новый

```bash
ssh root@193.187.92.147

cd /root/AI-CHE
bash scripts/migrate_import.sh /root/aiche-migrate-20260505-091200.tar.gz
```

Что проверится:
- ✅ chat.db integrity (`PRAGMA integrity_check`)
- ✅ Количество юзеров и ботов
- ✅ Применяются `lightweight_migrations`
- ✅ Права на `.env` (600), `.backup_encryption_key` (400), `.vapid_private.pem` (400)

---

### Этап 4. Настройка .env под новый сервер

```bash
ssh root@193.187.92.147
cd /root/AI-CHE
nano .env
```

Что **обязательно** обновить:

```bash
# Локация и URL
APP_URL=https://aiche.ru
APP_ENV=production
DEV_MODE=false

# AI-прокси (URL с этап 1.2)
AI_HTTPS_PROXY=http://aiche:<PASSWORD>@<HETZNER_IP>:3128

# (Опционально) можно задать прокси отдельно для каждого:
# OPENAI_HTTPS_PROXY=...
# ANTHROPIC_HTTPS_PROXY=...
# GOOGLE_HTTPS_PROXY=... (уже было)
# XAI_HTTPS_PROXY=...
# PERPLEXITY_HTTPS_PROXY=...

# JWT_SECRET — оставить тот же! Иначе все юзеры разлогинятся,
# секреты в EncryptedString не расшифруются.

# YOOKASSA_SHOP_ID / YOOKASSA_SECRET_KEY — те же

# SMTP (если ещё не настроен — настройте сейчас)
SMTP_HOST=smtp.unisender.com
SMTP_PORT=587
SMTP_USER=<логин>
SMTP_PASS=<пароль>
SMTP_FROM=AI Студия Че <noreply@aiche.ru>
```

---

### Этап 5. Запуск

```bash
ssh root@193.187.92.147

# Запускаем сервис
systemctl start ai-che
systemctl status ai-che   # должен быть active (running)

# Логи
journalctl -u ai-che -f

# Проверка работоспособности
curl http://127.0.0.1:8000/healthz
# {"status":"ok"}
```

**Тест AI-вызова через прокси:**
```bash
# Через cookies можно проверить /me, но проще:
journalctl -u ai-che | grep -E "openai|anthropic|google" | tail -5
# Если есть ошибки connection refused — прокси не настроен
```

---

### Этап 6. SSL и DNS

#### 6.1. Получение SSL-сертификата

```bash
ssh root@193.187.92.147

# Сначала временно поправьте /etc/hosts чтобы certbot подтвердил
# (потому что DNS ещё указывает на старый сервер)
echo "127.0.0.1 aiche.ru www.aiche.ru" >> /etc/hosts

# Получите сертификат (НЕ через nginx-плагин — он попытается достучаться до 80)
certbot certonly --standalone -d aiche.ru -d www.aiche.ru \
    --email your@email.com --agree-tos --no-eff-email

# Уберите временный hosts
sed -i '/aiche.ru/d' /etc/hosts
```

#### 6.2. Подключение SSL к nginx

```bash
nano /etc/nginx/sites-available/aiche.ru

# Добавьте listen 443 ssl блок и редирект с 80:
```

(см. финальный конфиг в `/root/AI-CHE/deploy/nginx-ssl.conf` — создадим его)

#### 6.3. Переключение DNS

В панели регистратора `aiche.ru`:

1. Найти A-запись `aiche.ru` → изменить с `194.104.9.219` на `193.187.92.147`
2. То же для `www.aiche.ru`
3. **TTL установить 60-300 секунд** перед изменением (чтобы быстро откатиться если что)

После сохранения — DNS распространится за **5-30 минут**. Проверка:
```bash
dig aiche.ru +short
# должно вернуть 193.187.92.147
```

---

### Этап 7. Проверка работы

После переключения DNS:

1. **Frontend:**
   - Откройте `https://aiche.ru` в режиме incognito
   - Залогиньтесь существующим аккаунтом (юзеры из старой БД)
   - Проверьте баланс — должен совпадать
   - Создайте тестовое сообщение в чате — AI должен ответить (значит прокси работает)

2. **Боты:**
   - Откройте `/chatbots.html`
   - Все боты на месте?
   - Webhook'и TG/VK/MAX могли отвалиться — нажмите **«Обновить»** в каждой карточке (переустановит webhook на новый IP)

3. **Платежи:**
   - Зайдите в кабинет → Токены → Пополнить 100 ₽
   - Тестовая ЮKassa-транзакция должна пройти

4. **Логи:**
   ```bash
   journalctl -u ai-che --since '10 minutes ago' | grep -iE 'error|traceback'
   # Должно быть пусто
   ```

---

### Этап 8. Финальная очистка

После 24-48 часов работы на новом сервере:

```bash
# На СТАРОМ сервере
ssh root@194.104.9.219

# Останавливаем сервис (но не удаляем — на случай отката)
systemctl stop ai-che
systemctl disable ai-che

# Можно держать VM выключенной 30 дней как backup
# Потом удалить через панель Clouvider
```

---

## 🛠 Решение проблем

### Сервис не запускается

```bash
journalctl -u ai-che -n 50 --no-pager
# Ищите "ERROR" / "ImportError" / "AttributeError"

# Частые проблемы:
# 1. JWT_SECRET в .env не совпадает со старым → сменить
# 2. Нет .vapid_private.pem → "файл не найден" — отключить push в env
# 3. Не применились миграции → запустить вручную:
cd /root/AI-CHE
DEV_MODE=false venv/bin/python -c "
import sys; sys.path.insert(0,'.')
from server.db import Base, engine, apply_lightweight_migrations
from server import models
Base.metadata.create_all(bind=engine)
apply_lightweight_migrations()
print('OK')
"
```

### AI-вызовы возвращают ошибки

```bash
# Тест прокси
curl -x "$AI_HTTPS_PROXY" https://api.openai.com/v1/models -H "Authorization: Bearer <KEY>"

# Если 'Connection refused' — прокси не работает или firewall блокирует
# Проверьте на Hetzner:
ssh root@<HETZNER_IP>
systemctl status squid
journalctl -u squid --no-pager -n 20
```

### Webhook'и Telegram не работают

Telegram запоминает старый webhook URL. После миграции:
1. Откройте `/chatbots.html`
2. Каждый бот → нажмите **«Обновить»** (это запустит pause+deploy → переустановит webhook)
3. Или вручную: `curl https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://aiche.ru/webhook/<bot_id>`

### Откат миграции

Если что-то пошло не так:

1. **DNS назад:** в панели регистратора `aiche.ru` → A-запись на `194.104.9.219`
2. **Старый сервер запустить:** `ssh root@194.104.9.219 && systemctl start ai-che`
3. **DNS распространится** через 5-30 минут (если TTL=60-300 сек)

---

## 📊 Чек-лист перед запуском в прод

- [ ] EU-прокси работает (тест curl через прокси)
- [ ] SSL-сертификат получен и nginx настроен
- [ ] systemctl status ai-che → active (running)
- [ ] curl https://aiche.ru/healthz → 200
- [ ] Старая учётка успешно входит на новом сервере
- [ ] Баланс юзеров совпадает (тест: ваш аккаунт)
- [ ] Чат с GPT работает (тест прокси на live-нагрузке)
- [ ] DNS обновлён (`dig aiche.ru +short` → новый IP)
- [ ] Бэкап БД настроен на Yandex Object Storage (см. ниже)

---

## ☁️ Дополнительно: бэкапы в Yandex Object Storage

**Для compliance** ст. 18 152-ФЗ — бэкапы ПДн должны быть в РФ.

```bash
# Регистрация в Yandex Cloud → создать бакет S3
# Получить static access keys в IAM

# Установить AWS CLI
apt install -y awscli
aws configure
# Endpoint: https://storage.yandexcloud.net
# Region: ru-central1
# Access key + secret из Yandex Cloud

# Скрипт бэкапа в /etc/cron.daily/aiche-yandex-backup:
cat > /etc/cron.daily/aiche-yandex-backup <<'EOF'
#!/bin/bash
DATE=$(date +%Y-%m-%d)
LATEST=$(ls -1t /root/AI-CHE/backups/chat.db.*.enc | head -1)
if [ -n "$LATEST" ]; then
    aws s3 cp "$LATEST" "s3://aiche-backups/db/$DATE.enc" \
        --endpoint-url https://storage.yandexcloud.net
fi
EOF
chmod +x /etc/cron.daily/aiche-yandex-backup
```

Стоимость: ~30 ₽/мес за 100 ГБ.

---

## 📝 После миграции — обновите HANDOVER.md

Замените:
- `194.104.9.219` → `193.187.92.147` (везде)
- Локацию: `Дронтен, NL, Clouvider` → `Москва, RU, HOSTKEY`
- Прокси: добавить упоминание `AI_HTTPS_PROXY`

---

*Гайд: версия 1.0, дата 2026-05-04. Скрипты: scripts/migrate_*.sh*
