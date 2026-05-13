# Модуль 12 — Marketplace (⏸ ОТКЛЮЧЁН)

> **Состояние:** Marketplace ботов **отключён через feature-flag** на проде (продуктовое решение 2026-05-10). Код и таблицы остались. Включается через `MARKETPLACE_ENABLED=1` в `.env` + restart. UI-ссылки убраны, write-эндпоинты возвращают `410 Gone`.

## Что было

- Юзеры публикуют свои чат-боты как шаблоны → другие юзеры устанавливают.
- Revenue-split **70% автору / 30% платформе**.
- Админ-модерация (`is_approved`).
- Anti-pump для платных листингов (нет повторной установки).
- Atomic UPDATE на rating (`e2a6134`).

## Куда смотреть если включишь обратно

- [server/routes/marketplace.py](server/routes/marketplace.py) — каталог + install + review + admin/approve/reject.
- [views/marketplace.html](views/marketplace.html) — UI каталога.
- Модели: `bot_marketplace_listings`, `bot_marketplace_installs`.
- [views/admin.html](views/admin.html) — раздел «🛍 Marketplace модерация».

## Если решено НЕ возвращать

Лучше **удалить** код и таблицы, чтобы новые чаты не тратили контекст на разбор «а что это и почему отключено». Сейчас feature-flag висит как «недозакрытое решение» — это шум.

Решение по сохранению/удалению — за юзером (продуктовая стратегия).

## Зависимости

- [05-chatbots](05-chatbots.md) — публиковались шаблоны ботов
- [02-billing](02-billing-payments.md) — install биллинг (70/30)
- [19-admin](19-admin.md) — модерация
