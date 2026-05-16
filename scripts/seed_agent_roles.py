"""Сид готовых ролей ИИ Агентов v2 (см. docs/modules/22-agents-v2-roadmap.md).

Каждая роль = AgentRole + парный shadow-Solution (subcategory='_agent_role').
Реальный pipeline (orchestra) хранится в Solution.orchestra_json — это
позволяет переиспользовать solutions_orchestra.run_orchestra без копипасты.
Shadow-Solutions скрыты из обычного каталога /solutions через фильтр.

Идемпотентен: повторный запуск перезаписывает pipeline_json + orchestra_json
существующих ролей по slug. SolutionRun'ы / AgentRun'ы не затрагиваются.

Запуск на проде:
    cd /root/AI-CHE && /root/AI-CHE/venv/bin/python scripts/seed_agent_roles.py

С логированием:
    ... scripts/seed_agent_roles.py --verbose
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.db import SessionLocal  # noqa: E402
from server.models import AgentRole, Solution, SolutionCategory  # noqa: E402


_REPORT_FORMAT_HINT = """

=== ФОРМАТ ОТЧЁТА ===
Markdown-документ:
- Заголовок (#) с темой
- 4-7 смысловых разделов (## H2)
- Внутри каждого раздела: 2-4 абзаца + по необходимости список / таблица
- Цитаты-факты со ссылками (Markdown links) на первоисточники из ресёрча
- В конце: «### 🎯 Ключевые выводы» (5-7 буллетов) и
  «### 📚 Источники» (нумерованный список первоисточников)
- Тон: аналитика без воды, факты-цифры-ссылки
- Объём: 2000-4000 слов
"""


# ════════════════════════════════════════════════════════════════════════════
# Каталог ролей (на Iter 2 — только Поисковик)
# ════════════════════════════════════════════════════════════════════════════

ROLES = [
    {
        "slug": "researcher",
        "title": "Поисковик",
        "icon": "🔍",
        "short_summary": "Глубокий research темы со ссылками на источники",
        "description":
            "Делает многоуровневый ресёрч: широкий обзор рынка/темы → точечная "
            "проверка ключевых фактов с цитатами → структурированный отчёт. "
            "Использует Perplexity (sonar-reasoning-pro + sonar-pro) для актуальных "
            "данных и Claude Sonnet для синтеза. Подходит для аналитики рынков, "
            "конкурентов, трендов, due diligence.",
        "base_price_kop": 20000,   # 200 ₽ за запуск
        "default_model": "claude-sonnet",
        "default_kb_categories": "",   # Поисковик не привязан к Knowledge Hub по умолчанию
        "system_prompt":
            "Ты — старший исследователь-аналитик. Работаешь с актуальными источниками, "
            "не выдумываешь факты, всегда ссылаешься на первоисточники. Если данных мало — "
            "честно говоришь. Структура важнее объёма.",
        "input_schema": [
            {"name": "topic", "label": "Тема исследования",
             "type": "textarea", "required": True, "rows": 3,
             "hint": "Что нужно изучить — рынок, компанию, тренд, конкурента",
             "placeholder": "Состояние рынка облачного видеонаблюдения для МСБ в России 2026"},
            {"name": "focus", "label": "На чём сфокусироваться",
             "type": "textarea", "required": False, "rows": 2,
             "hint": "Конкретные вопросы, метрики, конкуренты — что важно подсветить",
             "placeholder": "цены, доля рынка топ-5 игроков, нишевые B2B-сегменты"},
            {"name": "depth", "label": "Глубина",
             "type": "select", "required": True,
             "options": [
                 {"value": "quick", "label": "Быстрый обзор (3-5 минут, ~$0.10)"},
                 {"value": "standard", "label": "Стандартный отчёт (5-8 минут, ~$0.30)"},
                 {"value": "deep", "label": "Глубокий due-diligence (10-15 минут, ~$0.80)"},
             ]},
        ],
        "orchestra": {
            "default_model": "claude-sonnet",
            "input_hint": "Опишите тему — сделаем ресёрч с цитатами и структурным отчётом",
            "stages": [
                {
                    "id": "broad",
                    "type": "perplexity_research",
                    "label": "Широкий ресёрч контекста",
                    "model": "sonar-reasoning-pro",
                    "depth": "standard",
                    "query": "Полный обзор: {field.topic}. "
                             "Контекст рынка, ключевые игроки, размеры, динамика, "
                             "регуляторная среда. Дополнительный фокус: {field.focus}.",
                    "search_recency_filter": "year",
                },
                {
                    "id": "focused",
                    "type": "perplexity_research",
                    "label": "Точечный поиск фактов и цифр",
                    "model": "sonar-pro",
                    "depth": "standard",
                    "query": "Найди конкретные цифры, кейсы и факты по теме «{field.topic}». "
                             "Особенно интересует: {field.focus}. "
                             "Учитывай контекст широкого ресёрча: {broad.output}",
                    "search_recency_filter": "month",
                },
                {
                    "id": "synthesis",
                    "type": "synthesize",
                    "label": "Финальный отчёт-синтез",
                    "model": "claude-sonnet-4-6",
                    "stream": True,
                    "user_prompt":
                        "Ты — старший аналитик. Собери развёрнутый ресёрч-отчёт на тему:\n\n"
                        "**{field.topic}**\n\n"
                        "Фокус: {field.focus}\n"
                        "Глубина: {field.depth}\n\n"
                        "Контекст рынка:\n{broad.output}\n\n"
                        "Точечные факты и цифры:\n{focused.output}\n\n"
                        "Сделай отчёт по теме. Ссылайся на первоисточники из контекста "
                        "(URL в формате Markdown). Не выдумывай. Если данных по подвопросу "
                        "не хватает — явно отметь «недостаточно данных»."
                        + _REPORT_FORMAT_HINT,
                },
            ],
            "final_stage": "synthesis",
        },
        "sort_order": 10,
    },
]


# ════════════════════════════════════════════════════════════════════════════
# Хелперы — создание/обновление shadow-Solution и AgentRole
# ════════════════════════════════════════════════════════════════════════════

_SHADOW_CATEGORY_SLUG = "_agent_shadows"
_SHADOW_CATEGORY_TITLE = "Скрытые роли ИИ Агентов v2"


def _get_or_create_shadow_category(db) -> SolutionCategory:
    cat = db.query(SolutionCategory).filter_by(slug=_SHADOW_CATEGORY_SLUG).first()
    if cat:
        return cat
    cat = SolutionCategory(slug=_SHADOW_CATEGORY_SLUG, title=_SHADOW_CATEGORY_TITLE,
                           sort_order=999)
    db.add(cat)
    db.flush()
    return cat


def _upsert_shadow_solution(db, role_def: dict, cat_id: int) -> Solution:
    """Создаёт/обновляет hidden Solution который реально хранит pipeline."""
    title = f"[Agent] {role_def['title']}"
    sol = (db.query(Solution)
             .filter_by(subcategory="_agent_role")
             .filter(Solution.title == title)
             .first())
    orchestra_json = json.dumps(role_def["orchestra"], ensure_ascii=False)
    input_schema_json = json.dumps(role_def["input_schema"], ensure_ascii=False)
    if sol:
        sol.orchestra_json = orchestra_json
        sol.input_schema_json = input_schema_json
        sol.price_tokens = role_def["base_price_kop"]
        sol.description = role_def["description"]
        sol.short_summary = role_def["short_summary"]
        sol.is_active = True
    else:
        sol = Solution(
            category_id=cat_id,
            title=title,
            description=role_def["description"],
            short_summary=role_def["short_summary"],
            price_tokens=role_def["base_price_kop"],
            is_active=True,
            sort_order=role_def.get("sort_order", 0),
            subcategory="_agent_role",
            orchestra_json=orchestra_json,
            input_schema_json=input_schema_json,
        )
        db.add(sol)
        db.flush()
    return sol


def _upsert_role(db, role_def: dict, shadow_id: int) -> AgentRole:
    role = db.query(AgentRole).filter_by(slug=role_def["slug"]).first()
    pipeline_json = json.dumps(role_def["orchestra"], ensure_ascii=False)
    if role:
        role.title = role_def["title"]
        role.icon = role_def["icon"]
        role.description = role_def["description"]
        role.short_summary = role_def["short_summary"]
        role.base_price_kop = role_def["base_price_kop"]
        role.default_model = role_def["default_model"]
        role.system_prompt = role_def["system_prompt"]
        role.pipeline_json = pipeline_json
        role.default_kb_categories = role_def["default_kb_categories"]
        role.shadow_solution_id = shadow_id
        role.is_active = True
        role.sort_order = role_def.get("sort_order", 0)
    else:
        role = AgentRole(
            slug=role_def["slug"],
            title=role_def["title"],
            icon=role_def["icon"],
            description=role_def["description"],
            short_summary=role_def["short_summary"],
            base_price_kop=role_def["base_price_kop"],
            default_model=role_def["default_model"],
            system_prompt=role_def["system_prompt"],
            pipeline_json=pipeline_json,
            default_kb_categories=role_def["default_kb_categories"],
            shadow_solution_id=shadow_id,
            is_active=True,
            sort_order=role_def.get("sort_order", 0),
        )
        db.add(role)
    return role


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        cat = _get_or_create_shadow_category(db)
        for r in ROLES:
            sol = _upsert_shadow_solution(db, r, cat.id)
            role = _upsert_role(db, r, sol.id)
            db.commit()
            print(f"✓ {r['slug']:14s} → role.id={role.id} shadow.id={sol.id} ({r['title']})")
            if args.verbose:
                print(f"   stages: {[s['id'] for s in r['orchestra']['stages']]}")
                print(f"   price : {r['base_price_kop']/100:.2f} ₽")
        print()
        print(f"Готово: {len(ROLES)} ролей засеялись.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
