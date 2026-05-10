"""Чистит метаданные Solution для красивого UI:

1. Стрипит legacy-префикс `[Старая категория] ...` из description
2. Заполняет short_summary (короткое описание ≤140 симв) если пустой
3. Чинит несколько вручную-проверенных кривых subcategory

Запуск: PYTHONPATH=/root/AI-CHE /root/AI-CHE/venv/bin/python -m dotenv -f /root/AI-CHE/.env run /root/AI-CHE/venv/bin/python scripts/cleanup_solutions_metadata.py

Идемпотентен: повторный запуск ничего не сломает.
"""
import re
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.db import SessionLocal
from server.models import Solution


# Префикс [XXX] в начале description, который дублирует категорию
_LEGACY_PREFIX_RE = re.compile(r"^\s*\[[^\]\n]{1,40}\]\s*")

# Ручная карта правок subcategory: id → правильная категория
# Обоснование: текст на скриншоте + смысл title.
SUBCATEGORY_FIXES = {
    4:  "sales",      # Скрипт холодного звонка → продажи (а не маркетинг)
    9:  "marketing",  # Заголовки лендинга → маркетинг (а не продажи)
    10: "marketing",  # Рекламные тексты → маркетинг (а не legal!)
    12: "hr",         # Мотивация отдела продаж → HR (а не финансы)
    16: "strategy",   # Скрытые расходы при открытии бизнеса → планирование (а не финансы)
    17: "strategy",   # Описание/оптимизация бизнес-процесса → стратегия операций
    18: "hr",         # Регламент работы → HR/процессы
    19: "strategy",   # ИИ-автоматизация → стратегия
    23: "strategy",   # Гипотезы роста → стратегия (а не финансы — это идеи продукта)
    24: "strategy",   # Новые источники дохода → стратегия (а не research)
    25: "research",   # Тренды рынка → research
    26: "sales",      # Деловое письмо для партнёра → sales (outbound)
    28: "hr",         # Идеальный рабочий день руководителя → HR/менеджмент
    29: "strategy",   # Выход из операционки: делегирование → стратегия (а не research)
    30: "sales",      # Симулятор инвестора (питч) → sales (стиль убеждения)
}


def _strip_legacy_prefix(text: str | None) -> str:
    """Убирает '[Старая категория] ' в начале строки."""
    if not text:
        return ""
    return _LEGACY_PREFIX_RE.sub("", text).strip()


def _make_summary(description: str, max_len: int = 140) -> str:
    """Сжимает description до короткой выдержки для бейджа карточки."""
    cleaned = _strip_legacy_prefix(description)
    if len(cleaned) <= max_len:
        return cleaned
    # Обрезаем по последнему пробелу до max_len, добавляем '…'
    cut = cleaned[:max_len]
    last_space = cut.rfind(" ")
    if last_space > max_len * 0.6:
        cut = cut[:last_space]
    return cut.rstrip(",.;: ") + "…"


def main():
    with SessionLocal() as db:
        sols = db.query(Solution).order_by(Solution.id).all()
        print(f"Всего решений: {len(sols)}\n")

        n_desc_cleaned = 0
        n_summary_filled = 0
        n_subcat_fixed = 0

        for s in sols:
            changed = []

            # 1. Чистим legacy-префикс в description
            new_desc = _strip_legacy_prefix(s.description)
            if new_desc != (s.description or ""):
                s.description = new_desc
                n_desc_cleaned += 1
                changed.append("desc")

            # 2. Заполняем short_summary если пусто или содержит legacy-префикс
            current_summary = (s.short_summary or "").strip()
            if not current_summary or _LEGACY_PREFIX_RE.match(current_summary):
                summary = _make_summary(new_desc or s.description or "", max_len=140)
                if summary:
                    s.short_summary = summary
                    n_summary_filled += 1
                    changed.append("summary")

            # 3. Чиним subcategory из ручной карты
            if s.id in SUBCATEGORY_FIXES:
                target = SUBCATEGORY_FIXES[s.id]
                if (s.subcategory or "") != target:
                    old = s.subcategory or "—"
                    s.subcategory = target
                    n_subcat_fixed += 1
                    changed.append(f"subcat:{old}→{target}")

            if changed:
                print(f"#{s.id:3} [{(s.subcategory or '-'):>10}] {s.title[:50]:50} | "
                       f"{', '.join(changed)}")

        db.commit()
        print(f"\n=== Итого ===")
        print(f"  description очищено: {n_desc_cleaned}")
        print(f"  short_summary заполнено: {n_summary_filled}")
        print(f"  subcategory поправлено: {n_subcat_fixed}")


if __name__ == "__main__":
    main()
