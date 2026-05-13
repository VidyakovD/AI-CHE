# Модуль 00 — Overview (стек, инфра, прод)

> **Что это:** общая инженерная картина — стек, прод-сервер, прокси, деплой, тарифы. Открой этот модуль если задача про инфраструктуру, переезд, или нужен общий контекст без углубления в продукт.

## Стек

| Слой | Технологии |
|---|---|
| Backend | Python 3.10 (прод) / 3.12 (legacy NL) / 3.14 (dev), FastAPI 0.111, SQLAlchemy 2.0 |
| БД | **PostgreSQL 14** на проде / SQLite для dev (`DATABASE_URL=sqlite:///./chat.db`) |
| Frontend | HTML + Tailwind (CDN + build step) + vanilla JS (SPA) — НЕ React |
| AI | OpenAI, Anthropic, Perplexity, Grok (xai), Google AI Studio |
| Builders | xhtml2pdf + DejaVu/Liberation/Noto · python-docx · openpyxl · python-pptx |
| 2FA | pyotp 2.9 (TOTP) |
| Push | pywebpush 2.0 (VAPID) |
| Crypto | AES-256-GCM (cryptography), HKDF от `JWT_SECRET` |
| Auth | JWT в httpOnly cookie + CSRF (double-submit) + refresh single-use rotation |

## Прод-инфраструктура (на 2026-05-13)

| Параметр | Значение |
|---|---|
| IP | `193.187.92.147` |
| Локация | Москва, RU 🇷🇺 |
| Провайдер | HOSTKEY (AS50867) |
| OS | Ubuntu 22.04.5 LTS |
| Hardware | 2 vCPU / 4 ГБ RAM / 60 ГБ NVMe / 3 ТБ трафика |
| Python | 3.10.12 (системный) |
| venv | `/root/AI-CHE/venv/bin/python` |
| Путь | `/root/AI-CHE` |
| Сервис | `systemctl status ai-che` (4 worker'а на 127.0.0.1:8000) |
| nginx | `/etc/nginx/sites-available/aiche.ru` (HTTPS, LE + auto-renew, HSTS preload) |
| PostgreSQL | `localhost:5432` БД `aiche`, юзер `aiche`, пароль в `/root/.aiche-postgres-password` |
| UFW | только 22 / 80 / 443 |
| fail2ban | на SSH (5 fails / 10 min → ban 1h) |
| SSH-ключ Claude | в `~/.ssh/authorized_keys` |

**Legacy NL-сервер:** `194.104.9.219` (Дронтен), Python 3.12 — держим как backup. После подтверждения миграции можно выключить.

## AI-прокси

Российский сервер заблокирован OpenAI/Anthropic/Google/Grok → весь AI-трафик через **Xray-client на проде**:

- Xray слушает `127.0.0.1:10809` (HTTP-proxy) → VLESS Reality → `31.169.126.79:443`
- Конфиг: `/usr/local/etc/xray/config.json`
- ENV: `AI_HTTPS_PROXY=http://127.0.0.1:10809` (для OpenAI/Anthropic/Google/Grok)
- **Perplexity напрямую с РФ-сервера:** `PERPLEXITY_HTTPS_PROXY=` (пустая = override "no proxy")

Helper: [server/ai.py](server/ai.py) → `_ai_proxy(provider)`, `_openai_client_kwargs(provider)`.

⚠ **Если прокси падает — падают все AI-фичи кроме Perplexity.** План аварии: в [20-infra-deploy.md](20-infra-deploy.md).

## DNS / SSL

- A-запись `aiche.ru` + `www.aiche.ru` → `193.187.92.147` (TTL 300 для быстрого отката)
- Let's Encrypt cert (expires 2026-08-03), `certbot.timer` auto-renew
- TLSv1.2/1.3, HSTS preload (2 года), CSP, redirect HTTP→HTTPS

## Деплой

```bash
git push origin claude/<branch>:main

HOME="C:\\Users\\Денис" ssh -i "C:\\Users\\Денис\\.ssh\\id_ed25519" \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  root@193.187.92.147 \
  "cd /root/AI-CHE && git pull origin main && \
   systemctl restart ai-che && systemctl is-active ai-che"
```

⚠ **uvicorn слушает только 127.0.0.1.** Внешний 8000 закрыт UFW, доступ только через nginx.
⚠ **Кириллица в HOME** ломает ssh на Windows, обязательно `HOME="C:\\Users\\Денис"` (см. memory `ssh_workaround.md`).

## Local dev

```bash
DEV_MODE=true APP_ENV=dev JWT_SECRET=test-jwt-secret-32-chars-long-yes \
ALLOWED_ORIGINS=http://localhost:8000 \
python -m uvicorn main:app --reload --port 8001
```

## Тарифы — общая логика

- **Все цены в копейках** (1 ₽ = 100 коп) в `User.tokens_balance`, `Transaction.tokens_delta` и pricing_config.
- UI: `window.fmtRub(kop)` → "X.XX ₽".
- Источник истины — БД `pricing_config`, не env, не код. Изменения через `/admin/pricing` UI.
- Списания/начисления — **только** через `server.billing.deduct_strict / deduct_atomic / credit_atomic`.

Полные тарифы — в [02-billing-payments.md](02-billing-payments.md).

## Шрифты

- **Только Golos Text** (российский от Yandex/КБ Симон-Глюк) — захостили локально в `views/fonts/*.woff2`.
- Единый `views/fonts.css` подключён через `<link rel="stylesheet" href="/fonts.css"/>` в каждом HTML.
- Material Symbols тоже локально.
- ❌ **Никаких внешних CDN** (Google Fonts, bunny.net) — после серии 152-ФЗ/блокировочных рисков.

## Структура проекта (top-level)

```
/server/         — backend Python модули
/server/routes/  — FastAPI роутеры
/server/messaging/ — отправители для каналов ботов (TG/VK/Avito/MAX/WA/widget)
/server/agents/  — workflow-builder + специализированные роли агентов
/views/          — HTML SPA + JS helpers + шрифты
/scripts/        — миграции, сиды решений, утилиты
/tests/          — pytest (299 passing на проде)
/alembic/        — версионные миграции (baseline + parallel LIGHTWEIGHT_MIGRATIONS)
/uploads/        — пользовательские файлы (картинки/PDF/видео)
/deploy/         — systemd unit + nginx configs
/docs/           — внешняя документация
/docs/modules/   — модульная инженерная документация (← ты здесь)
/main.py         — entry point FastAPI
```

## PWA — выключен kill-switch'ем (2026-05-12)

После серии проблем с агрессивным кэшированием (юзеры залипали на старых версиях, не получали правки) Service Worker отключён через kill-switch в `views/sw.js`. Запись в [HANDOVER.md](HANDOVER.md), коммит `7f27ee0`. Возвращать — писать новый, не реанимировать старый.

## Production-readiness checklist

- ✅ Sentry (guarded `SENTRY_DSN`)
- ✅ Structured logs (`STRUCTURED_LOGS=1` → JSON)
- ✅ X-Request-ID middleware
- ✅ Auto-backup AES-GCM + integrity-check, retention 14 дней + Yandex Object Storage
- ✅ Audit log с retention
- ✅ Idempotency-Key (DB-based, multi-worker safe)
- ✅ CI с pytest + ruff + pip-audit
- ✅ 299 тестов
- ⚠ SMTP Yandex 360 настроен, **РКН-регистрация на юзере**, прод-shop ЮKassa на юзере

## Зависимости от других модулей

Этот модуль — родитель остальных. Не зависит ни от чего. Деталь конкретного домена — открывай свой модуль.
