-- Удаление таблиц Marketplace (фича снята окончательно, 2026-05-28).
--
-- Безопасно: на проде в этих таблицах 0 строк (фича была отключена
-- через MARKETPLACE_ENABLED=0 с 2026-05-10, никаких новых listings/
-- installs не появлялось). Все ранее установленные шаблоны живут
-- как обычные ChatBot записи и продолжают работать.
--
-- Запуск:
--   psql postgresql://aiche:.../aiche -f scripts/drop_marketplace_tables.sql

BEGIN;

-- Сначала уникальный индекс (если остался)
DROP INDEX IF EXISTS uq_marketplace_paid_install;

-- Связанная таблица первой (FK → listings)
DROP TABLE IF EXISTS bot_marketplace_installs CASCADE;

-- Основная таблица
DROP TABLE IF EXISTS bot_marketplace_listings CASCADE;

COMMIT;

-- Проверка после применения:
--   SELECT to_regclass('public.bot_marketplace_listings');   -- должно вернуть NULL
--   SELECT to_regclass('public.bot_marketplace_installs');   -- должно вернуть NULL
