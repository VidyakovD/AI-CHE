# Alembic migrations

## Зачем рядом с LIGHTWEIGHT_MIGRATIONS?

`server/db.py:LIGHTWEIGHT_MIGRATIONS` — простой список `(table, col, sql_type)`,
работает только для добавления колонок. Хорош для маленьких изменений, но:
- не умеет DROP COLUMN
- не умеет DATA migrations (переименование, нормализация)
- не версионируется (нет audit-log что когда применилось)
- одно событие на старте → откатить нельзя

Alembic решает всё это, плюс автогенерация миграции из diff ORM↔БД.
Подключён как **параллельная** система: старые LIGHTWEIGHT_* продолжают
работать, новые сложные изменения — через Alembic.

## Quick reference

```bash
# Применить все pending миграции
alembic upgrade head

# Сгенерить миграцию из diff ORM ↔ текущая БД
alembic revision --autogenerate -m "add user.notes column"

# Применить только следующую
alembic upgrade +1

# Откатить последнюю
alembic downgrade -1

# Текущая ревизия (что применено)
alembic current

# История
alembic history --verbose

# Сгенерить SQL без применения (для review перед prod-deploy)
alembic upgrade head --sql > pending.sql
```

## Baseline миграция

Первая миграция (`*_baseline.py`) маркирует текущее состояние схемы как
точку отсчёта. На уже задеплоенной БД (где LIGHTWEIGHT_* отработали):

```bash
alembic stamp head   # пометить текущую БД как up-to-date, без применения
```

На свежей БД:

```bash
alembic upgrade head  # применит baseline и все последующие
```

## Workflow для новых изменений

1. Изменить ORM-модель в `server/models.py` (добавить колонку, индекс, FK...)
2. `alembic revision --autogenerate -m "short_slug"` — создаст файл
   `alembic/versions/YYYYMMDD_HHMM_short_slug.py`
3. Review сгенерированный файл — autogenerate не идеален:
   - проверь что DROP COLUMN это намеренно
   - для DATA migrations добавь `op.execute("UPDATE ...")`
4. На staging: `alembic upgrade head`
5. Коммит → CI прогонит миграцию на чистой БД
6. Prod-deploy → `alembic upgrade head` после `git pull`

## Не использовать Alembic для:

- **pricing_config** изменения цен — это runtime-данные, через `/admin/pricing`
- **новые orchestra/solution seed** — это data, через `scripts/seed_*.py`
- **временные dev-эксперименты** — оставь в LIGHTWEIGHT_MIGRATIONS или забудь
