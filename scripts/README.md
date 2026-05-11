# scripts/

Категоризация — какие скрипты для чего. Файлы НЕ переносим в подкаталоги
(сломает упоминания в CLAUDE.md, MIGRATION.md, TODO_NEXT.md и cron'ах
на проде); просто документируем какой что делает.

## 🌱 Seed-скрипты (заполнение БД)

Идемпотентны, безопасно запускать многократно (есть `--update` где
полезно). Применяются на проде после правок промптов:

| Скрипт | Что делает |
|---|---|
| [`seed_orchestra_solutions.py`](seed_orchestra_solutions.py) | Базовые 8 orchestra-пилотов (web_search/llm/synthesize stages) |
| [`seed_v2_solutions.py`](seed_v2_solutions.py) | 40 пилотов v2 с input_schema + multi-stage |
| [`seed_perplexity_solutions.py`](seed_perplexity_solutions.py) | 5 Perplexity-фикс-цена пилотов (Контрагент / Брифинг / etc.) |
| [`seed_business_prompts.py`](seed_business_prompts.py) | Legacy plain-prompt пилоты |

Команды:
```bash
python scripts/seed_orchestra_solutions.py
python scripts/seed_v2_solutions.py
python scripts/seed_perplexity_solutions.py [--update]
```

## 🔧 Утилиты данных

| Скрипт | Что делает |
|---|---|
| [`categorize_solutions.py`](categorize_solutions.py) | Распределяет Solution по subcategory/tags. После добавления пилотов: `--force` |
| [`cleanup_solutions_metadata.py`](cleanup_solutions_metadata.py) | Чистит legacy `[XXX]` префиксы из description |
| [`upgrade_orchestra_perplexity.py`](upgrade_orchestra_perplexity.py) | Замена `web_search` на `perplexity_research` в 5 orchestra-пилотах |
| [`update_pricing.py`](update_pricing.py) | Bulk обновление pricing_config |
| [`add_tg_settings.py`](add_tg_settings.py) | Legacy для TG-настроек |

## 🗄 Миграции БД

| Скрипт | Что делает | Когда запускать |
|---|---|---|
| [`migrate_db.py`](migrate_db.py) | Универсальная миграция | По запросу |
| [`migrate_ch_to_kopecks.py`](migrate_ch_to_kopecks.py) | ОДНОРАЗОВАЯ: chips → копейки | Сделано на проде ~2026-02 |
| [`migrate_fk_cascade.py`](migrate_fk_cascade.py) | Пересоздать FK с CASCADE (RISKY: stop ai-che!) | По запросу |
| [`migrate_sqlite_to_postgres.py`](migrate_sqlite_to_postgres.py) | Перенос SQLite → Postgres | Сделано 2026-05-05 |

⚠️ Все три `migrate_*` БД-скрипта — используют legacy «один список ALTER».
Для новых изменений предпочитайте [Alembic](../alembic/README.md).

## 🔄 Backup / Restore

| Скрипт | Что делает |
|---|---|
| [`backup-db.sh`](backup-db.sh) | Ручной снапшот БД (используется scheduler автоматически) |
| [`restore_backup.py`](restore_backup.py) | Restore из локального AES-GCM бэкапа |
| [`restore_from_yc.py`](restore_from_yc.py) | Restore из Yandex S3 бэкапа |
| [`download-backup.ps1`](download-backup.ps1) | PowerShell для скачивания на Windows |

## 🚀 Деплой / Инфра

| Скрипт | Что делает |
|---|---|
| [`migrate_export.sh`](migrate_export.sh) | Экспорт со старого сервера (chat.db + uploads + .env) |
| [`migrate_setup.sh`](migrate_setup.sh) | Первичная настройка нового сервера (Ubuntu) |
| [`migrate_import.sh`](migrate_import.sh) | Импорт на новый сервер с rollback-бэкапом |
| [`diag.sh`](diag.sh) | Быстрая диагностика сервиса |
| [`anthropic_proxy.py`](anthropic_proxy.py) | Standalone прокси к Anthropic (legacy, не нужен с Xray) |

## ❌ Удалённые

Эти скрипты были удалены как одноразово-отладочные:
- `check_keys.py`, `check_anthropic_keys.py`, `check_claude_file.py` —
  лезли в legacy SQLite chat.db
- `check_google_keys.py` — содержал хардкоженный API ключ (⚠️ ротировать!)
- `test_pdf.py`, `test_pdf_claude.py`, `test_small_pdf.py`,
  `test_image_claude.py`, `debug_pdf.py` — разовые отладочные тулзы для
  PDF/image-generation, давно отлажено

Для smoke-проверки builder'ов теперь есть [tests/test_smoke_builders.py](../tests/test_smoke_builders.py).
