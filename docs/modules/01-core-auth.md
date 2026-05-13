# Модуль 01 — Core Auth

> **Что это:** регистрация / логин / refresh / OAuth / 2FA / QR-логин. Всё что касается «кто такой юзер и как он залогинен». Open когда: меняешь auth-флоу, чинишь refresh race, добавляешь провайдер OAuth, работаешь с 2FA.

## TL;DR

- **Код:** [server/auth.py](server/auth.py) (JWT helpers) + [server/routes/auth.py](server/routes/auth.py) + [server/routes/oauth.py](server/routes/oauth.py) + [server/routes/qr_login.py](server/routes/qr_login.py).
- **UI:** loginModal в [views/index.html](views/index.html) + [views/qr_confirm.html](views/qr_confirm.html).
- **Провайдеры:** email+password / **VK OAuth** / QR-логин со смартфона. **Google убран** (продуктовое решение: только российские провайдеры).
- **2FA:** TOTP (pyotp), включается админами через `/admin/2fa/setup` → QR → enable. Поле кода появляется в loginModal автоматически.

## Auth-флоу

| Сценарий | Поток |
|---|---|
| Регистрация email | `/auth/register` → email-verification token → клик в письме → `is_verified=true` |
| Login email | `/auth/login` → cookie (access + CSRF) + refresh single-use jti |
| Refresh | `/auth/token/refresh` → atomic JTI revoke + новый pair |
| VK OAuth | `/oauth/vk/start` → state PKCE → callback → user create/link |
| QR login | desktop polls `/qr-login/{token}/poll` ← mobile сканирует и подтверждает |
| 2FA админа | login возвращает `requires_2fa=true` → loginModal показывает поле кода → второй call с `totp_code` |

## Модели

| Таблица | Поля важные |
|---|---|
| `users` | email, password_hash (bcrypt), is_verified, is_admin, **totp_secret (EncryptedString)**, totp_enabled, marketing_consent, notifications_last_seen_at, onboarding_completed, last_login_at |
| `verify_tokens` | для email-verification + password-reset |
| `oauth_states` | PKCE state + verifier |
| `qr_login_sessions` | token, status (pending/confirmed), user_id |

## Безопасность (что важно не сломать)

- ✅ **bcrypt с timing-safe verify** + dummy-hash на login (чтобы не утечь «email существует»). Не убирать!
- ✅ **Password policy 10+ симв** + все 4 класса символов (`1d60bd0`)
- ✅ **JWT в httpOnly cookie** + CSRF double-submit token. CSRF обязателен при наличии cookie ДАЖЕ если Bearer (`dc7eecf`)
- ✅ **Refresh single-use rotation** через `_atomic_jtis_update` (race-safe). После use jti revoke'аем. Compromised refresh → autorevoke_all.
- ✅ **`compare_digest` везде** при сравнении токенов (timing attack)
- ✅ **2FA**: TOTP с pyotp, secret шифруется EncryptedString. QR-bypass закрыт (`dc7eecf`)
- ✅ **Login alert email** при новом IP (см. [server/email_service.py](server/email_service.py))
- ✅ **TG-link rate-limit** + email-alert при привязке Telegram

⚠ **Не использовать `_ALLOWED_PROVIDERS` без `vk` — текущий список `{"vk"}`.** Google `/auth/oauth/google/start` отдаёт 410 Gone.

## Endpoints

| Метод | Endpoint | Что |
|---|---|---|
| POST | `/auth/register` | Email + password + (optional marketing_consent) |
| POST | `/auth/login` | + totp_code если 2FA включена |
| POST | `/auth/logout` | revoke refresh |
| POST | `/auth/token/refresh` | single-use rotation |
| POST | `/auth/revoke-all` | revoke все refresh-токены юзера |
| POST | `/auth/verify-email/{token}` | подтвердить email |
| POST | `/auth/password-reset/request` | отправить reset-email |
| POST | `/auth/password-reset/confirm` | сменить пароль по токену |
| GET | `/auth/me` | текущий юзер + balance_kopecks (для UI balance pill) |
| GET | `/oauth/vk/start` | redirect to VK |
| GET | `/oauth/vk/callback` | callback |
| POST | `/admin/2fa/setup` | вернёт `provisioning_uri` для QR |
| POST | `/admin/2fa/enable` | подтвердить TOTP-код, активировать |
| POST | `/admin/2fa/disable` | требует TOTP-код |
| GET | `/admin/2fa/status` | enabled / disabled |
| POST | `/qr-login/start` | desktop генерирует QR-токен |
| GET | `/qr-login/{token}/poll` | desktop polls |
| POST | `/qr-login/{token}/confirm` | mobile подтверждает (нужен auth) |

## Производственные доступы (не для AI)

Админ-юзер: `vidyakovd@gmail.com` / пароль `28371988`. **lowercase email обязателен** — Postgres case-sensitive, scripts/admin → `create_admin.py` нормализует.

## Известные косяки / TODO

- aud/iss claims проверяются НЕ strict — отложено (см. `dd05348` — поставили дату планируемого включения, но не активировали).
- **Регистрация только в РКН на юзере** — нужно для совсем строгого 152-ФЗ-compliance.

## Тесты

- `tests/test_api.py` — auth, refresh single-use, CSRF
- `tests/test_security_hardening.py` — timing attacks, JWT, 2FA bypass

## Зависимости

- [02-billing](02-billing-payments.md) — `User.tokens_balance` отображается в `/auth/me`
- [18-privacy](18-privacy-compliance.md) — `marketing_consent` + retention анонимизация
- [19-admin](19-admin.md) — 2FA админки
