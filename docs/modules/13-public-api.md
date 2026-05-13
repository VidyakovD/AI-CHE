# Модуль 13 — Public API + Webhooks

> **Что это:** REST API для разработчиков юзера (Bearer-токены, scope-проверка) + 7 типов webhook-событий с HMAC-подписью. Open когда: добавляешь endpoint в Public API, новый webhook-event, чинишь auto-disable.

## TL;DR

- **Routes:** [server/routes/public_api.py](server/routes/public_api.py) (414 строк).
- **Webhooks dispatcher:** [server/webhooks.py](server/webhooks.py) (138 строк) — HMAC-SHA256, fire-and-forget threading, auto-disable.
- **UI:** [views/api.html](views/api.html) (753 строки) — токены / webhooks / CRM / docs. Standalone + в кабинете через iframe (`?embed=1`).
- **Аутентификация:** `Authorization: Bearer ai_che_***` — токен из `api_tokens` таблицы. Scope-aware через `authenticate_token(request, db, required_scope=...)` + `is_verified` check.
- **Idempotency:** atomic UPDATE `requests_count` (без race на multi-worker).

## Endpoints (для разработчиков юзера)

| Метод | Endpoint | Scope | Что |
|---|---|---|---|
| GET | `/api/v1/me` | `read:profile` | Профиль (balance, etc.) |
| POST | `/api/v1/proposals/generate` | `write:proposals` | Сгенерировать КП |
| GET | `/api/v1/proposals/{id}` | `read:proposals` | Деталь |
| (планируется) | `/api/v1/solutions/{id}/run` | `write:solutions` | Запуск пилота |
| (планируется) | `/api/v1/chatbots` | `read:chatbots` | Список ботов |

⚠ **`is_verified` обязателен** — неподтверждённый email = 403.

## Mgmt endpoints (UI для самого юзера в кабинете)

| Метод | Endpoint | Что |
|---|---|---|
| GET | `/api-tokens` | Список своих токенов |
| POST | `/api-tokens` | Создать токен с scopes |
| DELETE | `/api-tokens/{id}` | Отозвать |
| GET | `/api-tokens/webhooks` | Список webhook-подписок |
| POST | `/api-tokens/webhooks` | Создать (url + events + secret) |
| PUT | `/api-tokens/webhooks/{id}` | Update |
| DELETE | `/api-tokens/webhooks/{id}` | — |
| POST | `/api-tokens/webhooks/{id}/test` | Тестовый event для проверки |

## Webhook-события (7 типов)

| Event | Когда фаирится |
|---|---|
| `proposal.opened` | клиент открыл `/p/{token}` |
| `proposal.sent` | КП отправлено email-orchestrator'ом |
| `proposal.signed` | клиент подписал |
| `record.created` | бот собрал заявку/бронь у клиента |
| `solution.done` | orchestra-run завершилась |
| `site.done` | сайт сгенерирован |
| `site.failed` | генерация упала |

## HMAC-подпись

```
X-Aiche-Signature: sha256=<hex>
```

Подписывается `request_body` ключом `webhook.secret`. Юзер на своей стороне сверяет.

## Auto-disable

10 ошибок (non-2xx) подряд → `is_active=false`. UI показывает «Webhook отключён». Юзер должен исправить URL и включить вручную.

## Модели

| Таблица | Поля |
|---|---|
| `api_tokens` | user_id, token_hash (bcrypt), scopes (JSON), is_active, requests_count, last_used_at |
| `api_webhooks` | user_id, url, events (JSON), secret, is_active, fail_count, last_error_at |

## Безопасность

- ✅ **Token hash** в БД (bcrypt), не plaintext
- ✅ **SSRF-защита** webhook URL — no localhost / private CIDR / DNS-rebind
- ✅ **Scope-aware** все endpoints — нельзя через `read:profile` сделать write
- ✅ **Atomic UPDATE requests_count** (multi-worker race fix)
- ✅ **HMAC обязателен** — без подписи не отправляем

## Гочча

- **`?embed=1`** — Public API доступен в кабинете через iframe, чтобы юзер не переходил на отдельную страницу.
- **CSRF cookie+Bearer**: при наличии cookie CSRF обязателен **независимо** от Bearer (`dc7eecf`).
- **Standalone `/api.html`** остаётся для разработчиков (если кто-то хочет ссылку на доку).

## Тесты

- `tests/test_critical_paths.py::TestPublicAPI` — auth, scopes, atomic count
- `tests/test_new_features.py` — webhook test endpoint + HMAC

## Зависимости

- [01-core-auth](01-core-auth.md) — `is_verified` check
- [07-proposals](07-proposals.md) — события proposal.*
- [05-chatbots](05-chatbots.md) — событие record.created
- [06-solutions](06-solutions.md) — событие solution.done
- [09-sites](09-sites.md) — события site.done / site.failed
- [15-crm](15-crm.md) — родственная dispatch-логика (CRM это тоже исходящий webhook, отдельный)
