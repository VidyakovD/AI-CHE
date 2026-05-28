# DEPLOY — true zero-downtime через blue/green

Прод-сервер `193.187.92.147`. Развёрнута **blue/green** схема: два gunicorn-
инстанса за nginx upstream с автоматическим failover. Любое изменение кода
(включая `server/agents/registry.py` — модули агента) деплоится через
`scripts/deploy.sh` **без 502 для конечного юзера**.

**Замерено в проде: 120/120 запросов = 100% OK во время полного deploy
цикла (rolling restart обоих инстансов).**

## Архитектура

```
                                ┌─ 127.0.0.1:8000 (GREEN, primary)
   nginx upstream ai_che_backend ┤
                                └─ 127.0.0.1:8001 (BLUE, backup)
                                              ↑
                          proxy_next_upstream error timeout 502 503 504
                          max_fails=1 fail_timeout=2s
```

- `ai-che.service` → port 8000 (green, primary в upstream)
- `ai-che-blue.service` → port 8001 (blue, backup)
- nginx посылает запросы на 8000; если 8000 не отвечает (HTTP 5xx, refuse,
  timeout) — мгновенно retry на 8001 без 502 на клиента.

## Обычный деплой (рекомендуемый)

```bash
ssh root@aiche.ru "/root/AI-CHE/scripts/deploy.sh"
```

Скрипт:
1. Сбрасывает локальные изменения tracked файлов + pull origin/main
2. `pip install -r requirements.txt --upgrade --quiet`
3. Рестарт BLUE (8001) → smoke. GREEN продолжает обслуживать.
4. Прогрев BLUE через nginx — чтобы failover был мгновенным.
5. Рестарт GREEN (8000) → nginx failover на BLUE → smoke.
6. Финальная smoke публичного URL через nginx.

Запуск занимает ~30 секунд. Все запросы юзеров остаются HTTP 200.

При сбое: GREEN не поднялся → BLUE остаётся primary через nginx failover.
Сервис продолжает работать на старом коде blue (актуальный, мы уже его
успешно рестартовали). Разбирайся, потом снова запусти `deploy.sh`.

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

## Что blue/green покрывает

| Что меняется | Downtime через deploy.sh |
|---|---|
| Python код (.py) — server/, main.py | 0 |
| HTML/CSS/JS — views/ | 0 |
| **`server/agents/registry.py` (модули агента)** | **0** |
| Безопасные миграции БД (ADD COLUMN nullable) | 0 |
| Новый пакет в `requirements.txt` | 0 |
| Изменения в `.env` | 0 (через `Environment=` или подмены) |
| `gunicorn.conf.py` | 0 (оба инстанса берут новый при restart) |
| `deploy/ai-che.service` (systemd unit) | 0 (deploy.sh обновляет автоматом) |
| Опасные миграции (DROP/RENAME) | 2-step deploy (см. ниже) |

## Прод-сервер: общая информация

- **IP**: `193.187.92.147` (Москва, HOSTKEY)
- **OS**: Ubuntu 22.04
- **Python**: 3.10 (в venv `/root/AI-CHE/venv/`)
- **БД**: PostgreSQL local на этом же сервере
- **Web server**: nginx (TLS termination + reverse proxy на 127.0.0.1:8000)
- **Systemd**: `ai-che.service` (unit в `deploy/ai-che.service`)
- **Логи**: `journalctl -u ai-che -f` (live tail) или `--since "1 hour ago"`
- **Backup envов**: `.env.before-pg-20260505`, `.env.backup-yookassa-...` и т.д.
