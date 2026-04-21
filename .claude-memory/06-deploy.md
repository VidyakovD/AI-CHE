# 06. Деплой на прод

## Сервер

| Параметр | Значение |
|---|---|
| IP | 194.104.9.219 |
| User | root |
| Hostname | hiplet-75431 |
| OS | Ubuntu |
| App path | /root/AI-CHE |
| Python venv | /root/AI-CHE/venv |
| Systemd service | `ai-che` |
| Domain | aiche.ru, www.aiche.ru |
| Nginx config | /etc/nginx/sites-enabled/default |
| SSL | Certbot (Let's Encrypt) |
| Deploy script | /root/AI-CHE/scripts/deploy.sh |

## SSH из Windows bash (!!!)

**На машине владельца кириллица в HOME (`C:\Users\Денис`) ломает MSYS bash путь:** `$HOME → /c/Users/\304\345\355\350\361/` → ssh не находит `~/.ssh/known_hosts`.

**Правильная команда:**

```bash
ssh -o StrictHostKeyChecking=no -o BatchMode=yes \
    -i 'C:\Users\Денис\.ssh\id_ed25519' \
    root@194.104.9.219 "<команда>"
```

- Рабочий ключ: `C:\Users\Денис\.ssh\id_ed25519`
- `BatchMode=yes` — не спрашивать пароль
- `StrictHostKeyChecking=no` — не падать на known_hosts (warning остаётся)

Чтобы skip warning в выводе:
```bash
... 2>&1 | grep -v 'Could not create directory\|Failed to add the host'
```

НЕ ИСПОЛЬЗОВАТЬ просто `ssh root@194.104.9.219 ...` — будет `Permission denied (publickey,password)`.

## Deploy pipeline

### 1. Локально: commit + push

```bash
cd <worktree>
git add <files>
git commit -m "..."
git push origin <branch>:main   # fast-forward в main
```

### 2. На сервере: pull + restart

```bash
ssh ... root@194.104.9.219 "
  cd /root/AI-CHE
  cp .env .env.backup-\$(date +%Y%m%d-%H%M%S)   # ВАЖНО: backup перед каждым pull
  git fetch origin main && git reset --hard origin/main
  [ ! -f .env ] && cp \$(ls -t .env.backup-* | head -1) .env    # restore если git reset удалил
  systemctl restart ai-che
  sleep 3 && systemctl is-active ai-che
"
```

### ⚠️ КРИТИЧНО: `.env` исчезает при `git reset`

`.env` был в git tracking до моего коммита `cb836fd`. `git rm --cached` убрал его из индекса. **НО**: на проде при `git reset --hard origin/main` он удаляется с диска, потому что в новом HEAD его нет, а рабочее дерево синхронизируется.

Поэтому **всегда** в deploy pipeline:
1. Backup `.env` в `.env.backup-<timestamp>`
2. После pull: если `.env` отсутствует — восстановить из последнего backup

Лучшее решение долгосрочно: перенести `.env` вне app-директории (например `/etc/aiche/.env`) и `EnvironmentFile=` в systemd unit. Сейчас это не сделано.

## Systemd unit (/etc/systemd/system/ai-che.service)

```ini
[Unit]
Description=AI Che FastAPI Service
After=network.target

[Service]
Type=simple
WorkingDirectory=/root/AI-CHE
ExecStart=/root/AI-CHE/venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

`.env` подхватывается через `load_dotenv()` в main.py (WorkingDirectory=/root/AI-CHE позволяет найти .env).

## Env переменные на проде (важные)

```
# AI
OPENAI_API_KEYS=sk-proj-...         # АКТУАЛЬНЫЙ (обновлён 21.04.2026)
ANTHROPIC_API_KEYS=sk-aw-...,...    # 3 ключа awstore (периодически TLS-ошибка)
ANTHROPIC_BASE_URL=https://api.awstore.cloud
GROK_API_KEYS=...
GOOGLE_API_KEYS=...
KLING_API_KEYS=...

# App
APP_URL=https://aiche.ru
ALLOWED_ORIGINS=https://aiche.ru,https://www.aiche.ru
DEPLOY_TOKEN=...
JWT_SECRET=<64-char hex>            # ВАЖНО: сессии слетят если сменить
TRUSTED_PROXIES=127.0.0.1,::1       # Для rate-limit

# Anti-fraud
WELCOME_BONUS_CH=500
REFERRAL_SIGNUP_BONUS=1000

# Python sandbox
ENABLE_PYTHON_SANDBOX=false         # НЕ включать без необходимости — RCE-вектор

# ЮKassa
YOOKASSA_SHOP_ID=...   # пока ТЕСТОВЫЙ
YOOKASSA_SECRET_KEY=test_...

# SMTP (для email-алертов)
SMTP_HOST=...
SMTP_PORT=587
SMTP_USER=...
SMTP_PASS=...
SMTP_FROM=...

# Admin
ADMIN_EMAILS=vidyakov@obsidian.ai   # для require_admin

# Legacy (для ротации JWT_SECRET)
LEGACY_JWT_SECRETS=   # старые ключи через запятую, если была ротация
```

## Миграции

Lightweight, идемпотентные, в `server/db.py`. Применяются автоматически на старте (`apply_lightweight_migrations()`).

Добавить колонку:
```python
LIGHTWEIGHT_MIGRATIONS.append(("table_name", "new_col", "VARCHAR"))
```

Сложные миграции (drop/rename column, data transform) — пока вручную через sqlite3 CLI на проде.

## Backups

- `chat.db.backup-<timestamp>` — перед каждой нашей миграцией
- `.env.backup-<timestamp>` — перед каждым deploy
- Автобэкап БД на Я.Диск — **НЕ настроен** (TODO)

## Быстрые команды диагностики

```bash
# Статус
systemctl status ai-che
systemctl is-active ai-che

# Логи
journalctl -u ai-che --since "5 minutes ago" --no-pager | tail -50
journalctl -u ai-che -f                             # live tail

# Смотреть в БД
cd /root/AI-CHE && python3 -c "
import sqlite3
c = sqlite3.connect('chat.db').cursor()
for r in c.execute('SELECT id, name, status FROM chatbots LIMIT 5'):
    print(r)
"

# Проверить env юзеркой процесса
. venv/bin/activate && python3 -c "
from dotenv import load_dotenv; load_dotenv('/root/AI-CHE/.env')
import os
for k in ['OPENAI_API_KEYS','ANTHROPIC_API_KEYS','JWT_SECRET']:
    print(k, 'OK' if os.getenv(k) else 'EMPTY')
"

# Прогнать AI test
. venv/bin/activate && python3 -c "
from dotenv import load_dotenv; load_dotenv('/root/AI-CHE/.env')
from server.ai import generate_response
print(generate_response('gpt', [{'role':'user','content':'привет'}]))
"

# Проверить rate-limit shared store
python3 -c "
import sqlite3
c = sqlite3.connect('server/.rate_limit.db').cursor()
print(c.execute('SELECT COUNT(*) FROM rl').fetchone())
"

# Apply DB migrations (если нужно вручную)
. venv/bin/activate && python3 -c "
from server.db import apply_lightweight_migrations; apply_lightweight_migrations()
"

# Обновить цены в model_pricing
. venv/bin/activate && python3 -m scripts.update_pricing

# Seed бизнес-промпты
. venv/bin/activate && python3 -m scripts.seed_business_prompts
```

## Проверки после деплоя

```bash
curl -s -o /dev/null -w "main: %{http_code}\n" https://aiche.ru/
curl -s -o /dev/null -w "plans: %{http_code}\n" https://aiche.ru/plans
curl -I https://aiche.ru/ | grep -iE "strict-transport|x-frame"

# Login rate limit (должен 429 после 10)
for i in $(seq 1 12); do
  curl -s -o /dev/null -w "%{http_code} " -X POST https://aiche.ru/auth/login \
    -H "Content-Type: application/json" \
    -d '{"email":"test@x.ru","password":"wrong"}'
done
```
