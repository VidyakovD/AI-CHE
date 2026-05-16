"""Сид Бизнес-решения «Юрист — проверка контрагента» (категория Бизнес, subcategory=legal).

Pipeline: inn_lookup (Checko ЕГРЮЛ/ЕГРИП) → perplexity_research (свежая судебка) →
synthesize (Sonnet с светофором 🟢🟡🔴).

Идемпотентен: повторный запуск перезаписывает orchestra_json + input_schema_json
существующего Solution по title. SolutionRun'ы не затрагиваются.

Запуск на проде:
    cd /root/AI-CHE && /root/AI-CHE/venv/bin/python scripts/seed_solution_lawyer.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.db import SessionLocal  # noqa: E402
from server.models import Solution, SolutionCategory  # noqa: E402


_TITLE = "Юрист — проверка контрагента"
_SUBCATEGORY = "legal"


_REPORT_FORMAT_HINT = """

=== ФОРМАТ ОТЧЁТА ===
Markdown-отчёт:
- Заголовок (#) с ИНН и кратким именем контрагента
- ## Краткое резюме (3-5 строк) — можно работать / нужны уточнения / красные флаги
- ## Базовые сведения (из карточки Checko)
- ## Анализ рисков по категориям:
  - ### Финансовая устойчивость
  - ### Судебная история
  - ### Налоговая дисциплина
  - ### Репутационные риски
  - ### Регуляторные риски (по нише ОКВЭД)
- ## Светофор сделки: **🟢 безопасно** / **🟡 с осторожностью** / **🔴 не рекомендуется** (одно из трёх)
- ## Что запросить у контрагента дополнительно (по контексту сделки)
- ## Условия в договор (которые защитят, если работать)

Тон: юрист-практик. Не выдумывай факты. Если данных нет — пиши «недостаточно данных».
"""


ORCHESTRA = {
    "default_model": "claude-sonnet",
    "input_hint": "Введите ИНН контрагента — сделаем due diligence с выпиской и риск-отчётом",
    "stages": [
        {
            "id": "card",
            "type": "inn_lookup",
            "label": "Выписка из ЕГРЮЛ/ЕГРИП (Checko)",
            "inn_field": "inn",
        },
        {
            "id": "research",
            "type": "perplexity_research",
            "label": "Свежая судебная практика и регуляторные изменения",
            "model": "sonar-reasoning-pro",
            "depth": "standard",
            "query":
                "Проверка контрагента ИНН {field.inn}. "
                "Найди свежие судебные дела, банкротства, реорганизации, "
                "налоговые проверки, изменения в ОКВЭД за 12 мес. "
                "Контекст сделки: {field.deal_context}. "
                "Беспокоят риски: {field.concerns}. "
                "Учитывай уже собранную карточку: {card.output}",
            "search_recency_filter": "year",
        },
        {
            "id": "synthesis",
            "type": "synthesize",
            "label": "Юр-отчёт с оценкой рисков",
            "model": "claude-sonnet-4-6",
            "stream": True,
            "user_prompt":
                "Ты — юрист-практик с опытом проверки контрагентов и сделок. "
                "Работаешь со 152-ФЗ, ГК РФ, 44/223-ФЗ. Не даёшь рекомендаций "
                "без опоры на факты из выписки ЕГРЮЛ/ЕГРИП и судебной практики.\n\n"
                "**ИНН:** {field.inn}\n"
                "**Контекст сделки:** {field.deal_context}\n"
                "**Беспокоят:** {field.concerns}\n\n"
                "Карточка из ЕГРЮЛ/ЕГРИП:\n{card.output}\n\n"
                "Судебная практика и контекст:\n{research.output}\n\n"
                "Собери отчёт по структуре ниже. Не выдумывай. Если по какому-то "
                "блоку информации нет — пиши «недостаточно данных»."
                + _REPORT_FORMAT_HINT,
        },
    ],
    "final_stage": "synthesis",
}


INPUT_SCHEMA = [
    {"name": "inn", "label": "ИНН контрагента",
     "type": "text", "required": True,
     "hint": "10 цифр для ООО, 12 для ИП",
     "placeholder": "7707083893"},
    {"name": "deal_context", "label": "Контекст сделки",
     "type": "textarea", "required": False, "rows": 3,
     "hint": "Что собираетесь делать: купить/продать/подписать договор/нанять подрядчика",
     "placeholder": "Подписываем договор поставки на 5 млн ₽, первая сделка"},
    {"name": "concerns", "label": "Какие риски вас беспокоят",
     "type": "textarea", "required": False, "rows": 2,
     "placeholder": "репутация в суде, банкротство, налоговая дисциплина"},
]


DESCRIPTION = (
    "Делает юридическую проверку контрагента: тянет реальную выписку "
    "ЕГРЮЛ/ЕГРИП через Checko.ru (статус, ОКВЭД, директор, юр.адрес, "
    "санкции, недобросовестность, дисквалифицированные лица), проверяет "
    "свежую судебную практику и регуляторные изменения через Perplexity, "
    "синтезирует риск-отчёт со светофором 🟢🟡🔴. "
    "Подходит для due diligence новых поставщиков, проверки клиентов "
    "перед крупной сделкой, базовой юр-проверки контрагентов."
)


def main():
    db = SessionLocal()
    try:
        # Категория «Бизнес-решения» (id=1) — основная
        cat = db.query(SolutionCategory).filter_by(slug="business").first()
        if not cat:
            raise RuntimeError("Категория 'business' не найдена. Запустите базовый сид сначала.")

        # Upsert по title
        sol = db.query(Solution).filter_by(title=_TITLE).first()
        orchestra_json = json.dumps(ORCHESTRA, ensure_ascii=False)
        input_schema_json = json.dumps(INPUT_SCHEMA, ensure_ascii=False)

        if sol:
            sol.description = DESCRIPTION
            sol.short_summary = "Проверка контрагента по ИНН: ЕГРЮЛ + суды + светофор сделки"
            sol.price_tokens = 25000   # 250 ₽
            sol.is_active = True
            sol.subcategory = _SUBCATEGORY
            sol.tags = "due-diligence,checko,perplexity,deep"
            sol.is_featured = True
            sol.orchestra_json = orchestra_json
            sol.input_schema_json = input_schema_json
            sol.category_id = cat.id
            print(f"✓ обновили Solution.id={sol.id}: {_TITLE}")
        else:
            sol = Solution(
                category_id=cat.id,
                title=_TITLE,
                description=DESCRIPTION,
                short_summary="Проверка контрагента по ИНН: ЕГРЮЛ + суды + светофор сделки",
                price_tokens=25000,
                is_active=True,
                sort_order=5,
                subcategory=_SUBCATEGORY,
                tags="due-diligence,checko,perplexity,deep",
                is_featured=True,
                orchestra_json=orchestra_json,
                input_schema_json=input_schema_json,
            )
            db.add(sol)
            db.flush()
            print(f"✓ создали Solution.id={sol.id}: {_TITLE}")

        db.commit()
        print(f"   subcategory: {_SUBCATEGORY} · price: {sol.price_tokens/100:.0f} ₽")
        print(f"   pipeline: inn_lookup → perplexity_research → synthesize")
    finally:
        db.close()


if __name__ == "__main__":
    main()
