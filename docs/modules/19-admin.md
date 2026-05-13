# Модуль 19 — Admin panel

> **Что это:** админка для контроля платформы — юзеры, статистика, action_logs, ai-stats, pricing_config, 2FA, marketplace модерация, assistant-feedback кластеризация. Open когда: добавляешь admin-фичу, чинишь /admin/*, работаешь с TOTP.

## TL;DR

- **Routes:** [server/routes/admin.py](server/routes/admin.py) — все админ-endpoints.
- **UI:** [views/admin.html](views/admin.html) (1640 строк) — табы: Пользователи, Pricing, Действия, AI-stats, **🛍 Marketplace модерация** (если включена), **🔐 Безопасность 2FA**.
- **Доступ:** `ADMIN_EMAILS` в env (через [server/security.py](server/security.py)) + опционально 2FA TOTP.

## Endpoints

| Метод | Endpoint | Что |
|---|---|---|
| GET | `/admin/users` | Список с фильтрами |
| GET | `/admin/users/{id}` | Деталь |
| POST | `/admin/users/{id}/credit` | Начислить (бесплатно от админа) |
| GET | `/admin/stats` | Общая статистика |
| GET | `/admin/usage` | Использование AI / решений / КП |
| GET | `/admin/ai-stats?days=N` | Из `ai_request_logs` — totals + by_model + top_users |
| GET | `/admin/actions.txt?since_hours=N` | Audit-log в text |
| GET | `/admin/actions.jsonl?since_hours=N` | Audit-log в JSONL |
| GET/POST | `/admin/pricing` | Pricing_config UI |
| GET | `/admin/assistant/issues` | Feedback assistant — кластеризованные жалобы |
| POST | `/admin/2fa/setup` | TOTP setup → return provisioning_uri (QR) |
| POST | `/admin/2fa/enable` | Подтвердить код |
| POST | `/admin/2fa/disable` | Требует TOTP-код |
| GET | `/admin/2fa/status` | enabled / disabled |
| POST | `/admin/reencrypt-secrets` | Ротация JWT_SECRET без потери EncryptedString-полей |
| POST | `/admin/listings/{id}/approve` | Marketplace модерация (если включена) |
| POST | `/admin/listings/{id}/reject` | Marketplace модерация |

## 2FA для админов

См. [01-core-auth](01-core-auth.md). TOTP через pyotp, secret хранится EncryptedString.

После `/admin/2fa/enable`:
- Login возвращает `requires_2fa=true`
- loginModal показывает поле кода
- Второй call с `totp_code`

QR-bypass атака закрыта (`dc7eecf`).

## `/admin/reencrypt-secrets`

Сценарий: меняешь `JWT_SECRET` (например при компрометации) → все `EncryptedString` поля (api_keys, totp_secret, crm.url) расшифровываются старым → шифруются новым.

⚠ Должно вызываться **с обоими секретами в env одновременно**: `JWT_SECRET_OLD` + `JWT_SECRET`. Иначе потеря данных.

## Гочча

- **`ADMIN_EMAILS`** через запятую в env. **lowercase обязателен**.
- **Action_logs пишется sync** — для больших дампов через `/admin/actions.txt` может тормозить.
- **AI-stats считается JIT** — для долгих периодов лучше кэшировать.

## Тесты

- через `tests/test_api.py` + `tests/test_new_features.py` (2FA)

## Зависимости

- [01-core-auth](01-core-auth.md) — 2FA
- [02-billing-payments](02-billing-payments.md) — pricing_config UI
- [18-privacy-compliance](18-privacy-compliance.md) — action_logs, ai-stats
- [03-ai-core](03-ai-core.md) — `/admin/reencrypt-secrets`
- [12-marketplace](12-marketplace.md) — модерация (если flag включён)
