# Модуль 02 — Billing & Payments

> **Что это:** баланс юзера, атомарные списания, динамические цены, ЮKassa с 54-ФЗ чеком. Open когда: меняешь тариф, добавляешь способ оплаты, чинишь race на балансе, дебажишь refund.

## ⚠ ПРАВИЛО №1 — ДЕНЬГИ В КОПЕЙКАХ

- `User.tokens_balance` хранит **копейки** (1 ₽ = 100 коп).
- Legacy имена `tokens_balance`, `tokens_delta`, `ch_per_1k_*` — это копейки, не «токены».
- **Никогда не делай `user.tokens_balance -= X` напрямую.** Только через [server/billing.py](server/billing.py).
- UI всегда форматирует через `window.fmtRub(kop)` → "X.XX ₽".

## TL;DR

- **Код:** [server/billing.py](server/billing.py) + [server/pricing.py](server/pricing.py) + [server/payments.py](server/payments.py) + [server/routes/payments.py](server/routes/payments.py).
- **Атомарные API:** `deduct_strict(user_id, amount_kop, reason)` / `deduct_atomic(...)` / `credit_atomic(...)` / `refund_atomic(...)`.
- **Цены в БД** `pricing_config` (таблица), читаются через `pricing.get("key", default_kop)`. Изменения через `/admin/pricing` UI.
- **Платежи:** ЮKassa с HMAC-webhook + 54-ФЗ чек (payment_subject + tax_system_code + vat_code).

## Атомарность (что важно не сломать)

Все RMW операции должны быть **`UPDATE … SET balance = balance - X WHERE balance >= X AND id = ?`**. Никаких `SELECT → if balance >= X → UPDATE balance = balance - X`. Если строк затронуто 0 — это insufficient funds, кидаем 402.

- ✅ `IdempotencyRecord` UNIQUE(user_id, key) для multi-worker безопасности
- ✅ `yookassa_payment_id` UNIQUE — чтобы webhook не задвоил начисление
- ✅ Авто-refund при ошибке AI-stage / failed-генерации сайта
- ✅ Race на одобрении marketplace (`atomic UPDATE rating`)

## Тарифы (актуально на 2026-05-13)

Все цены в копейках в БД, ключи в `pricing_config`:

| Что | Цена | Pricing-key |
|---|---|---|
| Создание бота с нуля | бесплатно | `bot.scratch_create=0` |
| Бот из шаблона | бесплатно | `bot.template_create=0` |
| AI-конструктор бота | ≥ 1000 ₽ | `bot.ai_create_min=100_000` |
| AI-доработка / правки | real × 5 | `ai.improve_margin_pct=500` |
| Реальные диалоги бота | real × 3 | `ai.reply_margin_pct=300` |
| Edit-block в сайте | real × 5 | `ai.improve_margin_pct=500` |
| Storage файлов (включая RAG) | 50 ₽/мес за 100 МБ | `storage.per_100mb_month=5_000` |
| Сайт Sonnet | 1500 ₽ | `site.standard=150_000` |
| Сайт Opus | 1990 ₽ | `site.premium=199_000` |
| Свой API-ключ юзера | -80% (платит 20%) | `ai.user_key_discount_pct=20` |
| КП первый раз | 50 ₽ | `proposal.create=5_000` |
| КП перегенерация | 5 ₽ | `proposal.edit=500` |
| КП AI-правка секции | real × 5 | `ai.improve_margin_pct=500` |
| Презентация | real × 7 (margin внутри) | `presentation.margin_pct=700` |
| Бизнес-решение orchestra | real × 5 за каждый llm-stage | `ai.improve_margin_pct=500` |
| Voice (Whisper) | 5 ₽ за запрос | фикс |
| TTS | 2.25 ₽ / 1000 симв (мин 50 коп) | фикс |
| Marketplace install | price_kop листинга (70% автору) | per listing |
| `/iterate` сайта | через pricing_config | `site.iter` |

## ЮKassa

- HMAC-проверка webhook'а в [server/routes/payments.py](server/routes/payments.py)
- 54-ФЗ receipt: `payment_subject=service` + `payment_mode=full_payment` + `tax_system_code=2` (УСН) + `vat_code=1`
- В payment-логах **нет сумм** (152-ФЗ best practice)
- UNIQUE-индекс на `yookassa_payment_id` против двойного webhook
- Прод-shop на юзере — ⚠ задача его (не моя)

## Endpoints

| Метод | Endpoint | Что |
|---|---|---|
| POST | `/payment/buy-tokens` | Init заказ → return confirmation_url |
| POST | `/payment/webhook` | YooKassa webhook, HMAC |
| GET | `/payment/confirm-tokens/{id}` | Redirect после оплаты |
| GET | `/admin/pricing` | UI редактирования цен |
| POST | `/admin/pricing/set` | Изменить ключ |
| GET | `/user/transactions.csv` | Экспорт операций (CSV-injection-safe) |

## Модели

| Таблица | Что |
|---|---|
| `users` | tokens_balance (кoп), free balance |
| `transactions` | user_id, tokens_delta, reason, idempotency_key, yookassa_payment_id |
| `pricing_config` | key → value_kop (источник истины) |
| `pricing_settings` | глобальные булевые ключи (включены/нет промо) |
| `model_pricing` | per-model rates |
| `token_packages` | предустановленные пакеты для shop |
| `promo_codes` / `promo_uses` | промо |
| `idempotency_records` | UNIQUE(user_id, key) — multi-worker |

## Гочча

- **Bcrypt и passlib:** pin `bcrypt<5.0` — passlib 1.7.4 incompatible с bcrypt 5.x (`6d70a85`).
- **`PROPOSAL_COST_KOP=5000` хардкод** в [server/routes/proposals.py](server/routes/proposals.py) — игнорит pricing_config. _TODO: читать через pricing.get._
- **Storage billing race fix** — UNION StoredAsset + KnowledgeFile, одна транзакция.

## Тесты

- `tests/test_billing.py` — atomic gates, race conditions, widget Origin
- `tests/test_critical_paths.py` — promo, refund, secrets HKDF

## Зависимости

- [01-core-auth](01-core-auth.md) — `User.tokens_balance` показывается в `/auth/me`
- [03-ai-core](03-ai-core.md) — все llm-вызовы списывают `real × margin`
- [16-storage](16-storage.md) — storage-billing scheduler
- [18-privacy](18-privacy-compliance.md) — отсутствие сумм в логах для 152-ФЗ
