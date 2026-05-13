# Модуль 16 — Storage (файлы юзеров)

> **Что это:** хранение пользовательских файлов (PDF, картинки, видео, KP-черновики) с биллингом ₽/месяц за объём. Open когда: чинишь upload, дебажишь биллинг storage, добавляешь тип asset'а.

## TL;DR

- **Routes:** [server/routes/assets.py](server/routes/assets.py).
- **Биллинг:** **50 ₽/мес за 100 МБ** (`storage.per_100mb_month=5_000`) — общий с [11-knowledge-rag](11-knowledge-rag.md).
- **Хранение:** в `/uploads/` (корень проекта), доступ через `public_token`.
- **Cron-биллинг:** scheduler.py раз в день считает суммарный размер `StoredAsset + KnowledgeFile` юзера → списывает пропорционально.

## Модели

| Таблица | Поля |
|---|---|
| `stored_assets` | user_id, type (image/pdf/video/...), filename, size_bytes, public_token, **last_billed_at** |

## Endpoints

| Метод | Endpoint | Что |
|---|---|---|
| POST | `/assets/upload` | Upload + создание StoredAsset |
| GET | `/assets` | Список |
| DELETE | `/assets/{id}` | Удалить (файл с диска + row) |
| GET | `/assets/{public_token}/file.ext` | Public-доступ (если share-token валиден) |

## Биллинг

- **Race fix:** UNION StoredAsset + KnowledgeFile в одной транзакции (`d90e2f1`).
- **`last_billed_at`** — для пересчёта только разницы периода.
- **Формула:** `(total_size_mb / 100) × 50 ₽ × (days_passed / 30)`.

## Безопасность

- ✅ **public_token** ~160bit — нельзя enumerate
- ✅ **Path traversal protection** — `_abs_path` defense-in-depth
- ✅ **Image URL whitelist** при использовании в КП/презентации (только http/https/data:image/)

## Гочча

- **`/uploads/*` — корень проекта**, не `/var/www/...` (нужно резервировать в pg_dump-бэкапе отдельно через rsync/tar).
- **Удаление row не удаляет файл с диска автоматически** — есть orphan-cleanup scheduler.

## Тесты

- покрытие через интеграционные

## Зависимости

- [02-billing](02-billing-payments.md) — биллинг
- [11-knowledge-rag](11-knowledge-rag.md) — общая storage-квота
- [07-proposals](07-proposals.md) — логотипы брендов
- [08-presentations](08-presentations.md) — PPTX выгрузки
