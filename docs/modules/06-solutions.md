# Модуль 06 — Solutions (Бизнес-решения PRO)

> **Что это:** 40 готовых пилотов с multi-agent оркестрацией. Юзер заполняет 4-8 полей формы → бэкенд гоняет 2-6 стадий (Perplexity → Claude Sonnet → GPT-4o) → отдаёт результат с экспортом в PDF/DOCX/XLSX + share-ссылку + auto-flag на жалобы. Опционально — расписание (cron-пресеты) для регулярных запусков.

## TL;DR для нового чата

- **Где код:** [server/solutions_orchestra.py](server/solutions_orchestra.py) (runtime) + [server/routes/solutions.py](server/routes/solutions.py) (21 endpoint) + [server/routes/schedules.py](server/routes/schedules.py) (cron).
- **Где UI:** [views/index.html](views/index.html) — главная страница, секция «Бизнес-решения», `_currentInputSchema`, `launchSolution`, `runModal`.
- **Где пилоты-промпты:** `scripts/seed_v2_solutions.py` (10 + 5 file-решений) + `scripts/seed_orchestra_solutions.py` (8 deep-orchestra) + `scripts/seed_perplexity_solutions.py` (5 фикс-Perplexity) + остальные в БД через legacy `seed_business_prompts.py`. **Промпты живут в БД, не в коде.**
- **🎉 v2-редизайн ЗАВЕРШЁН (40/40):** все 40 имеют `input_schema_json` + multi-stage pipeline.
- **Биллинг:** `real_cost × pricing.ai.improve_margin_pct (500%)` за каждый llm-stage. Для Perplexity-пилотов с фикс-ценой — списание ДО вызова через `Solution.price_tokens`, audit-warn если real_cost > 70% от fix-price. Курс 95 ₽/$ как буфер.

---

## Архитектура

### 1. Модель данных

**Таблица `solutions`** ([server/models.py](server/models.py)):

| Поле | Что |
|---|---|
| `id`, `title`, `description` | базовое |
| `category`, `subcategory`, `tags` | для chip-фильтра (исследование/маркетинг/продажи/стратегия/юр/финансы/HR) |
| `is_featured`, `short_summary` | UX-поля для карточки (бейджи 🔥 ХИТ, 🆕 NEW, 💎 DEEP, 🤖 PRO) |
| `prompt_hint`, `prompt_template` | legacy plain-режим (один LLM-call) |
| `input_schema_json` | **v2:** массив полей формы (см. ниже) |
| `orchestra_json` | **v2:** граф стадий (см. ниже) |
| `price_tokens` | фикс-цена в копейках (для Perplexity-пилотов) |
| `model` | модель по умолчанию |

**Таблица `solution_runs`** — каждый запуск:

| Поле | Что |
|---|---|
| `user_id`, `solution_id`, `status` | running / done / failed |
| `input_text`, `output_text` | для plain |
| `stages_state` | JSON: состояние каждой стадии (для orchestra + restage) |
| `attachments_json` | загруженные файлы (для file-stages) |
| `public_token` | для share-ссылки `/s/{token}` |
| `user_mark` | 👍 / 👎 / 💡 |
| `compare_group` | для compare-режима (2-3 модели параллельно) |

**Таблица `solution_run_templates`** — сохранённые шаблоны запуска (`⭐ Save as template`).
**Таблица `orchestra_schedules`** — расписания cron-пресетов (см. ниже).

### 2. v2 input_schema_json

Явный массив полей вместо парсинга `prompt_hint`. UI рендерит подходящий control, валидирует `required`.

```json
[
  {"name":"product","label":"Продукт/услуга","type":"text","required":true,
   "hint":"Что вы продаёте?","placeholder":"SaaS для онлайн-курсов"},
  {"name":"goal","label":"Цель","type":"select","required":true,
   "options":[{"value":"meeting","label":"Назначить встречу"},
              {"value":"demo","label":"Демо-показ"}]},
  {"name":"audience_size","type":"number","label":"Размер аудитории"},
  {"name":"description","type":"textarea","rows":4,"label":"Описание"},
  {"name":"competitor_file","type":"file","label":"PDF конкурента",
   "accept":".pdf,.docx"}
]
```

**Типы:** `text` / `textarea` (rows опц) / `select` (options обяз) / `number` / `file` (accept опц).

### 3. v2 orchestra_json — 10 типов стадий

Граф стадий с подстановкой плейсхолдеров. Тип стадии определяется полем `type`:

| Type | Что делает | Биллинг |
|---|---|---|
| `web_search` | Sonar поиск в интернете | real × margin |
| `perplexity_research` | Deep-исследование (sonar/sonar-pro/sonar-reasoning-pro). max_tokens=16k, search_context=high, recency-фильтр | fix-price |
| `browse_url` | Скачать страницу (SSRF-safe) | бесплатно |
| `parallel_browse` | Несколько URL параллельно | бесплатно |
| `extract_urls` | Достать URL'ы из текста | бесплатно |
| `llm` | Одиночный LLM-вызов (Claude/GPT/Haiku) | real × margin |
| `parallel_llm` | Несколько LLM параллельно (например 4 квадранта SWOT) | sum × margin |
| `synthesize` | Финальная сборка из выходов предыдущих стадий | real × margin |
| `file_extract` | Извлечь текст из PDF/DOCX/XLSX (через `attachments_json`) | бесплатно |
| `vision_describe` | Описать картинку через Claude Haiku vision | real × margin |
| `generate_image` | DALL-E 3 / GPT-image | real × margin |

**Реализация switch'а:** [server/solutions_orchestra.py:830, :1021](server/solutions_orchestra.py) (две точки — startup hydration и runtime execution).

### 4. Плейсхолдеры в шаблонах

В `prompt`/`user_prompt` любой стадии:

| Синтаксис | Что подставляется |
|---|---|
| `{field.name}` или `{name}` | Значение поля из `input_schema` |
| `{stage_id.output}` | Результат предыдущей стадии |
| `{stage_id.outputs[i]}` | i-й элемент массива (для `parallel_llm`) |
| `{input}` | Joined «label: value\n…» (legacy) |

Реализация: [server/solutions_orchestra.py:51](server/solutions_orchestra.py) `_resolve_placeholder`. `ctx.fields` парсится из JSON-input как dict в `run_orchestra`.

### 5. Микс провайдеров

- **Perplexity** (`sonar` / `sonar-pro` / `sonar-reasoning-pro`) — свежие факты, цитаты, тренды. **Напрямую с РФ-сервера** (`PERPLEXITY_HTTPS_PROXY=` override).
- **Claude Sonnet 4.6** — длинный анализ, отчёты, сложные структурированные выходы.
- **Claude Haiku 4.5** — быстрые черновики, классификация.
- **GPT-4o** — финальная полировка, лаконичность.
- **parallel_llm** — несколько Claude параллельно.

---

## API endpoints (21 шт)

| Метод | Endpoint | Что |
|---|---|---|
| GET | `/solutions/categories` | Список категорий с count |
| GET | `/solutions` | Список пилотов с фильтрами |
| GET | `/solutions/{id}` | Деталь пилота (включая input_schema) |
| POST | `/solutions/{id}/run` | Запуск (legacy plain или v2) → возвращает `run_id` |
| POST | `/solutions/runs/{run_id}/continue` | Продолжить (диалоговые пилоты) |
| POST | `/solutions/{id}/orchestra/start` | Запуск orchestra-графа |
| POST | `/solutions/{id}/orchestra/start-compare` | Compare на 2-3 моделях параллельно |
| GET | `/solutions/compare/{compare_group}` | Результаты compare |
| GET | `/solutions/runs/my` | Вкладка «🎯 Мои запуски» в кабинете |
| GET | `/solutions/runs/{run_id}` | Деталь run'а |
| GET | `/solutions/runs/{run_id}/stream` | **SSE live-progress** (heartbeat 1s) |
| POST | `/solutions/runs/{run_id}/share` | Сгенерировать `public_token` |
| DELETE | `/solutions/runs/{run_id}/share` | Отозвать share |
| POST | `/solutions/runs/{run_id}/restage` | **↻ Re-run отдельной стадии** с extra_instruction |
| POST | `/solutions/runs/{run_id}/save-template` | ⭐ Сохранить как шаблон |
| GET | `/solutions/templates` | Мои шаблоны |
| DELETE | `/solutions/templates/{id}` | Удалить шаблон |
| POST | `/solutions/templates/{id}/run` | Запуск из шаблона |
| POST | `/solutions/runs/{run_id}/reaction` | 👍/👎/💡 + auto-flag (3+ 👎 за 7 дней → email админу) |
| GET | `/solutions/runs/{run_id}/docx` | 📝 Экспорт DOCX |
| GET | `/solutions/runs/{run_id}/xlsx` | 📊 Экспорт XLSX |

PDF делается через `main.py:/p/.../pdf` (общий с КП).

Public share: `main.py:/s/{token}` — HTML страница без auth.

---

## Расписания (cron-пресеты)

[server/routes/schedules.py](server/routes/schedules.py) — таблица `orchestra_schedules`, 8 пресетов в `VALID_FREQUENCIES`:

- `daily_09` / `daily_18` — каждый день 09:00 / 18:00 UTC
- `weekly_mon_09` / `weekly_mon_18` / `weekly_fri_09` / `weekly_fri_18`
- `monthly_1_09` / `monthly_15_09`

⚠ **Всё в UTC.** Юзер на МСК = UTC+3 → «09:00» = «12:00 МСК».

**Лимит:** `MAX_SCHEDULES_PER_USER = 5`.

**Worker:** [server/scheduler.py](server/scheduler.py) раз в минуту проверяет `next_run_at <= now`, стартует orchestra, считает следующий `_calc_next_run`.

**Endpoints:** `POST /orchestra-schedules` (create) / `GET /orchestra-schedules` (list) / `DELETE /{id}` / `PUT /{id}/toggle`.

---

## Frontend (views/index.html)

- `_currentInputSchema` — глобальный state, ставится в `launchSolution` из `sol.input_schema`.
- `_renderRunInputFields(hint)` — приоритеты:
  1. v2 schema (если есть)
  2. парсер хинта `prompt_hint`
  3. fallback — textarea
- `_collectRunInput()` — для v2 возвращает JSON-stringified dict, валидирует `required`.
- При наличии `input_schema` минуем prompt-editor и orchestra-textarea — всегда форма.
- **UX-фичи:** Live-progress SSE / ↻ restage / ⭐ template / 🔗 share / 📄 PDF / 📝 DOCX / 📊 XLSX / 👍/👎 / 🔬 compare / 📅 schedule.

---

## Биллинг — детали

| Тип | Формула |
|---|---|
| llm / parallel_llm / synthesize | real_cost × `pricing.ai.improve_margin_pct` (default 500%) за каждый stage |
| perplexity_research **в пилоте с фикс-ценой** | списание `Solution.price_tokens` ДО вызова; audit-warn если real > 70% от fix |
| perplexity_research **в обычной orchestra-стадии** | real × margin (как llm) |
| web_search / browse_url / file_extract / extract_urls / vision_describe | реал × margin (vision) или free |
| generate_image | real × margin |

Курс **95 ₽/$** как буфер на колебания.

**Авто-refund** при ошибке в любой стадии (см. `_execute_step` exception path).

---

## Как добавить новый пилот (рецепт)

1. Открой `scripts/seed_v2_solutions.py`, скопируй структуру одного из готовых (например #4 — Скрипт холодного звонка) как образец.
2. Спроектируй `input_schema` (3-7 полей, явные `required` где надо).
3. Спроектируй `orchestra` (где Perplexity для свежих фактов, где Sonnet для анализа, где GPT-4o для polish).
4. Промпты в стиле «ты — консультант уровня X, выдай Y».
5. Финальный stage обязательно `stream: true` для прогресса.
6. Категоризация — `scripts/categorize_solutions.py` + прогнать `--force` если изменил title.
7. Деплой:
   ```bash
   ssh ... "cd /root/AI-CHE && /root/AI-CHE/venv/bin/python scripts/seed_v2_solutions.py"
   ```
8. Проверь UX через `/solutions/{id}` + один реальный запуск.

**Для Perplexity-пилотов с фикс-ценой:**
```bash
ssh ... "cd /root/AI-CHE && /root/AI-CHE/venv/bin/python scripts/seed_perplexity_solutions.py [--update]"
```

**Усиление deep-orchestra пилотов:**
```bash
ssh ... "cd /root/AI-CHE && /root/AI-CHE/venv/bin/python scripts/upgrade_orchestra_perplexity.py [--force]"
```

---

## Известные косяки / TODO

См. [TODO_NEXT.md](TODO_NEXT.md). Актуальное на 2026-05-13:

- **`_abs_path` для `/uploads/*` как URL-path** — закрыто (`aa18470`), но если будут новые file-стадии — проверить.
- **Промпт «Проверка контрагента» (#36)** — переписан под ИП (12-цифровой ИНН) + не отказываться (`f57eb42`).
- **`async def run_solution / continue_run`** — `spawn()` требует event loop (`51529d8`). Не возвращать sync-версию.
- **Перечень всех 40 пилотов** — каталога нет, нужно делать SQL `SELECT id, title FROM solutions ORDER BY id`. _Идея: автогенерируемый `docs/solutions-catalog.md` из БД._

---

## Тесты

- `tests/test_critical_paths.py::TestSolutionsOrchestra` — pipeline + placeholders
- `tests/test_new_features.py` — schedule + reaction + share + restage

```bash
DEV_MODE=true APP_ENV=dev JWT_SECRET=test-jwt-secret-32-chars-long-yes \
ALLOWED_ORIGINS=http://localhost:8000 \
python -m pytest tests/test_critical_paths.py::TestSolutionsOrchestra -v
```

---

## Зависимости от других модулей

- [02-billing-payments](02-billing-payments.md) — `deduct_strict` / `credit_atomic` / `pricing_config`
- [03-ai-core](03-ai-core.md) — `generate_response`, MODEL_REGISTRY, прокси
- [11-knowledge-rag](11-knowledge-rag.md) — RAG для контекста (опционально подключается в стадии)
- [13-public-api](13-public-api.md) — webhook `solution.done` отправляется по завершении
- [14-mcp-server](14-mcp-server.md) — tools `list_solutions` / `run_solution` / `get_solution_status`
- [17-push](17-push.md) — push-уведомление при `done` (если юзер подписался)
