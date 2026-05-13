# Модуль 11 — Knowledge / RAG

> **Что это:** RAG база знаний юзера — загруженные документы (PDF/DOCX/XLSX/TXT) → чанкинг → embeddings → semantic search. Подключается к чат-ботам и агентам. Билится как storage. Open когда: чинишь embeddings, добавляешь тип файла, дебажишь поиск.

## TL;DR

- **Код:** [server/knowledge.py](server/knowledge.py) (707 строк) + [server/routes/knowledge.py](server/routes/knowledge.py).
- **UI:** [views/knowledge-ui.js](views/knowledge-ui.js) (345 строк) — встраивается в редактор бота.
- **Биллинг:** **50 ₽/мес за 100 МБ** через ключ `storage.per_100mb_month=5_000` — общий с [16-storage](16-storage.md).
- **Защита:** `_abs_path` defense-in-depth (path traversal) + `/uploads/*` как URL-path (`aa18470`).

## Архитектура

```
File upload → text extraction (pdf/docx/xlsx)
            → chunking (overlap, размер настраиваем)
            → embedding (per-chunk)
            → save KnowledgeChunk
RAG query:
  query embedding → cosine similarity → top-k chunks → инжект в context LLM
```

## Модели

| Таблица | Поля |
|---|---|
| `knowledge_files` | user_id, owner_type (bot/agent), owner_id, filename, size_bytes, **last_billed_at**, status |
| `knowledge_chunks` | file_id, idx, text, embedding (vector) |

## Endpoints

| Метод | Endpoint | Что |
|---|---|---|
| POST | `/knowledge/upload` | Upload + extract + chunk + embed |
| GET | `/knowledge` | Список файлов юзера (фильтр по owner) |
| PUT | `/knowledge/{id}/toggle` | Включить/выключить файл |
| DELETE | `/knowledge/{id}` | Удалить |
| POST | `/knowledge/search` | Тест поиска (для отладки бота) |

## Storage-биллинг (общий с [16-storage](16-storage.md))

- Cron-job в [server/scheduler.py](server/scheduler.py) **раз в день**: считает суммарный размер `KnowledgeFile + StoredAsset` юзера → списывает `(size_mb / 100) × 50 ₽` пропорционально дням.
- `last_billed_at` — для пересчёта только разницы.
- **Race fix:** UNION StoredAsset + KnowledgeFile в одной транзакции (`d90e2f1`).

## Использование в боте/агенте

- Нода `rag_search` в workflow — query → top-k чанков → подставка в LLM context.
- `owner_type='bot'` или `'agent'` определяет область видимости.

## Гочча

- **Embeddings провайдер:** OpenAI text-embedding-3-small по умолчанию (если меняешь — старые vectors несовместимы → reindex).
- **`/uploads/*` — это URL-path, не Linux abs path** (`aa18470`).
- **Большие PDF (>50MB)** могут таймаутить — нужно chunk-streaming.

## Тесты

- `tests/test_knowledge.py` — upload, chunk, search

## Зависимости

- [02-billing](02-billing-payments.md) — storage-биллинг
- [03-ai-core](03-ai-core.md) — embeddings провайдер
- [05-chatbots](05-chatbots.md) — rag_search в workflow
- [10-agents](10-agents-workflows.md) — rag_search tool
- [16-storage](16-storage.md) — общий биллинг по объёму
