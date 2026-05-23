# Миграция SQLite → PostgreSQL

Когда нужно: при ~100+ активных юзеров SQLite-WAL начинает давать
блокировки на параллельных коммитах (особенно `agent_runner` + `cron` +
запросы юзера одновременно). Postgres решает.

## Архитектура

`server/db.py` уже поддерживает оба бэкенда через `DATABASE_URL`:
```
sqlite:///./chat.db                              ← дефолт (dev)
postgresql+psycopg://user:pass@host:5432/dbname  ← prod
```

Все модели уже совместимы с PG (есть `_normalize_sql_type` в `db.py` для
автоконверсии `DATETIME` → `TIMESTAMP`).

## Шаги миграции

### 1. Установить psycopg на сервер

```bash
# В requirements.txt уже есть psycopg2-binary==2.9.9 — должен подтянуться.
pip install -r requirements.txt --upgrade
```

### 2. Поднять Postgres

**Managed (рекомендовано)**: Yandex Cloud Managed PG / Selectel / RDS.
Создать БД `aiche` с пользователем `aiche`, выдать пароль.

**Self-hosted** на том же сервере:
```bash
apt install postgresql-16
sudo -u postgres psql -c "CREATE USER aiche WITH PASSWORD '...';"
sudo -u postgres psql -c "CREATE DATABASE aiche OWNER aiche ENCODING 'UTF8';"
```

### 3. Перенести данные

```bash
# На сервере, при остановленном ai-che:
systemctl stop ai-che

# Экспорт схемы и данных из SQLite через pgloader (рекомендовано):
apt install pgloader
pgloader sqlite:///root/AI-CHE/chat.db \
  postgresql://aiche:PASS@localhost/aiche

# Альтернатива — через Python скрипт server/scripts/sqlite_to_pg.py
# (если он есть; иначе pgloader покрывает 99% кейсов).
```

### 4. Обновить env

```bash
# /root/AI-CHE/.env
DATABASE_URL=postgresql+psycopg://aiche:PASS@localhost:5432/aiche
# Опциональные тюнинги пула
DB_POOL_SIZE=10
DB_MAX_OVERFLOW=20
DB_POOL_RECYCLE=3600
```

### 5. Применить миграции на новую БД

```bash
# alembic довезёт схему до head если что-то не дотянуло после pgloader
python -m alembic upgrade head

# Lightweight migrations (новые колонки/индексы) применятся при первом
# старте — это safe-idempotent.
```

### 6. Старт + проверка

```bash
systemctl start ai-che
systemctl status ai-che
journalctl -u ai-che -n 100 --no-pager

# В отдельной сессии проверь SELECT-ы:
psql -U aiche -d aiche -c "SELECT count(*) FROM users;"
psql -U aiche -d aiche -c "SELECT count(*) FROM transactions;"

# Прогон smoke через web:
curl -s https://aiche.ru/health
```

### 7. Бэкап SQLite оставить на 30 дней

```bash
mv /root/AI-CHE/chat.db /root/backups/chat.db.$(date +%Y%m%d)
# Через 30 дней без инцидентов — удалить.
```

## Чего НЕ забыть

- **`LIGHTWEIGHT_INDEXES`** в `db.py` создаёт UNIQUE индексы — на PG
  они уникальны на уровне DDL (в отличие от SQLite где partial-unique
  через `WHERE col IS NOT NULL`). Должны примениться корректно.
- **`text_pattern_ops`** — для LIKE-запросов в PG нужен соответствующий
  индекс. Сейчас в проекте критичных `LIKE` не вижу, но если будет тормозить
  full-text поиск по логам — добавить.
- **Connection pool** — `db.py` уже использует `pool_pre_ping=True` и
  `pool_recycle=3600`. Это хорошо для облачных PG которые рвут idle.
- **Backups через `pg_dump` cron** — добавить в `server/cron/db_backup.py`
  ветку для PG (сейчас только SQLite). Можно копировать на Yandex.Disk
  как и SQLite-бэкап.

## Откат если что-то пошло не так

```bash
systemctl stop ai-che
# Восстановить SQLite-бэкап
cp /root/backups/chat.db.YYYYMMDD /root/AI-CHE/chat.db
# Вернуть DATABASE_URL в .env обратно на sqlite:
sed -i 's|^DATABASE_URL=.*|DATABASE_URL=sqlite:///./chat.db|' /root/AI-CHE/.env
systemctl start ai-che
```

## Локальная разработка с PG

См. `docker-compose.dev.yml` в корне репо:
```bash
docker compose -f docker-compose.dev.yml up -d
export DATABASE_URL="postgresql+psycopg://aiche:dev@localhost:5433/aiche_dev"
python -m alembic upgrade head
python main.py
```
