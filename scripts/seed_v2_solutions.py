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
