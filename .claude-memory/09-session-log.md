# 09. Журнал сессии (последняя крупная работа Claude)

Дата записи: апрель 2026
Предыдущий Claude сделал ~30 коммитов за одну большую сессию.

## Запрос пользователя и ход работы

1. **«Изучи проект»** → аудит структуры + git log
2. **«Перед этим изучи весь проект — есть ли проблемы?»** → запустил 3 параллельных Explore-агента (security/bugs/architecture) → составил список 40+ проблем
3. **«Да делай все по очереди»** → закрыл P0/P1/P2 security пачкой:
   - Race condition CH (server/billing.py)
   - Webhook ЮKassa HMAC + IDOR в /payment/confirm
   - IMAP password encryption (server/secrets_crypto.py)
   - Python sandbox AST-валидация
   - Path traversal /sites/hosted
   - OAuth code-exchange (не в URL фрагменте)
   - Утечка БД-сессий → db_session()
   - Lightweight миграции
4. **Прислал OpenAI ключ** → `ssh + sed + systemctl restart` — заменил на проде
5. **«Что дальше по плану»** → UX-аудит 7 модулей 3 параллельных агентов → план 5 направлений
6. **«Делай все шаги по очереди»** → Направление 1 (монетизация):
   - Dashboard расходов
   - Калькулятор стоимости
   - CSV экспорт
   - Low-balance алерты
   - Реферальная страница
7. **Направление 2 (UX)**:
   - Воркфлоу UX (порты, калькулятор, tooltips)
   - Empty states
   - Mobile гамбургер в index
8. **Техдолг**:
   - Rate-limit webhook
   - Welcome/referral bonus reduce
   - API keys monitoring раз в час + email alert
9. **«Давай проверим все на безопасность досконально»** → ещё 4 параллельных security-аудита → +19 уязвимостей → все закрыл (CSV injection, IDOR в solutions/agent, OAuth ATO, SSRF, XXE, CSP для sites, UNIQUE index, JWT strict, MIME magic, PII mask)
10. **«Зачем на будущее, можно сейчас?»** → IMAP key versioning + sandbox iframe + Admin audit log UI
11. **«Вкладка ИИ агенты не работает»** → JS синтакс-ошибка из моего replace_all → починил
12. **«Оркестраторы работают?»** → проверил → нашёл и починил keyword substring bug («фер» → оферте)
13. **«Делай всё что перечислил»** → в конструкторе: копирование блоков, undo/redo, шаблоны, export/import JSON, подсказки — в agents.html и workflow.html
14. **Скрин: белые квадраты, ? не работает, порты обрезаются** → убрал `overflow:auto` + сделал custom resize-handle
15. **«Разберемся с логикой вкладки ИИ агенты»** → объяснил архитектуру (3 сущности, потоки данных)
16. **«Собрал простого, не отвечает в TG»** → диагностика через БД + логи → нашёл что собрал AgentConfig, не ChatBot
17. **«Инструкция для бота френдли»** → на карточке «Мои агенты» добавил action-кнопки (Тест / В Telegram / ✏️ / 🗑) + 2-CTA в wizard step 4 + deployAgentAsBot с синтезом графа из enabled_blocks
18. **Жалобы: клик по карточке открывает мастер, Тест улетает, @username не показан** → убрал onclick с карточки, Тест = модалка, success-модалка после deploy с @username через TG getMe
19. **«/start работает, как дела — временно недоступен»** → живая диагностика → нашёл orchestrator bug (single-downstream без choice) → починил (коммит 762c5c4)
20. **«Работает»** → ✓
21. **«Закинь всё что знаешь в папку для себя в другой ветке»** → эта ветка `claude/memory`

## Что получилось в итоге

- ~30 коммитов в main за сессию
- Все security-P0/P1/P2 закрыты
- 19 security-CVE от 4 параллельных аудитов — закрыты
- Полная перестройка тарификации (единая схема 0.10 ₽/CH с маржой 100-250%)
- UX модули: dashboard, калькулятор, CSV, low-balance, referral stats, wizard 2-CTA
- Воркфлоу: копирование, undo/redo, шаблоны, import/export, auto-resize, tooltips
- Конструктор агентов полностью переработан
- Починены 6+ багов (orchestrator, keyword match, JS syntax, etc.)
- Создана эта память

## Принципы работы которые выработались

1. **Параллельные аудиты через Explore-agents** — самый быстрый способ найти проблемы
2. **Deploy: backup → pull → reset-env → restart → smoke-test** — иначе `.env` слетает
3. **Фронт проверять через `node -e "new vm.Script(...)"`** перед деплоем (иначе JS syntax rolled whole script)
4. **Live-trace воркфлоу через monkey-patch** `_execute_node` — невероятно полезно для диагностики
5. **Пользователь ценит bulk work** — делать пачкой, не по одному, но с понятным планом сверху
6. **Пояснять «почему» а не только «что»** — в commit messages и ответах пользователю
7. **Русский язык** — ВСЕГДА, для этого юзера

## Технические находки

### Openai 1.30.1 + httpx 0.28+ = баг «proxies» kwarg
На проде httpx 0.27.0 → работает. Локально у меня Python 3.14 + свежий httpx → ломается. Не обновлять эти зависимости вместе.

### Python 3.14 + Pydantic V1 warning
`UserWarning: Core Pydantic V1 functionality isn't compatible with Python 3.14` — на проде не критично (Python 3.12).

### SQLite WAL + BEGIN IMMEDIATE
Единственный рабочий паттерн atomicity между uvicorn workers без Redis. Применяется в rate_limit.db и worker_locks.db. Медленно, но работает.

### Telegram getMe для @username
После deploy в `chatbots.py::_auto_setup_channels` вызываем `GET bot{tok}/getMe` → возвращаем username в setup.telegram.{username,url}. Фронт использует для красивого success-модала.

### _execute_workflow gotcha
После orchestrator код отключает «неактивные ветки». Если orchestrator.choice = None → отключит все. Поэтому:
- В orchestrator-ноде при single-downstream: `ctx["orchestrator_choice"] = downstream[0].id`
- В _execute_workflow: `if chosen:` перед skipping logic (safe fallback)

Без обоих слоёв — бот молчит (что и случилось).
