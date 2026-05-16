# Модуль 22 — ИИ Агенты v2 (🔴 ROADMAP, следующий большой спринт)

> **Статус:** ⚪ концепт, реализации НЕТ. Это **следующая главная задача** после Креаторов. В новом чате — открой этот файл первым.

---

## Видение (со слов юзера, 2026-05-15)

**Сейчас ИИ-агенты сделаны как «конструктор» — слишком сложно и непонятно.** Юзер видит белый лист, должен понимать ноды, edges, триггеры, инструменты, контекст. Это работает для разработчиков, не для предпринимателей.

**Хотим:** дружелюбный набор **готовых предобученных ролей**, где сложные процессы — под капотом. Юзер выбирает «Юрист» / «Бухгалтер» / «Креатор» — он сразу работает, без графа. Можно поверх **накидывать скилы** (доп. инструменты, источники).

**Креаторы переезжают сюда** как один из агентов (текущий модуль 21 остаётся как backend-инфра).

**Главная идея — Knowledge Hub:** общая база компании (описание, прайсы, регламенты, контакты) загружается **один раз** в одно место. AI **автоматически нарезает** на семантические чанки и **выбирает релевантные** для каждой задачи. Не тратим кредиты на лишний контекст.

---

## Скоуп v2 (что войдёт)

### 1. Каталог готовых ролей (минимум 6 для MVP)

| Роль | Что делает | Главные tools | Дефолтная модель |
|---|---|---|---|
| 🔍 **Поисковик** | Глубокий research по теме — рынок, конкуренты, тренды, статистика | `perplexity_research` (sonar-pro/reasoning-pro), `web_search`, `browse_url` | `claude-sonnet-4-6` + Perplexity stages |
| 📊 **Парсер** | Парсит конкретные источники (сайты, маркетплейсы, реестры) и складывает структурированный отчёт | `parallel_browse`, `extract_urls`, `file_extract`, `vision_describe` | `claude-haiku` (быстрый) + Sonnet для саммари |
| ⚖️ **Юрист** | Проверка договоров, ИНН/контрагенты, ЕГРЮЛ, судебная практика, шаблоны юр-документов | `perplexity_research` recency=year, `file_extract` (PDF договоров), `web_search` | `claude-sonnet-4-6` |
| 💰 **Бухгалтер** | Считает unit-эконом, формирует P&L, проверяет НДС/УСН, парсит банковские выписки | `file_extract` (XLSX), `code_python` (вычисления) | `claude-sonnet-4-6` |
| 📅 **Креатор** | (нынешний модуль 21 переезжает сюда) Контент-планы, посты, картинки, автопостинг | `generate_image` (Imagen 4 Ultra), Perplexity для трендов, send_tg_message/send_vk_post | `claude-sonnet-4-6` + DALL-E/Imagen |
| 💬 **Автоответчик** | Отвечает клиентам в TG/VK/Avito/MAX/WhatsApp/Widget по знанию о компании + прайсам | RAG из Knowledge Hub, send_*, save_lead | `gpt-4o-mini` для скорости (real-time) |

**Идеи на v3:** SMM-аналитик · Найм/HR · Финансовый стратег · Sales-оператор · Personal Assistant.

### 2. Knowledge Hub — общий контекст компании

Одно место в кабинете «📚 База компании» (можно отдельный модуль или внутри ИИ Агентов).

Юзер загружает:
- **Описание компании** (текстом или PDF brand-guide)
- **Прайсы** (XLSX/CSV/PDF)
- **Регламенты, инструкции, FAQ**
- **Документы юриста** (типовые договоры, реквизиты)
- **Финансовая отчётность** (P&L прошлых периодов)
- **Контакты сотрудников** (для роутинга)
- **Логотип, бренд-кит** (для генерации картинок)

Backend (упрощённо):
- Расширяем существующий `KnowledgeFile` + `KnowledgeChunk` (модуль 11)
- Новое поле `KnowledgeFile.category` — auto-classified: `pricing` / `legal` / `finance` / `brand` / `regulation` / `contacts` / `other`
- Классификация при загрузке: один Haiku-вызов «к какой категории относится этот файл?» (дёшево, ~$0.002)
- Embedding на чанк (как сейчас)

Каждый агент при работе **запрашивает только релевантные категории**:
- Юрист → `legal` + `contacts`
- Бухгалтер → `finance` + `pricing` + `contacts`
- Креатор → `brand` + `pricing` + `regulation`
- Автоответчик → ВСЁ (RAG сам выберет top-k по embedding)

**Win:** юзер загружает один раз. Кредиты экономим — каждый агент не тянет всё в контекст.

### 3. Скилы (доп. модули поверх роли)

После выбора роли можно «накинуть» скилы. Пример для Юриста:
- ☑ Проверка контрагентов через ЕГРЮЛ
- ☑ Анализ судебной практики
- ☐ Подготовка исков (premium)
- ☐ Мониторинг изменений в законах (recurring)

Внутри — это просто доп. tools / стадии в orchestra-pipeline. UI показывает чекбоксы. Backend подставляет в граф.

### 4. Предобучение

«Предобучен» в нашем контексте = жёстко прописанный system_prompt + правильно подобранные tools + хорошие защёлки против AI hallucinations (например, «никогда не давай юр-консультацию без proof из ЕГРЮЛ/судебной базы»).

Промпты живут в seed-файлах (как сейчас Solutions). Обновление role-промпта — pull-request, не админка.

### 5. UX-flow

```
1. Юзер открыл «🤖 ИИ Агенты»
2. Видит карточки 6 ролей (как сейчас Solutions-каталог: иконка, кратко что делает, цена за запрос)
3. Клик «Юрист» → модалка «Что нужно сделать?»
   — input + опц. file upload
   — список скилов с чекбоксами (предлагаются дефолтные)
   — кнопка «Запустить»
4. Stream результата (SSE как Solutions)
5. История запусков → отдельная вкладка
```

Это **очень похоже на Solutions** — переиспользуем UI/orchestra-инфру максимально.

---

## Что выкинуть / спрятать

- **Текущий workflow-конструктор** ([views/workflow.html](views/workflow.html) и [views/agents.html](views/agents.html)) — НЕ удаляем код, прячем за роут `/workflow/advanced` или фичу-флаг. Для разработчиков остаётся.
- **Граф нод/edges** — юзер v2 этого не видит. Pipeline всех ролей — это закладные orchestra_json (как 40 пилотов Solutions), плюс conditional skills.
- **Тип «trigger_*»** для ролей кроме Автоответчика — нет триггеров, только on-demand. Автоответчик использует существующий chatbot-engine для webhook-каналов.

---

## Что переиспользуем (НЕ переписываем)

| Existing | Используем для |
|---|---|
| [server/solutions_orchestra.py](server/solutions_orchestra.py) | Pipeline-runtime, 10 stage-типов, streaming, restage |
| [server/agent_runner.py](server/agent_runner.py) tools | tool_perplexity_research, tool_browse_url, tool_run_llm, tool_send_* |
| [server/knowledge.py](server/knowledge.py) | RAG-инфра — расширим category и filtering |
| [server/cron/creators.py](server/cron/creators.py) | Креатор-агент = wrapper над существующим pipeline |
| [server/chatbot_engine.py](server/chatbot_engine.py) | Автоответчик-агент = wrapper над bot-engine + RAG |
| [server/routes/solutions.py](server/routes/solutions.py) | UI patterns + endpoints для запусков |

---

## Что новое создаём

| Файл | Что |
|---|---|
| `server/models.py`: `AgentRole` | id, slug, title, icon, description, system_prompt, default_skills_json, default_model, base_price_kop, pipeline_json (orchestra), is_active |
| `server/models.py`: `AgentSkill` | id, role_id, slug, title, description, extra_pipeline_json, price_delta_kop, is_premium |
| `server/models.py`: `AgentRun` | user_id, role_id, skills_json, input, output_md, status, cost_kop, created_at — аналог `SolutionRun` но для агентов |
| `server/models.py`: `KnowledgeFile.category` | auto-classified: pricing / legal / finance / brand / regulation / contacts / other (LIGHTWEIGHT_MIGRATIONS) |
| `server/routes/agents_v2.py` | GET /agents/roles · GET /agents/roles/{slug} · POST /agents/roles/{slug}/run · GET /agents/runs/my · GET /agents/runs/{id} · SSE stream |
| `server/agents/roles/` | Папка с seed-промптами и pipeline-JSON для каждой роли |
| `scripts/seed_agent_roles.py` | Сидинг 6 ролей в БД (как scripts/seed_v2_solutions.py) |
| `scripts/seed_agent_skills.py` | Доп. скилы для каждой роли |
| `server/knowledge_classifier.py` | Auto-classify уже загруженных KnowledgeFile по категориям (Haiku one-shot) |
| `views/agents-v2.html` | Новая страница (со временем заменит agents.html для нон-developer юзеров) |

---

## План итераций (черновой, 6 спринтов)

### ✅ Итерация 1 — Knowledge Hub (фундамент) — выкачена 2026-05-15
- ✅ `KnowledgeFile.category` колонка через LIGHTWEIGHT_MIGRATIONS (default `'other'`)
- ✅ `server/knowledge_classifier.py` — Haiku-вызов на upload, ~$0.001/файл, fallback `'other'` при любой ошибке. Поддерживает русские синонимы.
- ✅ `knowledge.add_file()` классифицирует ДО индексации
- ✅ `GET /knowledge?categories=pricing,legal` — фильтр через запятую + `category_counts` в summary для UI
- ✅ `PATCH /knowledge/{file_id}/category` — ручной override
- ✅ UI: pill-фильтр над списком, цветной chip на каждом файле с popup-меню override (7 категорий с эмодзи)
- ✅ `scripts/backfill_knowledge_categories.py` — `--dry-run / --limit / --force`. Запустить один раз после деплоя.
- ✅ 22 unit-теста на `_normalize` + `_build_user_prompt`. Полный suite 321 passed.

**Что НЕ сделано (опц., не блокирует Итерацию 2):**
- Не заведена `pricing_config` запись `knowledge.classify` — пока не биллим юзера за классификацию (это копейки, а добавление в storage-биллинг можно в Итерации 5).
- Backfill на проде НЕ запущен (база пустая — нечего бэкфилить, но скрипт готов).

### Итерация 2 — Каталог ролей + UI (без скилов)

### ✅ Итерация 2 — Каталог ролей + UI (без скилов) — выкачена 2026-05-16
- ✅ Модели `AgentRole` + `AgentRun` (server/models.py). Через shadow-Solution
  (subcategory='_agent_role') реюзаем весь solutions_orchestra без копипасты.
- ✅ Первая роль 🔍 **Поисковик** — 2-stage Perplexity (sonar-reasoning-pro
  broad + sonar-pro focused) → claude-sonnet-4-6 синтез. Цена 200 ₽/запуск.
  Input-schema: тема / фокус / глубина.
- ✅ Страница [views/agents-v2.html](../../views/agents-v2.html) — каталог
  карточек + модалка запуска с динамическим input_schema (text/textarea/select)
  + live SSE-стрим стадий + minimal markdown-renderer для итога. Раздел
  «🕘 Мои запуски» с возможностью открыть прошлый результат.
- ✅ Endpoints `/agents/roles`, `/agents/roles/{slug}`, `POST /agents/roles/{slug}/run`,
  `/agents/runs/my`, `/agents/runs/{id}`, `/agents/runs/{id}/stream` (SSE прокси
  на solutions runtime).
- ✅ Sidebar: «🤖 ИИ Агенты» теперь ведёт на /agents-v2.html (с NEW-бейджем).
  Старый workflow-конструктор переименован в «🛠 Конструктор (advanced)».
- ✅ Shadow-Solutions скрыты из `/solutions` каталога и `/solutions/runs/my`
  через фильтр `subcategory != '_agent_role'`.
- ✅ Засеяно на проде: `role.id=1 shadow.id=42 (Поисковик)`.

**Что НЕ сделано (для Иitre 3):**
- 5 остальных ролей (Парсер / Юрист / Бухгалтер / Креатор / Автоответчик).
- Wiring Knowledge Hub категорий в стадии (default_kb_categories пока не
  подмешивается в context — это Иitre 3 + Иitre 4 для скилов).

### Итерация 3 — 5 остальных ролей
- Парсер · Юрист · Бухгалтер · Креатор · Автоответчик
- Креатор-роль = thin wrapper над существующим creators_planner/prepare/publisher (модуль 21 жив)
- Автоответчик = wrapper над chatbot_engine (модуль 5 жив)

### Итерация 4 — Скилы
- Модель `AgentSkill`
- UI: чекбоксы скилов в модалке запуска
- Backend объединяет pipeline_json роли + extra_pipeline_json выбранных скилов
- Цена = base_price + sum(price_delta скилов)

### Итерация 5 — История + Архив
- Вкладка «🕘 Мои запуски» (как у Solutions)
- Share / PDF / DOCX-экспорт результатов (переиспользуем routes/solutions.py docx/xlsx)
- Reaction 👍/👎/💡 + auto-flag

### Итерация 6 — Скрыть старый конструктор + продакшен
- agents.html / workflow.html → `/workflow/advanced` за feature-flag
- В sidebar: «🤖 ИИ Агенты» ведёт на /agents-v2.html
- Старый код НЕ удалять (разработчики/админы могут зайти руками)
- Документация для юзеров (USER_GUIDE.md обновить)

---

## Открытые вопросы (уточнить у юзера в новом чате)

1. **Тариф:** Pay-per-run для каждой роли (как Solutions сейчас, 50-300 ₽ за запуск) или подписочная модель «безлимит за 1990 ₽/мес»?
2. **Knowledge Hub лимит:** Сколько МБ бесплатно? Сейчас storage 50 ₽/мес за 100 МБ. Оставить как есть или для агентов первые 500 МБ бесплатно?
3. **Автоответчик и старые чат-боты:** автоответчик-агент vs существующий chatbots.html — что для чего? Возможно унифицировать в один UI «🤖 Чат-боты», агенты = их «мозг».
4. **Старые AgentConfig** на проде есть? Если да — мигрировать как есть или предложить юзерам перейти на v2 ролью?
5. **Скил-маркетплейс?** Юзеры могут продавать свои скилы другим? (как marketplace ботов был). Возможно для v3.
6. **Predefined промпты:** Кто их пишет/обновляет? PR в git или админка с UI?

---

## Текущая боль с модулем 21 (Креаторы), которая повлияет на v2

⚠ **Юзер сообщил 2026-05-15:** при создании бренда «Сохранить» не закрывает модалку и список пустой, хотя на бэке бренды реально создаются (5 дубликатов в БД). Зафикшено в коммите `ac2a920` (optimistic update + изолированные try) — но юзер всё равно видел старое поведение, **возможно браузерный кеш JS не сбросился**.

В новом чате при работе над агентами v2 — если будут жалобы на «нажимаю сохранить, ничего не происходит», первым делом проверить:
1. Hard reload (Ctrl+F5) — кеш JS
2. DevTools Console → ищи логи с `[saveBrand]` или `[saveAgent]` префиксом
3. Network tab → статус POST-запроса

---

## Зависимости

- [03-ai-core](03-ai-core.md) — генерация
- [06-solutions](06-solutions.md) — UI/pipeline-инфра как образец
- [10-agents-workflows](10-agents-workflows.md) — current agents (отмирающий конструктор)
- [11-knowledge-rag](11-knowledge-rag.md) — Knowledge Hub фундамент
- [05-chatbots](05-chatbots.md) — Автоответчик = wrapper
- [21-creators-roadmap](21-creators-roadmap.md) — Креатор-роль = wrapper

---

## Промпт для нового чата

> «Делаем модуль ИИ Агенты v2 по [docs/modules/22-agents-v2-roadmap.md](docs/modules/22-agents-v2-roadmap.md). Сначала уточни 6 вопросов из секции «Открытые вопросы». Потом стартуй с Итерация 1 — Knowledge Hub.»
