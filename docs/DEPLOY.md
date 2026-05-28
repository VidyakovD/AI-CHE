# DEPLOY — zero-downtime rolling reload

Прод-сервер `193.187.92.147`, systemd unit `ai-che.service`. После перехода на
**gunicorn + uvicorn workers** деплой выполняется без 502/connection refused —
gunicorn по SIGHUP перезапускает воркеры по одному, старые дорабатывают
активные запросы, nginx продолжает отвечать.

## Однократная миграция с uvicorn на gunicorn

Делается ОДИН РАЗ при первом обновлении после внедрения `gunicorn.conf.py`.

```bash
# 1. На локальной машине: запушить ветку с этим патчем
git push origin <branch>:main

# 2. SSH на прод
HOME="C:\\Users\\Денис" ssh -i 'C:\\Users\\Денис\\.ssh\\id_ed25519' \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  root@193.187.92.147

# 3. На проде:
cd /root/AI-CHE
git pull origin main

# 4. Поставить gunicorn (он уже в requirements.txt)
./venv/bin/pip install -r requirements.txt --upgrade --quiet

# 5. Заменить systemd unit
cp deploy/ai-che.service /etc/systemd/system/ai-che.service
systemctl daemon-reload

# 6. Полный restart один раз (uvicorn → gunicorn — это смена ExecStart)
systemctl restart ai-che

# 7. Проверка
sleep 4
systemctl is-active ai-che              # → active
journalctl -u ai-che -n 30 --no-pager   # → должно быть "Booting worker with pid:" × 4
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/   # → 200
```

После этого **все следующие деплои — без downtime**.

## Обычный деплой (после миграции)

```bash
# На локали
git push origin <branch>:main

# На проде
HOME="C:\\Users\\Денис" ssh ... root@193.187.92.147 "
  cd /root/AI-CHE &&
  git pull origin main &&
  ./venv/bin/pip install -r requirements.txt --upgrade --quiet &&
  systemctl reload ai-che &&
  sleep 4 &&
  systemctl is-active ai-che
"
```

**`systemctl reload`** вместо `restart` — это ключ:
- `restart` = kill всех + старт новых (5-15 секунд downtime)
- `reload` = SIGHUP → gunicorn rolling restart (0 downtime)

## Что zero-downtime НЕ покрывает

`systemctl reload` хорош для изменений в коде Python / HTML / JS. Эти случаи
требуют полного `systemctl restart` с коротким downtime:

| Изменение | Почему restart нужен |
|---|---|
| `requirements.txt` (новый пакет) | gunicorn master не подхватывает новые libs |
| `gunicorn.conf.py` | gunicorn читает только при старте |
| Системный env (`.env`) | EnvironmentFile читается только systemd при старте |
| Изменения в `main.py` lifespan / startup events | Workers подхватят, но если master — нужен restart |
| Smoke-тесты с реальными API-ключами после ротации | Кэшированные клиенты могут держать старые |

Для всего остального — **`reload`**.

## Миграции БД

ВАЖНО: при rolling reload **новый код работает параллельно со старым ~60 секунд**
(graceful_timeout). Это значит миграции должны быть обратно-совместимыми
во время этого окна.

### ✅ Безопасные операции (можно применять напрямую)

| Операция | Почему OK |
|---|---|
| `ADD COLUMN ... NULL DEFAULT NULL` | Старый код игнорирует, новый — использует |
| `CREATE TABLE` | Старый код не знает о таблице |
| `CREATE INDEX CONCURRENTLY` | PG не блокирует таблицу |
| `ADD INDEX` (новый, не используется в старом коде) | Только метаданные |

### ⚠ Условно безопасные

| Операция | Условие |
|---|---|
| `ADD COLUMN ... NOT NULL DEFAULT <value>` | Только на маленьких таблицах (<100k строк), иначе lock |
| `ALTER COLUMN TYPE` | Только если новый тип совместим (int → bigint OK, varchar → int NOT) |

### ❌ Опасные операции — 2-step deploy

| Операция | Шаги |
|---|---|
| `DROP COLUMN x` | (1) Деплой: новый код больше не читает/пишет `x`. (2) После убедившись что трафик старого кода ушёл — миграция дропает |
| `RENAME COLUMN` | (1) Деплой добавляет НОВУЮ колонку, dual-write в обе. (2) Backfill. (3) Деплой переключает чтение. (4) Дроп старой |
| `ALTER COLUMN ... NOT NULL` (на колонке с NULL'ами) | (1) Backfill NULL → default. (2) Деплой обеспечивает always-non-null write. (3) Миграция SET NOT NULL |

`LIGHTWEIGHT_MIGRATIONS` в `server/db.py` запускается при старте каждого
worker'а. Для безопасных ADD COLUMN — просто добавь tuple, миграция применится
при следующем `reload`. Для опасных — alembic + 2-step plan.

## Откат

Если новый код упал на rolling reload:
```bash
git revert HEAD                # на локали, push
# На проде:
systemctl reload ai-che        # workers переключатся на код ДО revert
```

Если совсем плохо (master процесс упал):
```bash
systemctl restart ai-che       # короткий downtime, но точно восстановит
```

## Мониторинг во время деплоя

```bash
# В отдельной SSH-сессии перед reload:
watch -n 1 'curl -s -o /dev/null -w "HTTP %{http_code} · %{time_total}s\n" http://127.0.0.1:8000/'
```

Если видишь HTTP 502/504 во время `reload` — graceful_timeout слишком короткий.
Проверь `gunicorn.conf.py`.

## Прод-сервер: общая информация

- **IP**: `193.187.92.147` (Москва, HOSTKEY)
- **OS**: Ubuntu 22.04
- **Python**: 3.10 (в venv `/root/AI-CHE/venv/`)
- **БД**: PostgreSQL local на этом же сервере
- **Web server**: nginx (TLS termination + reverse proxy на 127.0.0.1:8000)
- **Systemd**: `ai-che.service` (unit в `deploy/ai-che.service`)
- **Логи**: `journalctl -u ai-che -f` (live tail) или `--since "1 hour ago"`
- **Backup envов**: `.env.before-pg-20260505`, `.env.backup-yookassa-...` и т.д.
