"""v2 редизайн первых 10 бизнес-решений: input_schema + multi-stage pipeline.

Концепция v2:
  • input_schema — явный массив полей вместо «угадай что юзер должен
    заполнить из хинта». Поля: name, label, type, required, hint, placeholder,
    options (для select).
  • orchestra-pipeline — каждое решение = 2-5 stage'ов с разными провайдерами:
    Perplexity для свежего ресёрча с цитатами, Claude Sonnet для длинного
    анализа, Claude Haiku для быстрых черновиков, GPT-4o для полировки.
  • {field.name} — placeholder в промптах, подставляется значениями полей.
  • {stage_id.output} — ссылка на результат предыдущего stage'а.

Цены НЕ трогаю — оставляю как в БД. Юзер пересчитает после тестов на проде.

Запуск на проде:
  ssh ... "cd /root/AI-CHE && \
    /root/AI-CHE/venv/bin/python -m dotenv -f .env run \
    /root/AI-CHE/venv/bin/python scripts/seed_v2_solutions.py"

Идемпотентен: повторный запуск перезапишет orchestra_json + input_schema_json
тех же 10 решений (по id). Результаты прошлых запусков (SolutionRun) не
затрагиваются.
"""
import json
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.db import SessionLocal
from server.models import Solution


# ════════════════════════════════════════════════════════════════════════════
# SHARED helpers
# ════════════════════════════════════════════════════════════════════════════

# Постфикс к финальному stage'у — единый «формат отчёта» для бизнес-юзера
_REPORT_FORMAT_HINT = """

=== ФОРМАТ ОТВЕТА ===
Markdown-отчёт уровня B2B-консультанта:
- Заголовок документа (#)
- 5-10 содержательных разделов (## H2)
- Подразделы (### H3) где уместно
- Маркированные/нумерованные списки
- Таблицы для сравнений (Markdown table)
- **Жирное** для ключевых тезисов
- Каждый раздел минимум 2-3 абзаца с конкретикой
- В конце: «### 🎯 Ключевые выводы» (5-7 буллетов) и
  «### 📋 Что делать дальше» (пошаговый план)
- Тон: профессиональный, по делу, без воды
- Объём: 2500-5000 слов
"""


# ════════════════════════════════════════════════════════════════════════════
# 10 РЕШЕНИЙ
# ════════════════════════════════════════════════════════════════════════════

V2_SOLUTIONS = {

    # ── #1 Полный SWOT-анализ бизнеса ────────────────────────────────────
    1: {
        "input_schema": [
            {"name": "company", "label": "Название компании / продукта",
             "type": "text", "required": True,
             "placeholder": "ООО «Зелёная грядка»"},
            {"name": "industry", "label": "Ниша / индустрия",
             "type": "text", "required": True,
             "hint": "В какой отрасли работаете",
             "placeholder": "доставка органических продуктов B2C"},
            {"name": "stage", "label": "Стадия бизнеса",
             "type": "select", "required": True,
             "options": [
                 {"value": "idea", "label": "Идея (ещё не запущен)"},
                 {"value": "early", "label": "Стартап (до 1 года)"},
                 {"value": "growth", "label": "Рост (1-3 года, ищем масштаб)"},
                 {"value": "mature", "label": "Зрелый (3+ года, хотим оптимизацию)"},
                 {"value": "pivot", "label": "Кризис / pivot"},
             ]},
            {"name": "current_revenue", "label": "Текущая выручка / план",
             "type": "text", "required": False,
             "placeholder": "например: 800k₽/мес или N/A для идеи"},
            {"name": "key_competitors", "label": "Главные конкуренты",
             "type": "textarea", "required": False, "rows": 2,
             "hint": "2-5 имён через запятую"},
            {"name": "user_advantage", "label": "Что считаете своим преимуществом",
             "type": "textarea", "required": False, "rows": 2},
        ],
        "orchestra": {
            "default_model": "claude-sonnet",
            "input_hint": "Заполните 6 полей про компанию — сделаем глубокий SWOT",
            "stages": [
                {
                    "id": "research",
                    "type": "perplexity_research",
                    "label": "Свежий ресёрч рынка",
                    "model": "sonar-reasoning-pro",
                    "depth": "standard",
                    "query": "Тренды и состояние рынка {field.industry} в России 2025-2026. "
                             "Конкуренты: {field.key_competitors}. "
                             "Угрозы регуляторные/экономические. Возможности роста.",
                    "search_recency_filter": "year",
                },
                {
                    "id": "swot",
                    "type": "parallel_llm",
                    "label": "4 параллельных аналитика (S/W/O/T)",
                    "model": "claude-sonnet-4-6",
                    "queries": [
                        {
                            "query": "STRENGTHS для {field.company} ({field.industry}, стадия {field.stage}, "
                                     "выручка {field.current_revenue}, преимущества: {field.user_advantage}). "
                                     "Учитывай ресёрч: {research.output}. "
                                     "Дай 5-7 сильных сторон в Markdown с обоснованием каждой."
                        },
                        {
                            "query": "WEAKNESSES для {field.company} ({field.industry}, стадия {field.stage}, "
                                     "конкуренты {field.key_competitors}). "
                                     "Учитывай ресёрч: {research.output}. "
                                     "Дай 5-7 слабых сторон в Markdown с обоснованием."
                        },
                        {
                            "query": "OPPORTUNITIES для {field.company} в нише {field.industry}. "
                                     "Учитывай ресёрч: {research.output}. "
                                     "Дай 5-7 возможностей с приоритизацией (high/med/low) в Markdown."
                        },
                        {
                            "query": "THREATS для {field.company} в нише {field.industry}. "
                                     "Учитывай ресёрч: {research.output}. "
                                     "Дай 5-7 угроз с оценкой вероятности (high/med/low) в Markdown."
                        },
                    ],
                },
                {
                    "id": "synthesis",
                    "type": "synthesize",
                    "label": "TOWS-стратегия + приоритеты",
                    "model": "claude-sonnet-4-6",
                    "stream": True,
                    "user_prompt":
                        "Ты — strategy-консультант. На основе SWOT-анализа собери единый отчёт.\n\n"
                        "Компания: {field.company}, ниша {field.industry}, стадия {field.stage}.\n"
                        "Преимущества по версии основателя: {field.user_advantage}\n\n"
                        "SWOT-блоки:\n{swot.outputs}\n\n"
                        "Контекст рынка:\n{research.output}\n\n"
                        "Сделай:\n"
                        "1. Сводный SWOT (4 квадранта в одной табличке)\n"
                        "2. TOWS-кросс-анализ: SO/WO/ST/WT стратегии (по 2 идеи на квадрант)\n"
                        "3. Топ-5 действий на ближайшие 90 дней с обоснованием\n"
                        "4. Главные риски и план их митигации"
                        + _REPORT_FORMAT_HINT,
                },
            ],
            "final_stage": "synthesis",
        },
    },

    # ── #2 90-дневный план запуска продукта ──────────────────────────────
    2: {
        "input_schema": [
            {"name": "product", "label": "Что запускаем",
             "type": "textarea", "required": True, "rows": 2,
             "hint": "Продукт, услуга, проект — 1-2 предложения"},
            {"name": "audience", "label": "Целевая аудитория",
             "type": "textarea", "required": True, "rows": 2,
             "hint": "Кто покупатель: должность/возраст/боли/где обитает"},
            {"name": "goal", "label": "Главная цель за 90 дней",
             "type": "text", "required": True,
             "placeholder": "100 платящих клиентов / 1 млн ₽ MRR / 10к лидов"},
            {"name": "budget", "label": "Маркетинг-бюджет на 90 дней",
             "type": "text", "required": True,
             "placeholder": "300 000 ₽ / без бюджета / гибкий"},
            {"name": "channels_now", "label": "Какие каналы уже работают / есть",
             "type": "textarea", "required": False, "rows": 2,
             "hint": "Сайт, Insta-аккаунт 5к, база email, партнёры — что уже есть"},
            {"name": "team", "label": "Команда",
             "type": "text", "required": False,
             "placeholder": "соло-фаундер / 3 человека / отдел продаж 5"},
        ],
        "orchestra": {
            "default_model": "claude-sonnet",
            "input_hint": "Опишите запуск — получите план на 90 дней с KPI",
            "stages": [
                {
                    "id": "research",
                    "type": "perplexity_research",
                    "label": "Бенчмарки запусков в нише",
                    "model": "sonar",
                    "depth": "quick",
                    "query": "Успешные кейсы запуска продукта похожего на: {field.product}. "
                             "Аудитория: {field.audience}. Каналы привлечения которые работают в РФ 2025.",
                    "search_recency_filter": "year",
                },
                {
                    "id": "plan",
                    "type": "llm",
                    "label": "Помесячный план + KPI",
                    "model": "claude-sonnet-4-6",
                    "stream": True,
                    "user_prompt":
                        "Ты — продакт-маркетолог уровня СЕО-маркетинг-агентства за 500к ₽/мес. "
                        "Сделай 90-дневный go-to-market план.\n\n"
                        "ВВОД:\n"
                        "- Продукт: {field.product}\n"
                        "- ЦА: {field.audience}\n"
                        "- Цель за 90 дней: {field.goal}\n"
                        "- Бюджет: {field.budget}\n"
                        "- Что уже есть: {field.channels_now}\n"
                        "- Команда: {field.team}\n\n"
                        "БЕНЧМАРКИ (учти):\n{research.output}\n\n"
                        "ВЫДАЙ:\n"
                        "1. Месяц 1 (фундамент): неделя 1, 2, 3, 4 — конкретные действия + ответственный + KPI\n"
                        "2. Месяц 2 (активный запуск): то же по неделям\n"
                        "3. Месяц 3 (рост и оптимизация): то же по неделям\n"
                        "4. Каскад KPI (выручка → лиды → трафик → бюджет на канал)\n"
                        "5. Топ-3 риска плана и план Б на каждый\n"
                        "6. Распределение бюджета по каналам с обоснованием"
                        + _REPORT_FORMAT_HINT,
                },
            ],
            "final_stage": "plan",
        },
    },

    # ── #3 Конкурентный анализ ниши ──────────────────────────────────────
    3: {
        "input_schema": [
            {"name": "my_company", "label": "Ваша компания / продукт",
             "type": "text", "required": True},
            {"name": "niche", "label": "Ниша",
             "type": "text", "required": True,
             "placeholder": "онлайн-курсы по UX-дизайну / доставка цветов / B2B SaaS для HR"},
            {"name": "competitors", "label": "Конкуренты для разбора",
             "type": "textarea", "required": True, "rows": 3,
             "hint": "3-5 имён или сайтов через запятую (или построчно)"},
            {"name": "focus", "label": "Что важнее всего узнать",
             "type": "select", "required": True,
             "options": [
                 {"value": "pricing", "label": "Ценообразование и упаковка"},
                 {"value": "marketing", "label": "Каналы привлечения и креативы"},
                 {"value": "product", "label": "Продуктовые фичи и УТП"},
                 {"value": "weak_spots", "label": "Слабые места — где их обходим"},
                 {"value": "all", "label": "Всё сразу (общий разбор)"},
             ]},
        ],
        "orchestra": {
            "default_model": "claude-sonnet",
            "input_hint": "Получите глубокий разбор 3-5 конкурентов",
            "stages": [
                {
                    "id": "research",
                    "type": "perplexity_research",
                    "label": "Глубокий ресёрч конкурентов",
                    "model": "sonar-reasoning-pro",
                    "depth": "deep",
                    "query": "Глубокий разбор конкурентов в нише {field.niche}: {field.competitors}. "
                             "Фокус: {field.focus}. Цены, упаковка, каналы привлечения, отзывы клиентов, "
                             "сильные и слабые стороны. Свежие данные 2025.",
                    "search_recency_filter": "year",
                },
                {
                    "id": "analysis",
                    "type": "llm",
                    "label": "Сводный отчёт + рекомендации",
                    "model": "claude-sonnet-4-6",
                    "stream": True,
                    "user_prompt":
                        "Ты — конкурентный аналитик. На основе ресёрча сделай разбор для {field.my_company}.\n\n"
                        "КОНКУРЕНТЫ: {field.competitors}\n"
                        "НИША: {field.niche}\n"
                        "ФОКУС: {field.focus}\n\n"
                        "ДАННЫЕ (Perplexity research):\n{research.output}\n\n"
                        "СТРУКТУРА ОТЧЁТА:\n"
                        "1. Карточка по каждому конкуренту: позиционирование, цены, ЦА, каналы, фишки\n"
                        "2. Сравнительная матрица (Markdown table) — все по строкам, критерии по столбцам\n"
                        "3. White spaces: где никто из них не играет\n"
                        "4. 5 идей как обойти каждого по {field.focus}\n"
                        "5. Что точно НЕ копировать (типичные ошибки в нише)\n"
                        "6. Топ-3 действия для {field.my_company} прямо сейчас"
                        + _REPORT_FORMAT_HINT,
                },
            ],
            "final_stage": "analysis",
        },
    },

    # ── #4 Скрипт холодного звонка + отработка возражений ────────────────
    4: {
        "input_schema": [
            {"name": "product", "label": "Продукт / услуга",
             "type": "text", "required": True,
             "placeholder": "SaaS для онлайн-школ"},
            {"name": "client", "label": "Целевой клиент",
             "type": "textarea", "required": True, "rows": 2,
             "hint": "Должность, тип компании, ключевые боли"},
            {"name": "avg_check", "label": "Средний чек",
             "type": "text", "required": False,
             "placeholder": "45 000 ₽/мес"},
            {"name": "goal", "label": "Цель звонка",
             "type": "select", "required": True,
             "options": [
                 {"value": "meeting", "label": "Назначить встречу"},
                 {"value": "demo", "label": "Демо продукта"},
                 {"value": "direct_sale", "label": "Прямая продажа"},
                 {"value": "qualify", "label": "Квалификация лида"},
             ]},
        ],
        "orchestra": {
            "default_model": "claude-sonnet",
            "input_hint": "Структурированный скрипт + 7 отработанных возражений",
            "stages": [
                {
                    "id": "objections_research",
                    "type": "perplexity_research",
                    "label": "Типичные возражения в нише",
                    "model": "sonar",
                    "depth": "quick",
                    "query": "Топ возражений ЦА «{field.client}» при покупке «{field.product}» в РФ. "
                             "Цена {field.avg_check}. Что мешает им сказать ДА.",
                    "search_recency_filter": "year",
                },
                {
                    "id": "draft",
                    "type": "llm",
                    "label": "Черновик скрипта",
                    "model": "claude-sonnet-4-6",
                    "user_prompt":
                        "Ты — sales-консультант. Напиши скрипт холодного звонка.\n\n"
                        "Продукт: {field.product}\n"
                        "Клиент: {field.client}\n"
                        "Чек: {field.avg_check}\n"
                        "Цель: {field.goal}\n\n"
                        "Контекст возражений:\n{objections_research.output}\n\n"
                        "СТРУКТУРА:\n"
                        "1. Открытие (5-10 сек) — как зацепить чтобы не бросили\n"
                        "2. Квалификация — 3 короткие вопроса\n"
                        "3. Презентация (30 сек pitch с цифрами)\n"
                        "4. Отработка топ-7 возражений (из ресёрча) — реплика клиента + ответ\n"
                        "5. Закрытие на цель «{field.goal}» с 2-3 запасными формулировками\n"
                        "Тон: профессиональный, без давления, на «вы».",
                },
                {
                    "id": "polish",
                    "type": "llm",
                    "label": "Финальная полировка (GPT-4o)",
                    "model": "gpt-4o",
                    "stream": True,
                    "user_prompt":
                        "Полируй скрипт — убери воду, сделай реплики живыми и короткими. "
                        "Раздели на ясные блоки с заголовками. Добавь временные маркеры (5 сек / 30 сек).\n\n"
                        "Черновик:\n{draft.output}"
                        + _REPORT_FORMAT_HINT,
                },
            ],
            "final_stage": "polish",
        },
    },

    # ── #5 Симулятор жёстких переговоров ─────────────────────────────────
    # Это особый случай — нужна интерактивность. Делаем 1-stage llm,
    # выдающий «первый раунд» + инструкцию что юзер пишет ответ в чате.
    5: {
        "input_schema": [
            {"name": "scenario", "label": "Сценарий переговоров",
             "type": "textarea", "required": True, "rows": 3,
             "hint": "Кратко: что продаёте/обсуждаете, кому"},
            {"name": "ai_role", "label": "Кого играет ИИ",
             "type": "textarea", "required": True, "rows": 2,
             "hint": "Должность, характер, мотивация, типичные тактики этого типа клиента",
             "placeholder": "CFO крупной компании, ставит давление по цене, проверяет ROI каждые 5 минут"},
            {"name": "your_target", "label": "Ваша цель в переговорах",
             "type": "text", "required": True,
             "placeholder": "контракт от 2 млн ₽/год с предоплатой 50%"},
            {"name": "your_walkaway", "label": "Минимально приемлемые условия (BATNA)",
             "type": "text", "required": True,
             "placeholder": "1.2 млн ₽/год без предоплаты"},
            {"name": "intensity", "label": "Уровень жёсткости ИИ",
             "type": "select", "required": True,
             "options": [
                 {"value": "easy", "label": "Лёгкий — мягкий клиент, для разогрева"},
                 {"value": "medium", "label": "Средний — обычный B2B"},
                 {"value": "hard", "label": "Жёсткий — давит, манипулирует"},
                 {"value": "brutal", "label": "Жёсткач — все приёмы pressure-selling против вас"},
             ]},
        ],
        "orchestra": {
            "default_model": "claude-sonnet",
            "input_hint": "ИИ играет вашего самого жёсткого клиента. Тренируйтесь!",
            "stages": [
                {
                    "id": "round1",
                    "type": "llm",
                    "label": "Раунд 1: ИИ начинает переговоры",
                    "model": "claude-sonnet-4-6",
                    "stream": True,
                    "user_prompt":
                        "Ты играешь роль клиента в учебной симуляции переговоров.\n\n"
                        "СЦЕНАРИЙ: {field.scenario}\n"
                        "ТВОЯ РОЛЬ: {field.ai_role}\n"
                        "УРОВЕНЬ ЖЁСТКОСТИ: {field.intensity} — соответствуй ему ВО ВСЁМ.\n\n"
                        "ЦЕЛЬ ПОЛЬЗОВАТЕЛЯ (но ты не знаешь её): {field.your_target}\n"
                        "BATNA пользователя: {field.your_walkaway}\n\n"
                        "ИНСТРУКЦИЯ:\n"
                        "1. Начни переговоры. Открой как реальный клиент — без раскрытия что это симуляция.\n"
                        "2. Используй типичные тактики своей роли: давление, перебивание, "
                        "ссылки на конкурентов, занижение цены, пробивание скидок.\n"
                        "3. Пользователь ответит — ты отреагируешь как реальный клиент.\n"
                        "4. После своей реплики добавь невидимую для роли ремарку курсивом "
                        "*[Тренер: совет — что делать дальше]* — это поможет пользователю обучаться.\n\n"
                        "Открой переговоры сейчас. Будь в роли с первого слова.",
                },
            ],
            "final_stage": "round1",
        },
    },

    # ── #6 Коммерческое предложение, которое читают ──────────────────────
    6: {
        "input_schema": [
            {"name": "product", "label": "Что продаёте",
             "type": "text", "required": True,
             "placeholder": "Внедрение CRM Битрикс24 под ключ"},
            {"name": "client_name", "label": "Имя клиента / компания",
             "type": "text", "required": True},
            {"name": "client_problem", "label": "Боль клиента",
             "type": "textarea", "required": True, "rows": 2,
             "hint": "Что у клиента НЕ работает что мы решаем"},
            {"name": "your_solution", "label": "Ваше решение",
             "type": "textarea", "required": True, "rows": 3,
             "hint": "Что конкретно делаем — функции/этапы/состав"},
            {"name": "price_block", "label": "Цена / условия",
             "type": "textarea", "required": True, "rows": 2,
             "placeholder": "180 000 ₽ под ключ + 18 000 ₽/мес поддержка, гарантия 6 мес"},
            {"name": "social_proof", "label": "Доказательства (опц.)",
             "type": "textarea", "required": False, "rows": 2,
             "hint": "Кейсы, отзывы, цифры, награды"},
            {"name": "urgency", "label": "Призыв и срочность",
             "type": "text", "required": False,
             "placeholder": "Скидка 15% до конца месяца / бесплатный аудит при заказе до пятницы"},
        ],
        "orchestra": {
            "default_model": "claude-sonnet",
            "input_hint": "Структурированное КП по принципу боль → решение → доказательства → действие",
            "stages": [
                {
                    "id": "draft",
                    "type": "llm",
                    "label": "Черновик КП",
                    "model": "claude-sonnet-4-6",
                    "user_prompt":
                        "Ты — копирайтер уровня агентства за 200к ₽/КП. Напиши коммерческое "
                        "предложение для конкретного клиента.\n\n"
                        "Продукт: {field.product}\n"
                        "Клиент: {field.client_name}\n"
                        "Его боль: {field.client_problem}\n"
                        "Решение: {field.your_solution}\n"
                        "Цена: {field.price_block}\n"
                        "Доказательства: {field.social_proof}\n"
                        "Срочность: {field.urgency}\n\n"
                        "СТРУКТУРА (формула AIDA + БРД-БДВ):\n"
                        "1. Заголовок (про БОЛЬ клиента, не про себя)\n"
                        "2. Личное обращение (1-2 фразы)\n"
                        "3. Понимание боли — покажи что слышишь клиента\n"
                        "4. Решение — что конкретно делаем (структурно)\n"
                        "5. Доказательства — кейсы, цифры, отзывы\n"
                        "6. Цена и условия — прозрачно, без сюрпризов\n"
                        "7. Призыв к действию + срочность\n"
                        "8. P.S. (главный аргумент дублируем)\n\n"
                        "Тон: уверенный, конкретный, без воды.",
                },
                {
                    "id": "polish",
                    "type": "llm",
                    "label": "Полировка заголовков (GPT-4o)",
                    "model": "gpt-4o",
                    "stream": True,
                    "user_prompt":
                        "Доведи КП до публикации. Особенно — переделай заголовок и подзаголовки "
                        "так чтобы их хотелось читать. Убери штампы («индивидуальный подход», "
                        "«гибкие условия»). Добавь конкретные цифры где их нет.\n\n"
                        "Черновик:\n{draft.output}"
                        + _REPORT_FORMAT_HINT,
                },
            ],
            "final_stage": "polish",
        },
    },

    # ── #7 Контент-план на месяц для соцсетей ────────────────────────────
    7: {
        "input_schema": [
            {"name": "niche", "label": "Ниша / о чём аккаунт",
             "type": "textarea", "required": True, "rows": 2},
            {"name": "audience", "label": "Целевая аудитория",
             "type": "textarea", "required": True, "rows": 2,
             "hint": "Возраст, интересы, на каком языке говорят"},
            {"name": "channels", "label": "Каналы (через запятую)",
             "type": "text", "required": True,
             "placeholder": "Telegram, VK, Instagram, YouTube Shorts"},
            {"name": "goal", "label": "Цель месяца",
             "type": "select", "required": True,
             "options": [
                 {"value": "awareness", "label": "Узнаваемость (рост охвата/подписчиков)"},
                 {"value": "engagement", "label": "Вовлечённость (комментарии/лайки/share)"},
                 {"value": "leads", "label": "Лиды (заявки в DM)"},
                 {"value": "sales", "label": "Продажи (трафик на чек-аут)"},
             ]},
            {"name": "tone", "label": "Tone of voice",
             "type": "text", "required": False,
             "placeholder": "экспертный без снобства / дружелюбный / провокационный"},
        ],
        "orchestra": {
            "default_model": "claude-sonnet",
            "input_hint": "30 дней контента под ваши каналы и цели — с разбивкой по форматам",
            "stages": [
                {
                    "id": "trends",
                    "type": "perplexity_research",
                    "label": "Тренды контента в нише",
                    "model": "sonar",
                    "depth": "standard",
                    "query": "Какой контент сейчас залетает в нише {field.niche} в РФ. "
                             "Тренды форматов (Reels/Shorts/посты), тематик, хэштегов 2025. "
                             "Что делают топовые блогеры и бренды в этой нише.",
                    "search_recency_filter": "month",
                },
                {
                    "id": "calendar",
                    "type": "llm",
                    "label": "Календарь 30 дней",
                    "model": "claude-sonnet-4-6",
                    "stream": True,
                    "user_prompt":
                        "Ты — SMM-стратег. Сделай контент-план на 30 дней.\n\n"
                        "ВВОД:\n"
                        "- Ниша: {field.niche}\n"
                        "- ЦА: {field.audience}\n"
                        "- Каналы: {field.channels}\n"
                        "- Цель месяца: {field.goal}\n"
                        "- Тон: {field.tone}\n\n"
                        "ТРЕНДЫ (учти):\n{trends.output}\n\n"
                        "ВЫДАЙ:\n"
                        "1. Контент-rubric'и (5-7 рубрик с обоснованием)\n"
                        "2. Календарь по дням (Markdown table: дата | канал | формат | тема | хук | "
                        "CTA). Минимум 30 строк.\n"
                        "3. Конверсионные посты для цели «{field.goal}» — отметь их в календаре\n"
                        "4. Идеи для UGC и коллабораций (5 штук)\n"
                        "5. KPI каждой недели (что измеряем)"
                        + _REPORT_FORMAT_HINT,
                },
            ],
            "final_stage": "calendar",
        },
    },

    # ── #8 Email-цепочка прогрева из 7 писем ─────────────────────────────
    8: {
        "input_schema": [
            {"name": "product", "label": "Продукт / услуга",
             "type": "text", "required": True},
            {"name": "audience", "label": "Кому шлём",
             "type": "textarea", "required": True, "rows": 2,
             "hint": "Это новые подписчики? Холодная база? Тёплые лиды?"},
            {"name": "problem_solved", "label": "Какую боль решает продукт",
             "type": "textarea", "required": True, "rows": 2},
            {"name": "social_proof", "label": "Социальные доказательства",
             "type": "textarea", "required": False, "rows": 2,
             "hint": "Кейсы, отзывы, цифры — что вставляем в письма"},
            {"name": "offer", "label": "Главный оффер (что предложим в финале)",
             "type": "textarea", "required": True, "rows": 2,
             "placeholder": "Бесплатная консультация / скидка 30% / чек-лист в обмен на email"},
            {"name": "frequency", "label": "Частота отправки",
             "type": "select", "required": True,
             "options": [
                 {"value": "daily", "label": "Ежедневно (агрессивный прогрев, 7 дней)"},
                 {"value": "every_other", "label": "Через день (14 дней)"},
                 {"value": "twice_week", "label": "2 раза в неделю (3-4 недели)"},
                 {"value": "weekly", "label": "Раз в неделю (7 недель)"},
             ]},
        ],
        "orchestra": {
            "default_model": "claude-sonnet",
            "input_hint": "7 писем по проверенным формулам прогрева с теми же CTA",
            "stages": [
                {
                    "id": "structure",
                    "type": "llm",
                    "label": "Стратегия 7 писем",
                    "model": "claude-haiku-4-5-20251001",
                    "user_prompt":
                        "Спроектируй стратегию серии 7 писем для прогрева.\n\n"
                        "Продукт: {field.product}\n"
                        "Аудитория: {field.audience}\n"
                        "Боль: {field.problem_solved}\n"
                        "Финальный оффер: {field.offer}\n"
                        "Частота: {field.frequency}\n\n"
                        "Выдай для каждого из 7 писем (одной табличкой):\n"
                        "номер | цель письма | главный месседж | CTA | hook subject\n\n"
                        "Принципы: первое — знакомство, 2-3 — value-bombs, 4-5 — кейсы и доказательства, "
                        "6 — откровение/история, 7 — оффер и закрытие.",
                },
                {
                    "id": "letters",
                    "type": "llm",
                    "label": "Тексты всех 7 писем",
                    "model": "claude-sonnet-4-6",
                    "stream": True,
                    "user_prompt":
                        "На основе структуры напиши ПОЛНЫЕ ТЕКСТЫ 7 писем.\n\n"
                        "Структура:\n{structure.output}\n\n"
                        "Контекст:\n"
                        "- Продукт: {field.product}\n"
                        "- ЦА: {field.audience}\n"
                        "- Боль: {field.problem_solved}\n"
                        "- Соц-пруфы: {field.social_proof}\n"
                        "- Оффер: {field.offer}\n\n"
                        "Каждое письмо:\n"
                        "## Письмо N — [Цель]\n"
                        "**Subject:** ...\n"
                        "**Preview-text:** ...\n\n"
                        "Здравствуйте, ...\n\n"
                        "[Тело письма — 200-400 слов, личный тон, без воды]\n\n"
                        "**CTA:** ...\n"
                        "С уважением, ...\n"
                        "P.S. ...\n\n"
                        "---\n\n"
                        "Тон: личный, как от знакомого эксперта. Каждое письмо начинается "
                        "с истории/факта/вопроса, не с продажи."
                        + _REPORT_FORMAT_HINT,
                },
            ],
            "final_stage": "letters",
        },
    },

    # ── #9 Заголовки для лендинга по 6 формулам ──────────────────────────
    9: {
        "input_schema": [
            {"name": "product", "label": "Продукт",
             "type": "text", "required": True},
            {"name": "audience", "label": "Кому продаём",
             "type": "textarea", "required": True, "rows": 2},
            {"name": "main_pain", "label": "Главная боль",
             "type": "text", "required": True,
             "placeholder": "теряют деньги на неэффективной рекламе / тратят 4 часа на отчёт"},
            {"name": "main_value", "label": "Главное обещание",
             "type": "text", "required": True,
             "placeholder": "ROAS 5x за 30 дней / отчёт за 5 минут вместо 4 часов"},
            {"name": "stage", "label": "Стадия осознанности ЦА",
             "type": "select", "required": True,
             "options": [
                 {"value": "unaware", "label": "Не осознают проблему"},
                 {"value": "problem_aware", "label": "Знают про проблему, не ищут решение"},
                 {"value": "solution_aware", "label": "Ищут решение"},
                 {"value": "product_aware", "label": "Знают про продукт, выбирают"},
                 {"value": "ready", "label": "Готовы купить, нужен последний толчок"},
             ]},
        ],
        "orchestra": {
            "default_model": "claude-sonnet",
            "input_hint": "20 заголовков под вашу ЦА по 6 проверенным формулам",
            "stages": [
                {
                    "id": "trends",
                    "type": "perplexity_research",
                    "label": "Топ-заголовки в нише сейчас",
                    "model": "sonar",
                    "depth": "quick",
                    "query": "Лучшие заголовки на лендингах в нише похожей на «{field.product}» "
                             "для аудитории «{field.audience}» в РФ. Какие конверсят. Свежие 2025.",
                    "search_recency_filter": "year",
                },
                {
                    "id": "headlines",
                    "type": "llm",
                    "label": "20 заголовков по формулам",
                    "model": "claude-sonnet-4-6",
                    "stream": True,
                    "user_prompt":
                        "Ты — копирайтер landing-pages. Сделай 20 заголовков по 6 формулам.\n\n"
                        "Продукт: {field.product}\n"
                        "ЦА: {field.audience}\n"
                        "Боль: {field.main_pain}\n"
                        "Обещание: {field.main_value}\n"
                        "Стадия осознанности: {field.stage}\n\n"
                        "Контекст рынка:\n{trends.output}\n\n"
                        "ФОРМУЛЫ (по 3-4 заголовка на каждую):\n"
                        "1. **Боль-Решение** — «Устали от X? Получите Y без Z»\n"
                        "2. **Цифры и сроки** — «X результат за Y дней»\n"
                        "3. **Гарантия результата** — «Y или вернём деньги»\n"
                        "4. **Социальное доказательство** — «N компаний уже...»\n"
                        "5. **Любопытство-разрыв шаблона** — «Почему ваш X не работает (и что делать)»\n"
                        "6. **Идентификация ЦА** — «Для тех, кто хочет X не делая Y»\n\n"
                        "Каждый заголовок: текст + 1-2 предложения подзаголовка + почему сработает "
                        "для стадии «{field.stage}»."
                        + _REPORT_FORMAT_HINT,
                },
            ],
            "final_stage": "headlines",
        },
    },

    # ── #11 Вакансия, которая привлекает лучших ──────────────────────────
    11: {
        "input_schema": [
            {"name": "position", "label": "Должность",
             "type": "text", "required": True,
             "placeholder": "Senior Backend Developer / Руководитель отдела продаж"},
            {"name": "level", "label": "Уровень",
             "type": "select", "required": True,
             "options": [
                 {"value": "junior", "label": "Junior (до 1 года)"},
                 {"value": "middle", "label": "Middle (1-3 года)"},
                 {"value": "senior", "label": "Senior (3-7 лет)"},
                 {"value": "lead", "label": "Lead / Head (7+ лет)"},
                 {"value": "exec", "label": "C-level / директор"},
             ]},
            {"name": "format", "label": "Формат работы",
             "type": "select", "required": True,
             "options": [
                 {"value": "office", "label": "Офис"},
                 {"value": "remote", "label": "Полная удалёнка"},
                 {"value": "hybrid", "label": "Гибрид (3+2 / 2+3)"},
                 {"value": "flexible", "label": "Гибкий (как удобно)"},
             ]},
            {"name": "location", "label": "Локация / часовой пояс",
             "type": "text", "required": False,
             "placeholder": "Москва / Санкт-Петербург / РФ+СНГ / любая"},
            {"name": "salary", "label": "Зарплата (вилка)",
             "type": "text", "required": True,
             "placeholder": "200-350к ₽ gross / договорная по результатам"},
            {"name": "responsibilities", "label": "Ключевые задачи",
             "type": "textarea", "required": True, "rows": 3,
             "hint": "3-7 главных обязанностей которые занимают 80% времени"},
            {"name": "must_have", "label": "Обязательные навыки",
             "type": "textarea", "required": True, "rows": 2},
            {"name": "company_pitch", "label": "Что вы предлагаете кандидату",
             "type": "textarea", "required": True, "rows": 2,
             "hint": "Не «дружный коллектив»! Конкретные плюшки: ESOP, обучение, "
                     "интересные задачи, бренд-имя, рост, что-то уникальное"},
        ],
        "orchestra": {
            "default_model": "claude-sonnet",
            "input_hint": "Не шаблонная вакансия — учитываем что ищут лучшие в этой роли",
            "stages": [
                {
                    "id": "research",
                    "type": "perplexity_research",
                    "label": "Что предлагают топ-компании на эту роль",
                    "model": "sonar",
                    "depth": "quick",
                    "query": "Лучшие вакансии «{field.position}» уровня {field.level} в РФ 2025. "
                             "Что предлагают топ-компании, какие плюшки, как описывают задачи. "
                             "Чего ожидают кандидаты этого уровня сейчас.",
                    "search_recency_filter": "year",
                },
                {
                    "id": "vacancy",
                    "type": "llm",
                    "label": "Вакансия которая зацепит",
                    "model": "claude-sonnet-4-6",
                    "stream": True,
                    "user_prompt":
                        "Ты — HR-маркетолог уровня Яндекса. Напиши вакансию которая выделяется на hh.\n\n"
                        "Должность: {field.position} ({field.level})\n"
                        "Формат: {field.format}, локация {field.location}\n"
                        "Зарплата: {field.salary}\n"
                        "Задачи: {field.responsibilities}\n"
                        "Must-have: {field.must_have}\n"
                        "Что предлагаем: {field.company_pitch}\n\n"
                        "БЕНЧМАРК (что у других):\n{research.output}\n\n"
                        "СТРУКТУРА:\n"
                        "1. Заголовок-крючок (не «Требуется...»)\n"
                        "2. О нас в 3 предложениях — без воды, только конкретика\n"
                        "3. Чем интересна именно эта роль (вызов, рост, влияние)\n"
                        "4. Что делать (задачи списком, в активном залоге)\n"
                        "5. Что важно уметь (must-have + nice-to-have)\n"
                        "6. Что предлагаем (конкретные плюшки + цифры)\n"
                        "7. Условия (ЗП, формат, переезд если нужен)\n"
                        "8. Как откликнуться (что приложить, кому писать)\n\n"
                        "БАН-СЛОВА: «дружный коллектив», «динамично развивающаяся», «индивидуальный подход», "
                        "«стрессоустойчивость», «многозадачность», «коммуникабельность»."
                        + _REPORT_FORMAT_HINT,
                },
            ],
            "final_stage": "vacancy",
        },
    },

    # ── #12 Система мотивации отдела продаж ───────────────────────────────
    12: {
        "input_schema": [
            {"name": "team_size", "label": "Размер команды продаж",
             "type": "text", "required": True,
             "placeholder": "5 менеджеров + 1 РОП"},
            {"name": "business_model", "label": "Модель бизнеса",
             "type": "select", "required": True,
             "options": [
                 {"value": "b2b_long", "label": "B2B длинный цикл (1-6 мес)"},
                 {"value": "b2b_short", "label": "B2B короткий цикл (до месяца)"},
                 {"value": "b2c_high", "label": "B2C высокий чек (>50к₽)"},
                 {"value": "b2c_low", "label": "B2C низкий чек (<10к₽)"},
                 {"value": "saas", "label": "SaaS (подписка)"},
                 {"value": "marketplace", "label": "Маркетплейс / агрегатор"},
             ]},
            {"name": "avg_check", "label": "Средний чек / ARPU",
             "type": "text", "required": True,
             "placeholder": "85 000 ₽ за сделку / 4 500 ₽/мес подписка"},
            {"name": "current_motivation", "label": "Текущая схема мотивации",
             "type": "textarea", "required": True, "rows": 2,
             "hint": "Оклад + % / только %, какие KPI и бонусы сейчас"},
            {"name": "problems", "label": "Что не работает",
             "type": "textarea", "required": True, "rows": 2,
             "hint": "Текучка / не выполняют план / уходят в конкуренты / низкая активность"},
            {"name": "revenue_target", "label": "Цель по выручке на 3-6 мес",
             "type": "text", "required": True,
             "placeholder": "+40% выручки / удвоить новых клиентов"},
        ],
        "orchestra": {
            "default_model": "claude-sonnet",
            "input_hint": "Индивидуальная схема: KPI, проценты, бонусы, нематериальная часть",
            "stages": [
                {
                    "id": "research",
                    "type": "perplexity_research",
                    "label": "Лучшие практики мотивации в РФ",
                    "model": "sonar",
                    "depth": "standard",
                    "query": "Эффективные схемы мотивации отдела продаж в {field.business_model} "
                             "в РФ 2025. Какие KPI, проценты, бонусы. Что работает у топовых компаний.",
                    "search_recency_filter": "year",
                },
                {
                    "id": "system",
                    "type": "llm",
                    "label": "Система мотивации",
                    "model": "claude-sonnet-4-6",
                    "stream": True,
                    "user_prompt":
                        "Ты — Sales-консультант с опытом построения систем мотивации в десятках компаний.\n\n"
                        "ВВОД:\n"
                        "- Команда: {field.team_size}\n"
                        "- Модель: {field.business_model}\n"
                        "- Средний чек: {field.avg_check}\n"
                        "- Текущая схема: {field.current_motivation}\n"
                        "- Проблемы: {field.problems}\n"
                        "- Цель: {field.revenue_target}\n\n"
                        "ИСТОЧНИКИ:\n{research.output}\n\n"
                        "СТРУКТУРА:\n"
                        "1. Диагноз текущих проблем (почему не работает)\n"
                        "2. Новая схема — структура (оклад / % / бонусы / KPI), с конкретными %%\n"
                        "3. KPI-каскад: метрики менеджера, РОПа, отдела\n"
                        "4. Сетка грейдов (junior/middle/senior внутри отдела) с разной мотивацией\n"
                        "5. Нематериальная мотивация: рейтинги, обучение, статусы, визуализация\n"
                        "6. Антифрод: что нельзя оптимизировать (защита от накрутки)\n"
                        "7. План внедрения (3 шага по неделям) и риски"
                        + _REPORT_FORMAT_HINT,
                },
            ],
            "final_stage": "system",
        },
    },

    # ── #13 Онбординг нового сотрудника: первые 30 дней ──────────────────
    13: {
        "input_schema": [
            {"name": "position", "label": "Должность",
             "type": "text", "required": True},
            {"name": "team", "label": "Команда / отдел",
             "type": "text", "required": True,
             "placeholder": "Отдел разработки 8 человек, 2 тимлида"},
            {"name": "key_skills", "label": "Ключевые навыки/инструменты для роли",
             "type": "textarea", "required": True, "rows": 2,
             "hint": "Что должен уметь делать самостоятельно к концу 30 дней"},
            {"name": "tools", "label": "Внутренние инструменты/системы",
             "type": "textarea", "required": False, "rows": 2,
             "hint": "CRM, Jira, Confluence, внутренние API, особые регламенты"},
            {"name": "success_criteria", "label": "Критерий успеха онбординга",
             "type": "textarea", "required": True, "rows": 2,
             "hint": "Что должен сделать чтобы мы поняли «всё ОК, прошёл испытательный»"},
            {"name": "buddy", "label": "Есть ли наставник/buddy",
             "type": "select", "required": True,
             "options": [
                 {"value": "yes", "label": "Да, выделен наставник"},
                 {"value": "no", "label": "Нет, новичок сам"},
                 {"value": "lead", "label": "Тимлид/руководитель"},
             ]},
        ],
        "orchestra": {
            "default_model": "claude-sonnet",
            "input_hint": "Структурированный план первых 30 дней — день за днём",
            "stages": [
                {
                    "id": "plan",
                    "type": "llm",
                    "label": "План онбординга",
                    "model": "claude-sonnet-4-6",
                    "stream": True,
                    "user_prompt":
                        "Ты — HR-эксперт по онбордингу. Сделай план первых 30 дней.\n\n"
                        "Должность: {field.position}\n"
                        "Команда: {field.team}\n"
                        "Навыки/инструменты: {field.key_skills}\n"
                        "Системы: {field.tools}\n"
                        "Критерий успеха: {field.success_criteria}\n"
                        "Наставник: {field.buddy}\n\n"
                        "СТРУКТУРА:\n"
                        "1. День 1 — пошагово (welcome, документы, доступы, знакомство)\n"
                        "2. Неделя 1 (дни 2-5) — фокус на onboarding-задачах + теория\n"
                        "3. Неделя 2 — первые реальные задачи под надзором\n"
                        "4. Неделя 3 — самостоятельная работа с проверкой\n"
                        "5. Неделя 4 — финальная аттестация + обратная связь\n"
                        "6. Чек-лист для buddy (что проверять)\n"
                        "7. Чек-лист для новичка (что должен сделать)\n"
                        "8. Маркеры тревоги: что значит «онбординг идёт плохо»"
                        + _REPORT_FORMAT_HINT,
                },
            ],
            "final_stage": "plan",
        },
    },

    # ── #14 Unit-экономика: считаем прибыльность ─────────────────────────
    14: {
        "input_schema": [
            {"name": "business", "label": "Что продаёте",
             "type": "textarea", "required": True, "rows": 2,
             "placeholder": "SaaS для онлайн-школ, абонплата 4500₽/мес"},
            {"name": "model", "label": "Модель",
             "type": "select", "required": True,
             "options": [
                 {"value": "subscription", "label": "Подписка (SaaS, мембершип)"},
                 {"value": "transaction", "label": "Разовые продажи (e-com, услуги)"},
                 {"value": "marketplace", "label": "Маркетплейс (комиссия с GMV)"},
                 {"value": "freemium", "label": "Freemium с upsell"},
                 {"value": "ads", "label": "Реклама / медиа"},
             ]},
            {"name": "arpu", "label": "Средний чек или ARPU",
             "type": "text", "required": True,
             "placeholder": "4500 ₽/мес / 12 000 ₽ за сделку"},
            {"name": "cogs", "label": "COGS на одного клиента",
             "type": "text", "required": True,
             "hint": "Прямая себестоимость обслуживания одного клиента (без накладных)",
             "placeholder": "800 ₽/мес (хостинг, поддержка) / 4500 ₽ за сделку (закупка)"},
            {"name": "cac", "label": "CAC — стоимость привлечения",
             "type": "text", "required": True,
             "placeholder": "8 000 ₽ за платящего"},
            {"name": "retention", "label": "Retention / срок жизни",
             "type": "text", "required": True,
             "hint": "Для подписки — % сохраняется через 12 мес. Для транзакций — кол-во повторных покупок",
             "placeholder": "70% через год / в среднем 2.5 покупки за жизнь"},
            {"name": "fixed", "label": "Фикс-расходы в месяц",
             "type": "text", "required": True,
             "placeholder": "350 000 ₽ (зарплаты, аренда, инструменты)"},
        ],
        "orchestra": {
            "default_model": "claude-sonnet",
            "input_hint": "CM1, CM2, LTV, payback, breakeven — со всеми выводами",
            "stages": [
                {
                    "id": "calc",
                    "type": "llm",
                    "label": "Полный расчёт + анализ",
                    "model": "claude-sonnet-4-6",
                    "stream": True,
                    "user_prompt":
                        "Ты — финансовый аналитик уровня PwC. Посчитай unit-экономику.\n\n"
                        "ВВОД:\n"
                        "- Бизнес: {field.business}\n"
                        "- Модель: {field.model}\n"
                        "- ARPU/чек: {field.arpu}\n"
                        "- COGS: {field.cogs}\n"
                        "- CAC: {field.cac}\n"
                        "- Retention: {field.retention}\n"
                        "- Фикс-расходы/мес: {field.fixed}\n\n"
                        "ВЫДАЙ:\n"
                        "1. Расчёт CM1 (Contribution Margin level 1) с формулой и числами\n"
                        "2. CM2 (с учётом CAC) — payback CAC в месяцах\n"
                        "3. LTV (Lifetime Value) с обоснованием через retention\n"
                        "4. LTV/CAC ratio и интерпретация (норма / плохо / отлично)\n"
                        "5. Точка безубыточности (сколько клиентов чтобы окупить fix)\n"
                        "6. Чувствительность: что будет если ARPU ↑10%, если CAC ↓20%, если retention ↑5%\n"
                        "7. ТОП-3 рычага улучшения unit-экономики с приоритетом\n"
                        "8. Красные флаги (если есть) — где сейчас пожар"
                        + _REPORT_FORMAT_HINT,
                },
            ],
            "final_stage": "calc",
        },
    },

    # ── #15 Финансовая диагностика бизнеса ───────────────────────────────
    15: {
        "input_schema": [
            {"name": "business_type", "label": "Тип бизнеса / ниша",
             "type": "text", "required": True,
             "placeholder": "Онлайн-школа английского / B2B IT-агентство / производство"},
            {"name": "revenue", "label": "Выручка за последние 3 месяца",
             "type": "textarea", "required": True, "rows": 2,
             "hint": "Помесячно: например «март 1.2 млн / апр 1.5 млн / май 1.1 млн»"},
            {"name": "expenses", "label": "Структура расходов в месяц",
             "type": "textarea", "required": True, "rows": 3,
             "hint": "Зарплаты / аренда / маркетинг / закупки / прочее — с цифрами"},
            {"name": "debt", "label": "Долги / кредиты",
             "type": "text", "required": False,
             "placeholder": "Кредит 800к под 18% / лизинг 35к/мес / нет долгов"},
            {"name": "cash", "label": "Деньги на счетах сейчас",
             "type": "text", "required": True,
             "placeholder": "450 000 ₽"},
            {"name": "concerns", "label": "Что больше всего беспокоит",
             "type": "textarea", "required": True, "rows": 2,
             "hint": "Cashflow / маржа / расходы / рост — что главная боль"},
        ],
        "orchestra": {
            "default_model": "claude-sonnet",
            "input_hint": "Диагноз: где утечки, точки роста, риски — с цифрами",
            "stages": [
                {
                    "id": "research",
                    "type": "perplexity_research",
                    "label": "Бенчмарки в нише",
                    "model": "sonar",
                    "depth": "quick",
                    "query": "Финансовые бенчмарки для {field.business_type} в РФ 2025: "
                             "норма маржи, ФОТ %, маркетинг %. Что считается здоровым.",
                    "search_recency_filter": "year",
                },
                {
                    "id": "diagnosis",
                    "type": "llm",
                    "label": "Диагноз + план улучшения",
                    "model": "claude-sonnet-4-6",
                    "stream": True,
                    "user_prompt":
                        "Ты — финдиректор-аутсорс который видел сотни малых бизнесов.\n\n"
                        "ВВОД:\n"
                        "- Бизнес: {field.business_type}\n"
                        "- Выручка 3 мес: {field.revenue}\n"
                        "- Расходы: {field.expenses}\n"
                        "- Долги: {field.debt}\n"
                        "- Кэш: {field.cash}\n"
                        "- Главная боль: {field.concerns}\n\n"
                        "БЕНЧМАРКИ ниши:\n{research.output}\n\n"
                        "ДИАГНОСТИКА:\n"
                        "1. Тренд выручки (рост/стагнация/падение)\n"
                        "2. P&L по месяцам (упрощённый): выручка – расходы = чистая прибыль\n"
                        "3. Маржа (gross / operating / net) с интерпретацией vs бенчмарк\n"
                        "4. Структура расходов: где % выше нормы, где есть жир\n"
                        "5. Cashflow: хватает ли денег на 1/3/6 мес работы\n"
                        "6. Долговая нагрузка: чему сравнима ежемесячная выплата\n"
                        "7. ТОП-5 пожаров (где деньги утекают)\n"
                        "8. ТОП-5 рычагов роста с прогнозом эффекта\n"
                        "9. План на 3 месяца: что сделать, в каком порядке"
                        + _REPORT_FORMAT_HINT,
                },
            ],
            "final_stage": "diagnosis",
        },
    },

    # ── #16 Скрытые расходы при открытии бизнеса ─────────────────────────
    16: {
        "input_schema": [
            {"name": "business", "label": "Что хотите открыть",
             "type": "textarea", "required": True, "rows": 2,
             "placeholder": "Кофейню в спальном районе / IT-агентство B2B / онлайн-школу программирования"},
            {"name": "location", "label": "Локация / география",
             "type": "text", "required": True,
             "placeholder": "Москва, ЦАО / РФ онлайн / Краснодар"},
            {"name": "scale", "label": "Масштаб запуска",
             "type": "select", "required": True,
             "options": [
                 {"value": "solo", "label": "Соло-предприниматель (без команды)"},
                 {"value": "small", "label": "Микро (1-5 человек)"},
                 {"value": "medium", "label": "Малый (5-20)"},
                 {"value": "ambitious", "label": "Сразу средний (20+)"},
             ]},
            {"name": "expected_budget", "label": "Бюджет на запуск (как видите сейчас)",
             "type": "text", "required": True,
             "placeholder": "1 500 000 ₽ / есть 500к, остальное в кредит"},
            {"name": "expected_revenue", "label": "Ожидаемая выручка к концу года 1",
             "type": "text", "required": False,
             "placeholder": "300к ₽/мес / выйти в 0 за 6 месяцев"},
        ],
        "orchestra": {
            "default_model": "claude-sonnet",
            "input_hint": "30+ скрытых расходов, которые упускают 90% начинающих",
            "stages": [
                {
                    "id": "research",
                    "type": "perplexity_research",
                    "label": "Опыт открывших — что упустили",
                    "model": "sonar-reasoning-pro",
                    "depth": "standard",
                    "query": "Скрытые расходы при открытии {field.business} в РФ 2025. "
                             "Что упускают начинающие предприниматели в бюджете. Реальный опыт.",
                    "search_recency_filter": "year",
                },
                {
                    "id": "list",
                    "type": "llm",
                    "label": "Список + калькуляция",
                    "model": "claude-sonnet-4-6",
                    "stream": True,
                    "user_prompt":
                        "Ты — бизнес-консультант. Помоги человеку спланировать реалистичный бюджет.\n\n"
                        "ВВОД:\n"
                        "- Бизнес: {field.business}\n"
                        "- Локация: {field.location}\n"
                        "- Масштаб: {field.scale}\n"
                        "- Ожидаемый бюджет: {field.expected_budget}\n"
                        "- План выручки: {field.expected_revenue}\n\n"
                        "ИНСАЙТЫ:\n{research.output}\n\n"
                        "СТРУКТУРА:\n"
                        "1. Очевидные расходы (которые человек уже учёл): таблица с цифрами\n"
                        "2. **30+ СКРЫТЫХ РАСХОДОВ** — категории:\n"
                        "   - Юр и налоги (открытие, бухгалтерия, ОП, патент, эквайринг)\n"
                        "   - Подключения и сервисы (CRM, кассы, ОФД, интернет, телефония)\n"
                        "   - Маркетинг (не реклама — а сайт, фото, тексты, дизайн, СМИ)\n"
                        "   - Команда (поиск, обучение, оборудование, сменщики)\n"
                        "   - Помещение/инфра (ремонт сверх бюджета, доделки, мебель)\n"
                        "   - Резервы (3-6 мес ФОТ + аренда, налоги по итогу)\n"
                        "   Для каждого пункта — диапазон цен в РФ 2025\n"
                        "3. Реалистичный пересмотренный бюджет (X1.4-X1.7 от обычного плана)\n"
                        "4. Топ-5 ловушек первого года и как их избежать\n"
                        "5. Точка безубыточности — сколько нужно зарабатывать чтобы выйти в 0"
                        + _REPORT_FORMAT_HINT,
                },
            ],
            "final_stage": "list",
        },
    },

    # ── #17 Описание и оптимизация бизнес-процесса ───────────────────────
    17: {
        "input_schema": [
            {"name": "process", "label": "Какой процесс описываем",
             "type": "text", "required": True,
             "placeholder": "Обработка входящей заявки / онбординг клиента / закрытие месяца"},
            {"name": "as_is", "label": "Как процесс работает сейчас (AS-IS)",
             "type": "textarea", "required": True, "rows": 4,
             "hint": "Опиши шаги от начала до конца, кто что делает"},
            {"name": "pain_points", "label": "Где болит",
             "type": "textarea", "required": True, "rows": 2,
             "hint": "Долго / ошибки / срывы / дублирование / зависимости"},
            {"name": "people", "label": "Кто участвует",
             "type": "textarea", "required": True, "rows": 2,
             "hint": "Должности и зоны ответственности"},
            {"name": "frequency", "label": "Как часто запускается",
             "type": "text", "required": True,
             "placeholder": "10 раз в день / раз в месяц / по запросу"},
            {"name": "target", "label": "Цель оптимизации",
             "type": "select", "required": True,
             "options": [
                 {"value": "speed", "label": "Ускорить (меньше времени на 1 цикл)"},
                 {"value": "errors", "label": "Меньше ошибок"},
                 {"value": "scale", "label": "Масштабировать (могут делать новички)"},
                 {"value": "cost", "label": "Снизить стоимость"},
                 {"value": "automate", "label": "Автоматизировать рутину"},
             ]},
        ],
        "orchestra": {
            "default_model": "claude-sonnet",
            "input_hint": "AS-IS → анализ узких мест → TO-BE с инструментами",
            "stages": [
                {
                    "id": "analysis",
                    "type": "llm",
                    "label": "Описание + оптимизация",
                    "model": "claude-sonnet-4-6",
                    "stream": True,
                    "user_prompt":
                        "Ты — Process Excellence консультант (Lean / Six Sigma).\n\n"
                        "ВВОД:\n"
                        "- Процесс: {field.process}\n"
                        "- AS-IS: {field.as_is}\n"
                        "- Боли: {field.pain_points}\n"
                        "- Участники: {field.people}\n"
                        "- Частота: {field.frequency}\n"
                        "- Цель: {field.target}\n\n"
                        "СТРУКТУРА:\n"
                        "1. Формализованный AS-IS (по шагам, с указанием ответственных и времени каждого шага)\n"
                        "2. Карта узких мест (где теряется время, где возникают ошибки, где зависимости)\n"
                        "3. Анализ причин (5 Why для топ-3 проблем)\n"
                        "4. TO-BE — оптимизированный процесс. Markdown-схема.\n"
                        "5. Что меняется (delta vs AS-IS): шаги объединены, шаги удалены, шаги автоматизированы\n"
                        "6. Инструменты для внедрения (CRM-настройки, скрипты, шаблоны, автоматизации Make/Zapier)\n"
                        "7. Ожидаемый эффект: время / ошибки / cost — с цифрами\n"
                        "8. План внедрения (4 недели по неделям)\n"
                        "9. KPI для отслеживания нового процесса"
                        + _REPORT_FORMAT_HINT,
                },
            ],
            "final_stage": "analysis",
        },
    },

    # ── #18 Регламент работы сотрудника / отдела ─────────────────────────
    18: {
        "input_schema": [
            {"name": "scope", "label": "Регламент для",
             "type": "select", "required": True,
             "options": [
                 {"value": "person", "label": "Конкретной должности"},
                 {"value": "team", "label": "Целого отдела"},
                 {"value": "process", "label": "Конкретного процесса (cross-team)"},
             ]},
            {"name": "role", "label": "Должность / отдел / процесс",
             "type": "text", "required": True,
             "placeholder": "Менеджер по продажам / Отдел маркетинга / Закрытие сделки"},
            {"name": "responsibilities", "label": "Главные функции (4-7 шт)",
             "type": "textarea", "required": True, "rows": 3},
            {"name": "kpis", "label": "По каким KPI оцениваем",
             "type": "textarea", "required": True, "rows": 2,
             "hint": "3-5 метрик с целевыми значениями"},
            {"name": "tools", "label": "В каких системах работают",
             "type": "textarea", "required": False, "rows": 2,
             "hint": "CRM / Jira / Slack / 1C — что используется"},
            {"name": "escalation", "label": "Кому эскалировать в кризисе",
             "type": "text", "required": False,
             "placeholder": "РОП → CEO / Тимлид → СТО"},
        ],
        "orchestra": {
            "default_model": "claude-sonnet",
            "input_hint": "Документ-регламент — можно публиковать в Confluence",
            "stages": [
                {
                    "id": "doc",
                    "type": "llm",
                    "label": "Регламент в Markdown",
                    "model": "claude-sonnet-4-6",
                    "stream": True,
                    "user_prompt":
                        "Ты — HR-эксперт. Напиши формальный регламент.\n\n"
                        "Тип: {field.scope}\n"
                        "Объект: {field.role}\n"
                        "Функции: {field.responsibilities}\n"
                        "KPI: {field.kpis}\n"
                        "Инструменты: {field.tools}\n"
                        "Эскалация: {field.escalation}\n\n"
                        "СТРУКТУРА (Markdown):\n"
                        "1. Цели и зона ответственности (что входит / не входит)\n"
                        "2. Ежедневные задачи (чек-лист)\n"
                        "3. Еженедельные задачи (чек-лист)\n"
                        "4. Ежемесячные задачи (отчёты, планёрки)\n"
                        "5. KPI и как они меряются (формулы, источники)\n"
                        "6. Регламент работы с инструментами (что куда писать, когда)\n"
                        "7. Процедура эскалации (кому, когда, как)\n"
                        "8. Стандарты качества (что считается хорошо / плохо)\n"
                        "9. Действия в типовых ситуациях (5-10 кейсов с инструкциями)\n"
                        "10. Что запрещено категорически\n"
                        "Тон: формальный, без воды, как корпоративный документ."
                        + _REPORT_FORMAT_HINT,
                },
            ],
            "final_stage": "doc",
        },
    },

    # ── #19 ИИ-автоматизация: что внедрить прямо сейчас ──────────────────
    19: {
        "input_schema": [
            {"name": "business", "label": "Тип бизнеса",
             "type": "textarea", "required": True, "rows": 2,
             "placeholder": "Агентство digital-маркетинга на 12 человек, B2B"},
            {"name": "team_size", "label": "Размер команды",
             "type": "text", "required": True,
             "placeholder": "12 человек: 3 PM, 5 спецов, 2 продажника, 2 саппорт"},
            {"name": "rutine_tasks", "label": "Какие задачи рутинные / отнимают время",
             "type": "textarea", "required": True, "rows": 3,
             "hint": "Конкретно — не «работа с клиентами», а «писать первичные предложения по запросу»"},
            {"name": "current_tools", "label": "Что уже используете",
             "type": "textarea", "required": False, "rows": 2,
             "hint": "CRM / таск-трекер / ChatGPT / другие AI"},
            {"name": "ai_budget", "label": "Бюджет на AI / автоматизацию (мес)",
             "type": "text", "required": True,
             "placeholder": "до 30 000 ₽/мес / без бюджета — только бесплатные / гибкий"},
        ],
        "orchestra": {
            "default_model": "claude-sonnet",
            "input_hint": "Конкретный план: что внедрить, сколько стоит, какой ROI",
            "stages": [
                {
                    "id": "research",
                    "type": "perplexity_research",
                    "label": "Лучшие AI-инструменты под задачу",
                    "model": "sonar",
                    "depth": "standard",
                    "query": "Топ AI-инструменты для бизнеса {field.business} в РФ 2025. "
                             "Что реально работает для автоматизации задач: {field.rutine_tasks}. "
                             "Цены, доступность из РФ, кейсы.",
                    "search_recency_filter": "year",
                },
                {
                    "id": "plan",
                    "type": "llm",
                    "label": "План внедрения с приоритетами",
                    "model": "claude-sonnet-4-6",
                    "stream": True,
                    "user_prompt":
                        "Ты — AI-внедренец, работающий с малым/средним бизнесом.\n\n"
                        "ВВОД:\n"
                        "- Бизнес: {field.business}\n"
                        "- Команда: {field.team_size}\n"
                        "- Рутина: {field.rutine_tasks}\n"
                        "- Текущие инструменты: {field.current_tools}\n"
                        "- Бюджет AI: {field.ai_budget}\n\n"
                        "ОБЗОР ИНСТРУМЕНТОВ:\n{research.output}\n\n"
                        "СТРУКТУРА:\n"
                        "1. Анализ рутины: какие задачи стоит автоматизировать в первую очередь "
                        "(критерии: частота, объём, повторяемость, низкая креативность)\n"
                        "2. Топ-5 решений под ваш бизнес — таблица: задача / инструмент / цена / эффект\n"
                        "3. Quick wins (внедрить за 1 неделю — быстрая выгода)\n"
                        "4. Средняя дистанция (1-3 месяца — настройка + обучение команды)\n"
                        "5. Долгосрочно (полгода+) — серьёзные интеграции\n"
                        "6. Оценка ROI: сколько часов/денег экономим / окупится за N месяцев\n"
                        "7. План внедрения по приоритетам + риски\n"
                        "8. Что НЕ автоматизировать (где AI пока подведёт)"
                        + _REPORT_FORMAT_HINT,
                },
            ],
            "final_stage": "plan",
        },
    },

    # ── #20 Портрет идеального клиента (ICP) ─────────────────────────────
    20: {
        "input_schema": [
            {"name": "product", "label": "Что продаёте",
             "type": "textarea", "required": True, "rows": 2,
             "placeholder": "Внедрение Битрикс24 для риелторских агентств"},
            {"name": "best_clients", "label": "Кто ваши лучшие клиенты сейчас",
             "type": "textarea", "required": True, "rows": 3,
             "hint": "Имена/типы 3-5 клиентов которые приносят больше всего денег и не уходят"},
            {"name": "churn_clients", "label": "Кто уходит/недоволен",
             "type": "textarea", "required": False, "rows": 2,
             "hint": "Кто отвалился — что у них общего"},
            {"name": "deal_cycle", "label": "Длина цикла сделки",
             "type": "text", "required": False,
             "placeholder": "В среднем 3 недели"},
            {"name": "geography", "label": "География",
             "type": "text", "required": False,
             "placeholder": "Москва+Петербург / РФ / СНГ / EU"},
        ],
        "orchestra": {
            "default_model": "claude-sonnet",
            "input_hint": "Кого таргетим, кого избегаем, как искать — конкретный документ",
            "stages": [
                {
                    "id": "icp",
                    "type": "llm",
                    "label": "ICP документ",
                    "model": "claude-sonnet-4-6",
                    "stream": True,
                    "user_prompt":
                        "Ты — sales-стратег. Сделай ICP (Ideal Customer Profile) документ.\n\n"
                        "ВВОД:\n"
                        "- Продукт: {field.product}\n"
                        "- Лучшие клиенты: {field.best_clients}\n"
                        "- Кто уходит: {field.churn_clients}\n"
                        "- Цикл сделки: {field.deal_cycle}\n"
                        "- География: {field.geography}\n\n"
                        "СТРУКТУРА:\n"
                        "1. Демография ICP (отрасль, размер компании, выручка, география, возраст бизнеса)\n"
                        "2. Психография decision maker'а (роль, возраст, опыт, стиль решений, "
                        "болевые точки в работе)\n"
                        "3. Jobs-to-be-Done — какие 3-5 задач решает наш продукт у этого клиента\n"
                        "4. Триггеры: события которые подталкивают к покупке\n"
                        "5. Где их искать (каналы, форумы, мероприятия, источники)\n"
                        "6. Decision process: кто участвует, как долго, кто бюджетный держатель\n"
                        "7. Anti-ICP — кого НЕ таргетировать (по чёрному списку из ушедших)\n"
                        "8. Готовые формулировки питча под каждую боль\n"
                        "9. Чек-лист квалификации лида: 7 вопросов чтобы понять «наш / не наш»"
                        + _REPORT_FORMAT_HINT,
                },
            ],
            "final_stage": "icp",
        },
    },

    # ── #10 Рекламные тексты для всех форматов ───────────────────────────
    10: {
        "input_schema": [
            {"name": "product", "label": "Продукт",
             "type": "text", "required": True},
            {"name": "audience", "label": "Целевая аудитория",
             "type": "textarea", "required": True, "rows": 2},
            {"name": "key_benefit", "label": "Главная выгода (одно предложение)",
             "type": "text", "required": True,
             "placeholder": "Сохраните 4 часа в неделю на отчётах"},
            {"name": "platforms", "label": "Платформы",
             "type": "text", "required": True,
             "placeholder": "Яндекс.Директ, VK Ads, Telegram Ads, Авито Promo"},
            {"name": "cta", "label": "Призыв к действию",
             "type": "text", "required": True,
             "placeholder": "Запишитесь на демо / Получите чек-лист / Купить со скидкой"},
            {"name": "tone", "label": "Tone of voice",
             "type": "select", "required": False,
             "options": [
                 {"value": "expert", "label": "Экспертный"},
                 {"value": "friendly", "label": "Дружеский"},
                 {"value": "urgent", "label": "Срочность/дефицит"},
                 {"value": "challenging", "label": "Провокационный"},
                 {"value": "premium", "label": "Премиум/luxury"},
             ]},
        ],
        "orchestra": {
            "default_model": "claude-sonnet",
            "input_hint": "Рекламные тексты под все форматы выбранных платформ",
            "stages": [
                {
                    "id": "ads",
                    "type": "llm",
                    "label": "Объявления по форматам",
                    "model": "claude-sonnet-4-6",
                    "stream": True,
                    "user_prompt":
                        "Ты — performance-копирайтер. Напиши рекламные тексты под платформы.\n\n"
                        "Продукт: {field.product}\n"
                        "ЦА: {field.audience}\n"
                        "Главная выгода: {field.key_benefit}\n"
                        "Платформы: {field.platforms}\n"
                        "CTA: {field.cta}\n"
                        "Тон: {field.tone}\n\n"
                        "ДЛЯ КАЖДОЙ ПЛАТФОРМЫ из списка дай 3-4 варианта объявлений с учётом её формата:\n"
                        "- **Яндекс.Директ**: Заголовок 1 (35 симв) + Заголовок 2 (30) + Текст (81)\n"
                        "- **VK Ads / Telegram Ads / Facebook Ads**: креатив-описание + текст 90 симв + CTA\n"
                        "- **Авито Promo**: цепляющий заголовок + первая строка + 5 преимуществ\n"
                        "- **Stories / Reels**: 3 кадра (текст для каждого) + закадровый сценарий\n"
                        "- **Email subject** (если в списке): 5 вариантов с разной механикой\n\n"
                        "Для каждого объявления — пометка какой механике соответствует "
                        "(scarcity, social proof, curiosity, etc) и кому из ЦА подходит лучше всего."
                        + _REPORT_FORMAT_HINT,
                },
            ],
            "final_stage": "ads",
        },
    },
}


def main():
    with SessionLocal() as db:
        for sid, payload in V2_SOLUTIONS.items():
            sol = db.query(Solution).filter_by(id=sid).first()
            if not sol:
                print(f"⚠ Решение #{sid} не найдено в БД — пропускаю")
                continue
            sol.input_schema_json = json.dumps(payload["input_schema"], ensure_ascii=False)
            sol.orchestra_json = json.dumps(payload["orchestra"], ensure_ascii=False)
            print(f"✅ #{sid:2} «{sol.title}»: input_schema={len(payload['input_schema'])} полей, "
                  f"orchestra={len(payload['orchestra']['stages'])} stage'ов")
        db.commit()
        print(f"\n=== Готово: переписано {len(V2_SOLUTIONS)} решений ===")
        print("Теперь проверь UI: при клике на любое из этих 10 решений должна "
              "открыться форма с полями (а не одна textarea).")


if __name__ == "__main__":
    main()
