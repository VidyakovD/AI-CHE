# 04. Тарификация и биллинг

## Базовая ставка

**1 CH = 0.10 ₽** — хранится в `pricing_settings.ch_to_rub`, НЕ менять без миграции существующих балансов.

Курс USD/RUB берётся с ЦБ РФ (`exchange_rates`, автообновление при старте). В `pricing_settings.usd_to_rub` устаревший (90 ₽) — не используется.

## Model Pricing (CH за 1k токенов)

Таблица `model_pricing`. После миграции (scripts/update_pricing.py):

| model_id | in/1k | out/1k | per_req | min |
|---|---|---|---|---|
| gpt (4o-mini) | 0.3 | 1.0 | 0 | 1 |
| gpt-4o | 5 | 20 | 0 | 3 |
| claude-sonnet | 8 | 30 | 0 | 3 |
| claude-haiku | 2 | 10 | 0 | 1 |
| grok (mini) | 1 | 1.5 | 0 | 1 |
| grok-large | 8 | 30 | 0 | 3 |
| perplexity | 2 | 2 | 0 | 1 |
| perplexity-large | 8 | 30 | 0 | 3 |
| nano (DALL-E) | — | — | 10 | 1 |
| kling | — | — | 250 | 1 |
| kling-pro | — | — | 500 | 1 |
| veo | — | — | 400 | 1 |

Маржа 100–250% над себестоимостью API (Claude Sonnet 4 $3/$15, GPT-4o $2.5/$10).

Claude-дубль (`claude` 0.6/2.8) удалён — был убыточным.

## Фикс-цены модулей (код)

| Модуль | Константа в коде | CH | ~₽ |
|---|---|---|---|
| Чат-сообщение | calculate_cost() | per токены | 0.1–5 |
| Чат-бот ответ | chatbot_engine:_deduct_bot_usage | per токены | 0.1–5 |
| Сайт-чат (1 turn) | sites.py:SPEC_CONVERSATION_CH_COST | 5 | 0.5 |
| Сайт-генерация кода | sites.py:CODE_GEN_CH_COST | 50 | 5 |
| Сайт-доработка | sites.py:CODE_ITER_CH_COST | 25 | 2.5 |
| Презентация/КП | presentations.py:PRES_CH_COST | 50 | 5 |
| AI-агент (сервис) | agent.py:AGENT_SERVICE_COST | 100 | 10 |
| AI-агент (свой ключ) | agent.py:AGENT_OWN_KEY_COST | 5 | 0.5 |
| Бизнес-промпт light | seed_business_prompts.py | 30 | 3 |
| Бизнес-промпт medium | | 50 | 5 |
| Бизнес-промпт heavy | | 100 | 10 |

## Подписки (hardcoded в server/payments.py)

```python
PLANS = {
    "starter": {"price_rub": 590,  "tokens": 7_000},   # 0.084 ₽/CH (−16%)
    "pro":     {"price_rub": 1590, "tokens": 22_000},  # 0.072 ₽/CH (−28%)
    "ultra":   {"price_rub": 4590, "tokens": 75_000},  # 0.061 ₽/CH (−39%)
}
```

На 30 дней. Подписки ВСЕГДА выгоднее разовых пакетов.

## Token Packages (БД, token_packages)

| Name | Tokens | ₽ | ₽/CH |
|---|---|---|---|
| Старт | 1 000 | 99 | 0.099 (−1%) |
| Базовый | 5 000 | 450 | 0.090 (−10%) |
| Про | 20 000 | 1 600 | 0.080 (−20%) |
| Макси | 100 000 | 7 000 | 0.070 (−30%) |

## ⚠️ АТОМАРНЫЙ БИЛЛИНГ — правила

**НИКОГДА** не писать `user.tokens_balance += X` или `-= X` напрямую. Только через `server/billing.py`:

```python
from server.billing import deduct_atomic, deduct_strict, credit_atomic

# Списание "сколько есть, но не в минус" — после AI-ответа
charged = deduct_atomic(db, user_id, cost)  # возвращает min(balance, cost)

# Списание "всё или ничего" — предоплата перед дорогим действием
if not deduct_strict(db, user_id, cost):
    raise HTTPException(402, "Недостаточно токенов")

# Зачисление
credit_atomic(db, user_id, amount)

# После → обычный db.commit()
```

Реализация через SQL `UPDATE ... WHERE tokens_balance >= cost` без read-then-write. Защищает от race condition (lost update при параллельных запросах).

**Low-balance алерты**: `_maybe_send_low_balance_alert` вызывается из `deduct_atomic/strict`. Если баланс упал ниже `user.low_balance_threshold` (по умолч 100 CH) и не слали 24 часа → email с кнопкой «Пополнить».

## Welcome / Referral bonuses

Было ранее 5000 / 10000 CH (= 500/1000 ₽ халявы, фрод-вектор). Уменьшено и вынесено в env:

```
WELCOME_BONUS_CH=500           # при verify_email
REFERRAL_SIGNUP_BONUS=1000     # при регистрации друга по реф-коду
```

Реф-бонус при оплате друга = 10% от suma (payments.py:credit_referral_bonus).

## ЮKassa

- Тестовый shop_id + secret сейчас на проде. Ждёт live от владельца.
- HMAC проверка webhook: `X-Content-Signature` с `sha256=...`, compare_digest.
- Источник истины — `YKP.find_one(payment_id)` через API, а не тело webhook.
- `user_id` в metadata проверяется в `/payment/confirm/{id}` (защита от IDOR, раньше можно было украсть чужой payment_id).
- UNIQUE index на `subscriptions.yookassa_payment_id` — защита от double-spend между `/confirm` и webhook.

## Промокоды

- `promo_codes` + `promo_uses`
- Atomic increment: `UPDATE used_count + 1 WHERE used_count < max_uses` (иначе race condition превышал лимит).
- Один юзер = одно применение (проверка `PromoUse.filter_by(code_id, user_id)`).

## Калькулятор для юзера

Фронт имеет helper:
```js
window.chToRub(50)   // "~5 ₽"
window.formatCH(50)  // "50 CH (~5 ₽)"
```

Читает `ch_to_rub` из `/pricing/settings`, кэширует в localStorage. Применён в chatbots, sites, presentations, index, agents.

В чате под инпутом динамическая подсказка:
«💡 Типичный ответ ≈ 25 CH (~2.5 ₽)» — обновляется при смене модели.

## Dashboard расходов

`GET /user/cabinet/stats` → `spend_by_module` (разбивка по 7 модулям: чат/боты/сайты/преза/агенты/решения/медиа) за 30 дней + `top_expensive` (топ-5 транзакций).

Рендерится в ЛК → вкладка «Расход» в index.html.

## CSV-экспорт транзакций

`GET /user/transactions.csv` — все транзакции юзера. BOM для Excel, кириллица ок. CSV-injection защита через `_csv_safe` (префикс `'` для `= + - @`).
