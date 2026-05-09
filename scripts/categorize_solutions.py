"""
Распределяет существующие Solution по подкатегориям на основе ключевых
слов в title. Идемпотентный — повторный запуск только проставляет
subcategory тем, у кого ещё пусто. Если субкатегория уже задана (вручную
из админки или через предыдущий запуск этого скрипта) — не трогаем.

Использование:
    python scripts/categorize_solutions.py [--force]

  --force — перезаписать ВСЕ subcategory (даже если уже задано)

Категории:
  research   — конкурентный анализ, аудит, ресёрч
  marketing  — контент, реклама, оффер, соцсети
  legal      — договоры, юр-проверки
  finance    — финансы, налоги, Excel
  strategy   — SWOT, бизнес-план, лендинг
  sales      — скрипты, КП, продажи
  hr         — рекрутинг, регламенты, оценка
"""
import os, sys, argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.db import db_session
from server.models import Solution


# Ключевые слова → категория (порядок важен — первый match выигрывает)
RULES = [
    # Юридическое
    ("legal", ["договор", "юр-", "юр.", "юридическ", "152-фз", "152фз",
               "персональн", "право", "суд", "иск", "контрагент"]),
    # Финансы
    ("finance", ["финанс", "бухгалт", "налог", "усн", "ндс", "excel",
                 "эксель", "бюджет", "кэш-флоу", "cashflow", "выручк"]),
    # Маркетинг
    ("marketing", ["контент-план", "контент план", "соцсет", "instagram",
                   "вконтакт", "email-рассылка", "холодн", "оффер",
                   "реклам", "лид", "smm", "блог", "рассылк"]),
    # Продажи
    ("sales", ["скрипт", "продаж", "возражен", "звонок", "коммерческое",
               "кп ", " кп", "клиент"]),
    # HR
    ("hr", ["вакан", "рекрут", "сотрудник", "hr", "найм", "регламент",
            "должностн", "оценка персонал"]),
    # Стратегия
    ("strategy", ["swot", "бизнес-план", "бизнесплан", "стратеги",
                  "лендинг", "позицион", "uvp"]),
    # Ресёрч (выделяем последним — широкий)
    ("research", ["анализ", "исследован", "ресёрч", "ресерч", "research",
                  "аудит", "конкурент", "ниш"]),
]


def detect_category(title: str, description: str = "") -> str | None:
    text = f"{title} {description}".lower()
    for cat, keywords in RULES:
        for kw in keywords:
            if kw in text:
                return cat
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="Перезаписать subcategory даже если уже задано")
    args = parser.parse_args()

    with db_session() as db:
        rows = db.query(Solution).all()
        if not rows:
            print("Нет Solution в БД")
            return

        stats = {}
        unknown = []
        for s in rows:
            if s.subcategory and not args.force:
                stats.setdefault(s.subcategory, 0)
                stats[s.subcategory] += 1
                continue
            cat = detect_category(s.title or "", s.description or "")
            if not cat:
                unknown.append(s.title)
                continue
            s.subcategory = cat
            stats.setdefault(cat, 0)
            stats[cat] += 1
        db.commit()

        print()
        print("Распределение по категориям:")
        for cat in sorted(stats.keys()):
            print(f"  {cat:12s} {stats[cat]:3d}")
        if unknown:
            print()
            print(f"Без категории ({len(unknown)} шт.) — нужно проставить вручную:")
            for t in unknown[:20]:
                print(f"  - {t}")


if __name__ == "__main__":
    main()
