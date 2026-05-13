# Модуль 18 — Privacy / Compliance (152-ФЗ + 54-ФЗ)

> **Что это:** PrivacyGuard (PII-маскировка для LLM), data-retention cron (анонимизация неактивных юзеров), audit-log, шифрование backup'ов. Open когда: меняешь PII-логику, дебажишь retention, готовишься к проверке РКН.

## TL;DR

- **PrivacyGuard:** [server/privacy_guard.py](server/privacy_guard.py) (223 строки) — маскирует PII перед LLM, unmask на ответе. **152-ФЗ ст. 6 compliance.**
- **AiRequestLog:** таблица `ai_request_logs` ([03-ai-core](03-ai-core.md)).
- **Audit-log:** таблица `action_logs` ([server/audit_log.py](server/audit_log.py)), endpoints `/admin/actions.txt|.jsonl`.
- **Data-retention cron:** в [server/scheduler.py](server/scheduler.py) — анонимизация User с `last_login_at > N мес` + purge старого `ProposalProject.generated_html`. **152-ФЗ ст. 5.**

## PrivacyGuard — PII-маскировка для LLM

Адаптация Dolibarr `privacy_guard.class.php`. Перед LLM:

```python
from server.privacy_guard import with_pii_protection

safe, guard = with_pii_protection("Иван Петров (ИНН 7707083893, +79991234567)")
# safe = "Иван Петров (ИНН [INN_1], [PHONE_1])"
ans = generate_response(model, messages=[{"role":"user","content":safe}])
final = guard.unmask_response(ans["content"])
# восстанавливает реальные значения в ответе
```

**Что маскируется:**
- ИНН (10 / 12 цифр)
- КПП (9 цифр)
- ОГРН / ОГРНИП
- СНИЛС
- Номер карты (с Luhn-проверкой)
- Email
- Телефон (RU форматы)

## Data-retention cron (152-ФЗ ст. 5)

Адаптация Dolibarr `datapolicycron.class.php`. ENV-настройки:

| ENV | Default | Что |
|---|---|---|
| `DATA_RETENTION_USER_INACTIVE_MONTHS` | 24 | Анонимизация User'а если `last_login_at` старше |
| `DATA_RETENTION_PROPOSAL_YEARS` | 5 | Purge `generated_html` старых КП |
| `DATA_RETENTION_DRY_RUN` | `true` | Если true — только лог, без изменений |

⚠ **На проде сейчас НЕ включён** (`DATA_RETENTION_DRY_RUN=` пустая = выключен). Юзер должен решить когда активировать.

**Анонимизация User:** заменяет email на `deleted_<id>@local`, обнуляет имя, телефон, оставляет id для FK.

## Audit-log (action_logs)

Что логируется:
- `auth.*` (login, logout, refresh, 2fa)
- `payment.*` (без сумм — best practice)
- `ai.*` (модель, не контент)
- `proposal.*` + `proposal.signed`
- `solution.*` + `solution.auto_flagged`
- `orchestra_schedule.*`
- `marketplace.*`
- `api_token.*`
- `api_webhook.*`
- `crm.connection_*`
- `admin.*`
- `record.created`
- `asset.*`

**Endpoints:** `/admin/actions(.txt|.jsonl)?since_hours=N`. Cleanup retention в scheduler.

## 54-ФЗ (онлайн-кассы)

См. [02-billing-payments](02-billing-payments.md). YooKassa receipt:
- `payment_subject=service`
- `payment_mode=full_payment`
- `tax_system_code=2` (УСН)
- `vat_code=1`

## Маркетинговое согласие

- Отдельно от оферты (`d8ffb61`)
- Чекбокс в UI регистрации
- Поле `User.marketing_consent` (boolean + timestamp)

## Шифрование

- ✅ **AES-256-GCM** для DB-бэкапов (chat.db.YYYY-MM-DD.enc), retention 14 дней
- ✅ **EncryptedString** для секретов в БД (api_keys, totp_secret, crm.url) через HKDF от JWT_SECRET
- ✅ **`/admin/reencrypt-secrets`** — ротация JWT_SECRET без потери EncryptedString-полей

## Compliance checklist

- ✅ AES-256-GCM шифрование DB-бэкапов
- ✅ 54-ФЗ receipt в YooKassa
- ✅ Маркетинговое согласие отдельно
- ✅ Из payment-логов убраны суммы
- ✅ **Сервер в РФ (Москва)** (после миграции с NL)
- ✅ PrivacyGuard для PII в LLM
- ✅ Audit-log + retention
- ⚠ SMTP настроен Yandex 360 (юзеры получают verification)
- ⚠ **РКН-регистрация** — задача юзера
- ⚠ Прод-shop ЮKassa — задача юзера
- ⚠ **DATA_RETENTION_DRY_RUN не включён** на проде

## Гочча

- **PrivacyGuard опционален** — не подключён ко всем местам автоматически. Только там, где явно вызывается `with_pii_protection`.
- **Audit-log пишется sync** — если БД медленная, тормозит запросы. Идея: async-queue.
- **`compliance_ru.md`** ([docs/compliance_ru.md](docs/compliance_ru.md)) — родственный документ для юр-лиц.

## Тесты

- `tests/test_privacy_guard.py` — 22 теста
- `tests/test_data_retention.py` — 5 тестов
- `tests/test_ai_request_log.py` — 6 тестов

## Зависимости

- [03-ai-core](03-ai-core.md) — PrivacyGuard вызывается перед LLM, AiRequestLog после
- [02-billing-payments](02-billing-payments.md) — 54-ФЗ receipt
- [19-admin](19-admin.md) — endpoints для просмотра action_logs/ai-stats
- [01-core-auth](01-core-auth.md) — marketing_consent
