# AI Студия Че — CLAUDE.md (индекс)

Это **корневой контекст**, который AI-ассистент читает в начале каждого чата. Здесь только то, что нужно знать **всегда**. Детали по модулям — в `docs/modules/`.

> Если ты впервые в проекте: прочитай этот файл целиком, затем посмотри [docs/modules/00-overview.md](docs/modules/00-overview.md), затем — модуль, релевантный задаче. Не нужно читать все 21 модуль подряд.

---

## 🔴 ГЛАВНАЯ ЗАДАЧА — ИИ Агенты v2

**Переработка ИИ Агентов**: сложный workflow-конструктор → простой каталог 6 готовых ролей (Поисковик / Парсер / Юрист / Бухгалтер / Креатор / Автоответчик), Knowledge Hub (общая база компании, AI auto-classify), скилы поверх ролей. Креаторы переезжают как одна из ролей. Полное ТЗ + 6 итераций — [docs/modules/22-agents-v2-roadmap.md](docs/modules/22-agents-v2-roadmap.md).

**В новом чате СНАЧАЛА** уточни 6 вопросов из секции «Открытые вопросы» (тариф, лимиты Knowledge Hub, что делать с автоответчиком vs текущими ботами), потом стартуй с Итерации 1 — Knowledge Hub.

---

## ✅ Закрытые спринты

- **2026-05-15** — Аудит 40 пилотов Solutions + 2 продакшен-бага (MODEL_REGISTRY / placeholder regex), 14 техдолгов КП/Сайтов закрыто, scheduler.py 1379→349 (split на server/cron/), JWT-strict с авто-активацией 2026-06-10, КП-Jinja public page.
- **2026-05-13** — Креаторы MVP за 6 итераций. Профиль бренда → AI-план в календарном виде → подготовка постов (freemium 3/мес) → автопостинг TG/VK → анализ соцсетей. [docs/modules/21-creators-roadmap.md](docs/modules/21-creators-roadmap.md).

Следующие крупные направления — см. [TODO_NEXT.md](TODO_NEXT.md).

---

## Что это за проект

**B2B AI-платформа для предпринимателей.** FastAPI + HTML SPA (PWA отключён) + Public REST API + Webhooks + CRM-интеграции + **MCP-сервер для Claude Desktop**.

**Продукты:** Чат с AI (5 провайдеров) · Бизнес-решения PRO (40 пилотов) · Чат-боты (6 каналов) · AI-агенты (workflow) · **Креаторы (контент-планирование с автопостингом TG/VK)** · Сайты под ключ · КП с e-подписью · Презентации · Public API + MCP · CRM-интеграции · RAG база знаний.

**Простой гайд для самих юзеров:** [USER_GUIDE.md](USER_GUIDE.md).
**История спринтов (changelog):** [HANDOVER.md](HANDOVER.md).
**Текущие задачи:** [TODO_NEXT.md](TODO_NEXT.md).

---

## 📚 Карта модулей

Каждый модуль — отдельный файл в `docs/modules/`. Если в чате обсуждается тема — открой соответствующий модуль и работай по нему.

| # | Модуль | Когда открывать |
|---|---|---|
| 00 | [overview](docs/modules/00-overview.md) | Стек, прод-сервер, прокси, деплой, тарифы, шрифты |
| 01 | [core-auth](docs/modules/01-core-auth.md) | JWT, refresh-rotation, 2FA TOTP, VK OAuth, QR-логин |
| 02 | [billing-payments](docs/modules/02-billing-payments.md) | Баланс, pricing_config, ЮKassa, 54-ФЗ |
| 03 | [ai-core](docs/modules/03-ai-core.md) | ai.py, провайдеры, прокси, AiRequestLog |
| 04 | [chat](docs/modules/04-chat.md) | /message, idempotency, voice/TTS |
| 05 | [chatbots](docs/modules/05-chatbots.md) | chatbot_engine, 7 шаблонов, 6 каналов |
| 06 | [solutions](docs/modules/06-solutions.md) | 40 пилотов, orchestra, v2 input_schema, schedules |
| 07 | [proposals](docs/modules/07-proposals.md) | КП, бренды, e-подпись, прайсы |
| 08 | [presentations](docs/modules/08-presentations.md) | PPTX/HTML/PDF |
| 09 | [sites](docs/modules/09-sites.md) | sandbox-iframe, edit-режим, patch-based /iterate |
| 10 | [agents-workflows](docs/modules/10-agents-workflows.md) | agent_runner, workflow_builder, 25+ ролей |
| 11 | [knowledge-rag](docs/modules/11-knowledge-rag.md) | embeddings, chunks, storage-биллинг |
| 12 | [marketplace](docs/modules/12-marketplace.md) | ⏸ Отключён (feature-flag) |
| 13 | [public-api](docs/modules/13-public-api.md) | Bearer-токены, scopes, 7 webhook-событий |
| 14 | [mcp-server](docs/modules/14-mcp-server.md) | JSON-RPC, 10 tools, 3 resources |
| 15 | [crm](docs/modules/15-crm.md) | Bitrix24, amoCRM, generic webhook |
| 16 | [storage](docs/modules/16-storage.md) | StoredAsset, public_token, биллинг |
| 17 | [push](docs/modules/17-push.md) | VAPID, /notifications |
| 18 | [privacy-compliance](docs/modules/18-privacy-compliance.md) | PrivacyGuard PII, data-retention, 152-ФЗ |
| 19 | [admin](docs/modules/19-admin.md) | /admin/*, 2FA, reencrypt-secrets, ai-stats |
| 20 | [infra-deploy](docs/modules/20-infra-deploy.md) | db.py, alembic, scheduler, миграция |
| 21 | [creators](docs/modules/21-creators-roadmap.md) | Креаторы: бренд / план / подготовка / автопостинг TG·VK / анализ |
| 22 | [agents-v2-roadmap](docs/modules/22-agents-v2-roadmap.md) | 🔴 **Roadmap** — переработка ИИ Агентов (готовые роли + Knowledge Hub + скилы) |

---

## ⚠️ Правила, которые нельзя нарушать

### Деньги — копейки, не рубли

- Баланс юзера = `User.tokens_balance` в **копейках** (1 ₽ = 100 коп).
- Поля `tokens_balance`, `tokens_delta`, `ch_per_1k_*` — legacy имена, **значения в копейках**.
- UI: `window.fmtRub(kop)` → "X.XX ₽".
- Списания/начисления — **только** через `server.billing.deduct_strict / deduct_atomic / credit_atomic`. Никаких `user.tokens_balance -= X`.

### Базовая архитектура

- Сессии БД вне FastAPI Depends — **только** через `with db_session() as db:`.
- Секреты в БД — через `EncryptedString` (HKDF от `JWT_SECRET`).
- Миграции колонок — `LIGHTWEIGHT_MIGRATIONS` в [server/db.py](server/db.py). Целые новые таблицы — через `Base.metadata.create_all`.
- Native dialogs запрещены — везде `aiAlert / aiConfirm / aiPrompt` (см. [views/icons.js](views/icons.js)).
- Картинки в `/uploads/` (КОРЕНЬ проекта).
- Логи действий — `log_action(...)` в новых endpoint'ах.
- Public API endpoints — scope-aware через `authenticate_token(request, db, required_scope=...)` + `is_verified` check.
- Race на multi-worker: все RMW операции — либо `UPDATE ... SET = + 1`, либо UNIQUE-protected.

### Деплой

```bash
git push origin claude/<branch>:main

HOME="C:\\Users\\Денис" ssh -i "C:\\Users\\Денис\\.ssh\\id_ed25519" \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  root@193.187.92.147 \
  "cd /root/AI-CHE && git pull origin main && \
   systemctl restart ai-che && systemctl is-active ai-che"
```

**NEVER:** `db.drop_all()`, ресет `api_keys`/`users`/`transactions`. **NEVER:** `--no-verify` без явной команды юзера.

### Тесты (на dev-машине)

```bash
DEV_MODE=true APP_ENV=dev JWT_SECRET=test-jwt-secret-32-chars-long-yes \
ALLOWED_ORIGINS=http://localhost:8000 \
python -m pytest tests/ --tb=line
```

299 проходят / 8 skipped (без xhtml2pdf/docx/openpyxl на dev). На проде Python 3.10 — все 307 запускаются.

### Стиль кода и общения

- Ответы юзеру — на русском.
- Комментарии в коде минимальные, только где неочевидно.
- API-ключи — в БД `api_keys`, не в env, не в коде.
- Шрифты — только локально (`views/fonts/` + `views/fonts.css`). Никаких внешних CDN.

### Risky actions

Перед `git reset --hard`, `--force-push`, удалением таблиц, ротацией секретов, отправкой email/Slack/CRM-событий — **спрашивать юзера**, даже если он раньше дал общее «делай как считаешь нужным».

---

## Что делать при каждом новом запуске

1. **Прочитай этот файл.** Если задача очевидно по одной теме — открой соответствующий модуль из карты выше.
2. Открой [TODO_NEXT.md](TODO_NEXT.md) — там 🔴 первостепенная задача + остальное.
3. Если нужна история — открой [HANDOVER.md](HANDOVER.md).
4. `git log --oneline -25` — последние коммиты.
5. Если нужны live-логи прода — попроси юзера `/admin/actions.txt?since_hours=72`.

## ⚙ Прод-доступы (краткое)

- **Прод:** `193.187.92.147` (Москва, HOSTKEY), Ubuntu 22.04, https://aiche.ru
- **SSH:** `ssh -i 'C:\Users\Денис\.ssh\id_ed25519' root@193.187.92.147`
- **PostgreSQL:** `postgresql://aiche:...@localhost:5432/aiche`
- **AI-прокси:** Xray на `127.0.0.1:10809` (для OpenAI/Anthropic/Google/Grok). Perplexity напрямую.
- **Сервис:** `systemctl status ai-che` (4 workers на 127.0.0.1:8000, nginx наружу).

Подробности — в [docs/modules/00-overview.md](docs/modules/00-overview.md) и [docs/modules/20-infra-deploy.md](docs/modules/20-infra-deploy.md).

---

_Последнее обновление структуры: 2026-05-13. Текущее состояние спринтов — в HANDOVER.md._
