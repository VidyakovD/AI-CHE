# Модуль 20 — Infrastructure & Deploy

> **Что это:** db.py (SQLAlchemy + миграции), scheduler.py (cron-воркеры), alembic, deploy-скрипты, бэкапы. Open когда: добавляешь cron, миграцию, дебажишь сервис, переезжаешь, ротируешь секреты.

## TL;DR

- **DB:** [server/db.py](server/db.py) — Base, SessionLocal, pool, **LIGHTWEIGHT_MIGRATIONS** (parallel со стандартным Alembic).
- **Scheduler:** [server/scheduler.py](server/scheduler.py) (1221 строк) — все cron'ы в одном файле.
- **Alembic:** `alembic/` — baseline + версии (CI прогоняет на чистой SQLite).
- **Deploy:** ручной push + ssh + systemctl restart (см. ниже).
- **Backups:** AES-GCM ежедневно + Yandex Object Storage upload.

## Подключение к БД

| Env | Прод | Dev |
|---|---|---|
| `DATABASE_URL` | `postgresql://aiche:...@localhost:5432/aiche` | `sqlite:///./chat.db` или umolchanie |

⚠ **Postgres case-sensitive** на email — `vidyakovd@gmail.com` (lowercase обязателен!).

## LIGHTWEIGHT_MIGRATIONS (parallel Alembic)

В [server/db.py](server/db.py) есть массив добавления колонок при старте:

```python
LIGHTWEIGHT_MIGRATIONS = [
    ("solutions", "input_schema_json", "TEXT"),
    ("users", "totp_secret", "VARCHAR(255)"),
    # ...
]
```

Применяется через `_existing_columns()` — совместим с обоими backend'ами (Postgres + SQLite). **Для целиком новых таблиц** достаточно `Base.metadata.create_all` — race-safe (`963c365`).

**Alembic** (`930cb85`) — baseline настроен, новые миграции писать через него для production-grade схемы. CI прогоняет `LIGHTWEIGHT_MIGRATIONS + alembic upgrade head` на чистой SQLite (`b7e2ff2`).

## Scheduler — cron'ы

| Job | Частота | Что делает |
|---|---|---|
| `scheduler/apikey` | раз в сутки | rotate `api_keys.last_check_at` |
| `scheduler/pdf` | по запросу | offline PDF-генерация для больших КП |
| `scheduler/db_backup` | раз в сутки | pg_dump → AES-GCM → Yandex Object Storage |
| `scheduler/conv` | раз в час | cleanup истёкших `bot_conversations` |
| `scheduler/audit` | раз в сутки | retention `action_logs` |
| `scheduler/storage-billing` | раз в сутки | `(StoredAsset + KnowledgeFile)` биллинг |
| `scheduler/orchestra_schedules` | раз в минуту | проверка `next_run_at <= now` → запуск orchestra |
| `scheduler/idempotency_cleanup` | раз в сутки | удаление `IdempotencyRecord > 24h` |
| `scheduler/data_retention` | раз в сутки | анонимизация User'ов + purge КП (если `DATA_RETENTION_DRY_RUN!=true`) |
| `scheduler/broken_alerts` | раз в час | если %failed-генераций сайта > 30% → email админу |

⚠ **`_last_alerted_broken_ids` hydrate из БД на старте** (`ef4ecb6`) — чтобы перезагрузка не задвоила алерт.

## Deploy workflow

```bash
git push origin claude/<branch>:main

HOME="C:\\Users\\Денис" ssh -i "C:\\Users\\Денис\\.ssh\\id_ed25519" \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  root@193.187.92.147 \
  "cd /root/AI-CHE && git pull origin main && \
   systemctl restart ai-che && systemctl is-active ai-che"
```

**Сиды решений (после изменений в `scripts/seed_*`):**
```bash
ssh ... "cd /root/AI-CHE && /root/AI-CHE/venv/bin/python scripts/seed_v2_solutions.py"
ssh ... "cd /root/AI-CHE && /root/AI-CHE/venv/bin/python scripts/seed_perplexity_solutions.py [--update]"
ssh ... "cd /root/AI-CHE && /root/AI-CHE/venv/bin/python scripts/upgrade_orchestra_perplexity.py [--force]"
```

## Аварийный план — если AI-прокси упал

Симптом: все AI-вызовы (OpenAI/Anthropic/Google/Grok) фейлятся, Perplexity работает.

1. Проверь `systemctl status xray` на проде.
2. Конфиг `/usr/local/etc/xray/config.json` — VLESS Reality на `31.169.126.79:443`.
3. Если внешний прокси-сервер недоступен — найди новый и обнови `outbounds[0].settings.vnext[0].address`.
4. `systemctl restart xray`.
5. Сервис `ai-che` рестартить не нужно — http_client просто подхватит.

## Бэкапы

- `pg_dump --format=custom` ежедневно → AES-GCM → `/root/AI-CHE/backups/chat.db.YYYY-MM-DD.enc`
- Retention 14 дней локально
- Загрузка в Yandex Object Storage (`f87641d`)
- Restore: `scripts/restore_backup.py` (локально) или `scripts/restore_from_yc.py`

## Шифрование секретов

`EncryptedString` через HKDF от `JWT_SECRET`. При ротации JWT_SECRET → вызвать `/admin/reencrypt-secrets` со старым+новым в env одновременно. Иначе **потеря данных**.

## Перенос между серверами

Toolkit:
- `scripts/migrate_export.sh` — экспорт со старого (chat.db + uploads + .env + ключи → tar.gz)
- `scripts/migrate_setup.sh` — первичная настройка нового
- `scripts/migrate_import.sh` — распаковка с rollback-бэкапом

Полный гайд — [MIGRATION.md](MIGRATION.md).

## Гочча

- **Worker_lock** ([server/worker_lock.py](server/worker_lock.py)) — координация cron'ов между 4 workers (только один воркер выполняет cron).
- **`asyncio.create_task` без сохранения ссылки** → GC может убить → паттерн `_pending_tasks` (`36c6af7`).
- **scheduler.py 1221 строк** — кандидат на split в `server/cron/<name>.py` (см. фидбек в HANDOVER.md).

## Тесты

- `tests/test_smoke_builders.py::TestScheduler` — smoke
- CI: pytest + ruff + pip-audit + alembic upgrade head

## Зависимости

- Все модули — этот фундамент
- [18-privacy](18-privacy-compliance.md) — retention cron
- [11-knowledge-rag](11-knowledge-rag.md), [16-storage](16-storage.md) — storage-billing
- [06-solutions](06-solutions.md) — orchestra_schedules
