# Модуль 04 — Chat (общий чат с AI)

> **Что это:** основной чат-интерфейс юзера с AI (GPT/Claude/Perplexity/Grok). Идемпотентность, голосовой ввод/вывод, история. Open когда: чинишь /message, добавляешь модель в выбор, дебажишь голосовой ввод.

## TL;DR

- **Код:** [server/routes/chat.py](server/routes/chat.py) (POST /message, upload, voice/parse/transcribe/tts).
- **UI:** [views/index.html](views/index.html) — основная вкладка чата + микрофон + 🔊 TTS-кнопка.
- **Lite UI:** [views/mobile.html](views/mobile.html) — мобильный режим (`/routes/mobile.py`).
- **Идемпотентность:** **DB-based** `IdempotencyRecord` с UNIQUE(user_id, key) — multi-worker safe.
- **Voice:** Whisper (audio→text) + TTS (text→speech, 6 голосов OpenAI).

## Endpoints

| Метод | Endpoint | Что |
|---|---|---|
| POST | `/chat/create` | Новый чат |
| GET | `/chat/{chat_id}` | История |
| POST | `/message` | Отправка → AI → ответ. **Idempotency-Key обязателен** |
| POST | `/upload` | Загрузить файл к сообщению (картинки/PDF/docx) |
| GET | `/kling/status/{task_id}` | Polling статуса генерации видео |
| POST | `/mobile/voice/parse` | Whisper → text → AI команда (lite-режим) |
| POST | `/mobile/voice/transcribe` | Whisper → чистый текст |
| POST | `/mobile/voice/tts` | TTS, 6 голосов OpenAI |

## Идемпотентность

⚠ **Многоworker-сейф:** не in-memory, а DB-table `IdempotencyRecord` с UNIQUE(user_id, key).

Поток:
1. Frontend генерирует UUID и кладёт в header `Idempotency-Key`.
2. Backend пробует INSERT в `idempotency_records`. Если UNIQUE-violation — возвращает кэшированный ответ.
3. Cleanup-cron удаляет записи > 24h.

Без этого двойной клик на «Отправить» при медленной сети = двойное списание баланса.

## Голос (Whisper + TTS)

- **Whisper** — 5 ₽ за запрос (фикс).
- **TTS** — 2.25 ₽ / 1000 симв, минимум 50 коп. 6 голосов OpenAI.
- Имплементация: [server/messaging/voice.py](server/messaging/voice.py) (после декомпозиции chatbot_engine — `56ce8d6`).
- В UI: 🎤 кнопка в input chat + 🔊 кнопка под ответом ассистента.

## Модели в выпадайке чата

Список собирается из MODEL_REGISTRY ([server/ai.py](server/ai.py)) с фильтром `purpose in ("chat", "image", "video")`.

Юзер может включить «свои API-ключи» (-80% цена) — кнопка в кабинете → таблица `user_apikeys`.

## Файлы

- `/upload` → сохраняет в `/uploads/{user_id}/...`, возвращает URL.
- В чате аттачит к следующему `/message`.
- Поддержка: картинки (vision Claude/GPT-4o), PDF/DOCX/XLSX (file_extract стадии).

## Гочча

- **Не подключать `chatbot_engine.send_*` к чату с AI** — это для каналов ботов (TG/VK/etc).
- **`/upload` не лимитит размер** — упирается в nginx `client_max_body_size`.
- **TTS-fallback** — если voice_id не валидный, OpenAI возвращает 400 → refund.

## Тесты

- `tests/test_api.py::TestChat` — /message + idempotency
- `tests/test_mobile.py` — voice/parse/transcribe/tts

## Зависимости

- [02-billing](02-billing-payments.md) — списание после `generate_response`
- [03-ai-core](03-ai-core.md) — `generate_response`
- [11-knowledge-rag](11-knowledge-rag.md) — RAG knowledge можно подключать к чату (через бот, не напрямую)
