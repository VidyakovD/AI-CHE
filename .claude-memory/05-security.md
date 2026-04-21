# 05. Security — что сделано и чего не трогать

## Закрытые CVE / уязвимости (19 штук)

### P0 (критичные, все закрыты)

| # | Что было | Где | Как закрыто |
|---|---|---|---|
| 1 | Ключи `.env`, `.jwt_secret` закоммичены в git | корень репо | `git rm --cached` + в .gitignore (но ВСЁ ЕЩЁ В ИСТОРИИ — ключи нужно ревокнуть в OpenAI/Anthropic/Google) |
| 2 | Race condition списания CH (lost update) | везде где `balance +=/-=` | server/billing.py — атомарный UPDATE через SQL |
| 3 | HMAC ЮKassa с silent except + fallback на тело без верификации | routes/payments.py:webhook | Убран try/except, `Payment.find_one()` = источник истины |
| 4 | IDOR в `/payment/confirm/{id}` — можно украсть чужую подписку | routes/payments.py | Проверка `p.metadata.user_id == current_user.id` |
| 5 | Python sandbox в chatbot_engine | `_run_python_sandbox` | Выключен по умолчанию (`ENABLE_PYTHON_SANDBOX=false`) + AST-валидация (no imports/dunder/eval) |
| 6 | Path traversal `/sites/hosted/{id}/*` | routes/sites.py | `Path.resolve() + relative_to()` |
| 7 | CSV injection в `/user/transactions.csv` | routes/user.py | `_csv_safe()` префикс `'` для `=+-@` |
| 8 | Promocode race (used_count++) + bypass (не писался в confirm) | routes/public.py:apply_promo | Atomic UPDATE с WHERE |
| 9 | IDOR `/solutions/runs/{id}/continue` | routes/solutions.py | `run.user_id == user.id` |
| 10 | IDOR `/agent/{id}/status\|cancel` | routes/agent.py | `task.user_id == user.id` |
| 11 | OAuth Account Takeover (auto-link email) | routes/oauth.py | Новый user с таким email = HTTP 400 «войдите паролем» |

### P1 (high, все закрыты)

| # | Что было | Как |
|---|---|---|
| 12 | Нет security headers | main.py middleware: HSTS, X-Frame, X-CT-Opts, Referrer, Permissions |
| 13 | Нет body-size limit (DoS 1GB JSON) | main.py middleware: 12 MB limit |
| 14 | Rate-limit login слабый (100/60s) + multi-worker bypass | SQLite WAL store, 10/300s |
| 15 | SSRF в `http_request` node (можно ходить на AWS metadata) | `_ssrf_validate()` — private/loopback/link-local/metadata |
| 16 | XXE в DOCX parsing | defusedxml fallback без entity expansion |
| 17 | Stored XSS через `/sites/hosted/*` (JS крадёт токены основного домена) | sandbox iframe + strict CSP на обёртке |
| 18 | Double-spend confirm vs webhook | UNIQUE index на `subscriptions.yookassa_payment_id` |
| 19 | TG webhook без secret | `X-Telegram-Bot-Api-Secret-Token` (derived из JWT_SECRET) |

### P2 (medium)

- JWT aud/iss строго проверяется если claim есть
- MIME magic bytes для uploads
- ASCII-only filename
- PII email mask в логах (`vidyakov@... → vi***@...`)
- Admin audit log (таблица + UI во вкладке /admin)
- Timing-attack fix на /auth/login (dummy bcrypt при non-existent email) + /forgot-password (убран user_id из ответа)

## Hardening patterns

### Атомарные локи между workers

```python
# server/worker_lock.py
with worker_lock("scheduler_tick", ttl_sec=25) as acquired:
    if acquired:
        await _scheduler_tick()
```

Применено в: `scheduler_loop`, `apikey_check_loop`, `imap_loop`. SQLite WAL + BEGIN IMMEDIATE + `name+expires_at` row. Fail-open (лучше выполнить дважды чем не выполнить).

### Rate limit (SQLite shared)

```python
# server/security.py
RULES = {
    "/auth/login":        (10, 300),    # 10/5мин
    "/auth/register":     (5, 60),
    "/auth/forgot-password": (5, 300),
    "/message":           (60, 60),
    "/webhook/tg/":       (120, 60),
    "/webhook/vk/":       (120, 60),
    "/webhook/avito/":    (120, 60),
    "/payment/webhook":   (60, 60),
    "/internal/deploy":   (10, 3600),
    "/agent/run":         (30, 60),
    "/auth/oauth/exchange": (30, 60),
}
```

Store: `server/.rate_limit.db` (SQLite WAL). `BEGIN IMMEDIATE → DELETE старые → COUNT → INSERT`.

`_get_client_ip()`: `X-Forwarded-For` доверяется только от `TRUSTED_PROXIES` (env, default `127.0.0.1,::1`). Без этого атакующий подделывает IP и обходит rate-limit.

### Key versioning для IMAP passwords

`server/secrets_crypto.py`:
- Формат: `enc:v1:<token>` (token — Fernet base64)
- Ключ выводится из `JWT_SECRET` через SHA-256
- `LEGACY_JWT_SECRETS` env — список старых ключей через запятую
- При ротации: старый → в LEGACY, новый JWT_SECRET → `POST /admin/reencrypt-secrets` перешифрует все
- Fallback: пробуем current → все legacy → возвращаем ""

### Admin audit log

`server/admin_audit.py::log_admin_action()` пишет в таблицу `admin_audit_log`. Применено в:
- `/admin/users/{id}/adjust-balance`
- `/admin/users/{id}/toggle-ban`
- `/admin/reencrypt-secrets`

GET `/admin/audit-log?limit=200` — читать. UI в admin.html → вкладка «Audit Log».

### Sandbox iframe для `/sites/hosted/*`

HTML AI-сгенерирован, может содержать XSS. Отдаём НЕ напрямую, а в обёртке:

```html
<iframe sandbox="allow-scripts allow-forms allow-popups"
        srcdoc="<escaped inner html>"></iframe>
```

Внутри iframe origin=null → `document.cookie/localStorage` основного `aiche.ru` недоступны. На обёртке strict CSP `default-src 'none'`.

## OAuth exchange flow (не в URL-фрагменте)

1. `/auth/oauth/google/callback` создаёт одноразовый `code` (в `verify_tokens` с purpose=`oauth_exchange`, TTL 60s)
2. Redirect на `/?oauth_code=<code>`
3. Фронт делает `POST /auth/oauth/exchange {code}` → получает access+refresh токены
4. Токены никогда не проходят через URL (раньше были в `#fragment`)

## Что НЕ трогать

1. `server/billing.py::deduct_atomic/strict/credit_atomic` — атомарный SQL-код, ломать нельзя
2. `server/security.py::_get_client_ip` — trusted proxies логика
3. `server/worker_lock.py` — advisory locks, многое завязано
4. `server/secrets_crypto.py::_PREFIX`, `_CURRENT_VERSION` — если меняешь формат — сломаются старые записи
5. HMAC проверка ЮKassa (`routes/payments.py:webhook`) — НЕ глушить exceptions
6. UNIQUE index `uq_subscriptions_yookassa_id` — защита от double-spend
7. CSP header в `/sites/hosted/*` — удалить = вернёт XSS-вектор

## Где найти CVE/аудит отчёты

Последние аудиты проводились через параллельных Explore-agents. Логи в `.claude/output_logs/` — удаляются после сессии. Основные находки в git log:

```bash
git log --grep="security" --oneline
git log --grep="fix" --oneline
```
