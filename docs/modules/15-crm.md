# Модуль 15 — CRM-интеграции

> **Что это:** native поддержка Bitrix24 / amoCRM / generic webhook. При `record.created` (бот собрал заявку) → fire-and-forget POST в каждую active интеграцию юзера. Open когда: добавляешь CRM-провайдера, чинишь mapping, дебажишь auto-disable.

## TL;DR

- **Dispatcher:** [server/crm.py](server/crm.py) (168 строк) — `dispatch_record_to_crm` + mapping per provider.
- **Routes:** [server/routes/crm.py](server/routes/crm.py) — CRUD + test + providers.
- **UI:** в [views/api.html](views/api.html) → вкладка «📞 CRM». Native UI с маппингом полей + тестовая кнопка.
- **Триггер:** при `record.created` из бота → [server/chatbot_engine.py](server/chatbot_engine.py) вызывает `dispatch_record_to_crm` в threading-task.

## Провайдеры

| Провайдер | Как работает |
|---|---|
| **Bitrix24** | Incoming webhook `crm.lead.add`. Mapping `PHONE/EMAIL` → массив объектов `[{"VALUE": ..., "TYPE": "WORK"}]` |
| **amoCRM** | Webhooks «Свой URL». Поля попадают в кастомные. |
| **Generic webhook** | Для Zapier / Make / N8N — просто POST JSON |

## Endpoints

| Метод | Endpoint | Что |
|---|---|---|
| GET | `/crm/connections` | Список интеграций юзера |
| POST | `/crm/connections` | Создать (url + provider + mapping_json) |
| PUT | `/crm/connections/{id}` | Update |
| DELETE | `/crm/connections/{id}` | — |
| POST | `/crm/connections/{id}/test` | Отправить тестовый record |
| GET | `/crm/providers` | Список провайдеров с шаблонами mapping |

## Модели

| Таблица | Поля |
|---|---|
| `crm_connections` | user_id, provider, url (EncryptedString), mapping_json, is_active, fail_count, last_error_at |

## Mapping

JSON-структура: поля `bot_records.fields_json` → поля CRM. Шаблоны для Bitrix24/amoCRM возвращаются через `/crm/providers`.

Пример (Bitrix24):
```json
{
  "TITLE": "{name}",
  "PHONE": [{"VALUE": "{phone}", "TYPE": "WORK"}],
  "EMAIL": [{"VALUE": "{email}", "TYPE": "WORK"}],
  "COMMENTS": "Заявка из бота {bot_name}"
}
```

## Auto-disable + retry

- **Fire-and-forget threading** — не блокируем основной воркфлоу бота.
- **10 ошибок подряд → `is_active=false`** (как в [13-public-api](13-public-api.md)).
- При успехе `fail_count=0` обнуляется.

## Безопасность

- ✅ **SSRF-защита** — DNS rebind + private CIDR блок + scheme whitelist (`d13cb7e`)
- ✅ **`url` хранится зашифрованным** (EncryptedString)
- ✅ **`/crm/connections/{id}/test`** — отправляет фейк-record, не реальные данные клиентов

## Гочча

- **CRM ≠ Public API webhooks** ([13-public-api](13-public-api.md)) — два разных dispatcher'а, разные таблицы (`crm_connections` vs `api_webhooks`), разный HMAC.
  - CRM: для коробочных провайдеров с native-mapping, юзер мало знает про webhook'и
  - Public API webhooks: для разработчиков юзера с подписью HMAC
- **Refactor (`1c3b5a1`):** общий outbound dispatcher для CRM + webhooks ([server/_outbound.py](server/_outbound.py)).

## Тесты

- `tests/test_crm.py` (12 тестов) — dispatcher + mapping + auto-disable

## Зависимости

- [05-chatbots](05-chatbots.md) — `record.created` триггер
- [13-public-api](13-public-api.md) — родственный outbound (общий `_outbound.py`)
