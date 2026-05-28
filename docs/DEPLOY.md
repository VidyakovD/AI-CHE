# DEPLOY — gunicorn + правила миграций

Прод-сервер `193.187.92.147`, systemd unit `ai-che.service`. После перехода на
**gunicorn + uvicorn workers** деплой выполняется через `systemctl reload`
вместо `restart`. Reload даёт **~5-9 секунд** окна 502 (новые workers cold-start
~9 сек: импорт 26 модулей + scheduler init); restart — 10-20 сек жёсткого
downtime. Истинный zero-downtime требует blue/green с двумя инстанциями за
nginx upstream — отложено до 100+ платящих юзеров.

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

**`systemctl reload`** вместо `restart`:
- `restart` = жёсткий kill всех 4 worker'ов + полный старт (10-20 сек downtime)
- `reload` = SIGHUP → gunicorn graceful workers swap (~5-9 сек короткое окно
  502 пока новые workers импортируются). Старые workers получают TERM и
  дорабатывают активные запросы (graceful_timeout=60s), новые fork'аются
  от master без preload и cold-start'ятся.

Если деплоишь срочный security-фикс — лучше reload.
Если требуется истинный zero-downtime для платящих юзеров — blue/green
(см. секцию ниже).

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

## Будущее: blue/green для true zero-downtime (TODO post-100-юзеров)

Когда появятся платящие юзеры и 5-сек dip станет проблемой — мигрировать на
два инстанса за nginx upstream:

```nginx
upstream ai_che_app {
    server 127.0.0.1:8000;
    server 127.0.0.1:8001 backup;
}
```

Два systemd unit'а:
- `ai-che.service` → port 8000
- `ai-che-blue.service` → port 8001

`deploy.sh`:
```bash
# 1. Подтянуть код
git pull origin main && pip install -r requirements.txt --upgrade

# 2. Перезапустить blue (8001) — nginx переключает на 8000
systemctl restart ai-che-blue
sleep 10  # warm-up

# 3. Smoke-тест blue
curl -s http://127.0.0.1:8001/ > /dev/null || exit 1

# 4. nginx upstream swap (без даунтайма)
# (либо вручную nginx reload с актуальным upstream, либо weight=...)

# 5. Перезапустить green (8000) — uplink идёт через blue
systemctl restart ai-che
sleep 10

# 6. Возврат к default upstream order
```

С blue/green истинный zero-downtime достигается. Это **отдельный спринт**,
~1 час работы. Сейчас 4 юзера на проде — текущей gunicorn-схемы хватает.

## Прод-сервер: общая информация

- **IP**: `193.187.92.147` (Москва, HOSTKEY)
- **OS**: Ubuntu 22.04
- **Python**: 3.10 (в venv `/root/AI-CHE/venv/`)
- **БД**: PostgreSQL local на этом же сервере
- **Web server**: nginx (TLS termination + reverse proxy на 127.0.0.1:8000)
- **Systemd**: `ai-che.service` (unit в `deploy/ai-che.service`)
- **Логи**: `journalctl -u ai-che -f` (live tail) или `--since "1 hour ago"`
- **Backup envов**: `.env.before-pg-20260505`, `.env.backup-yookassa-...` и т.д.
