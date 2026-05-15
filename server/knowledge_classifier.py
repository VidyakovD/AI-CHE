"""Авто-классификатор файлов Knowledge Hub (Агенты v2).

Один Haiku-вызов на файл — определяем категорию из фиксированного списка.
Каждая роль агента запрашивает только релевантные категории, экономим
кредиты на лишний контекст.

Категории:
  pricing    — прайсы, тарифы, коммерческие условия (XLSX/PDF/CSV)
  legal      — договоры, оферты, реквизиты юр-лица, шаблоны юр-документов
  finance    — P&L, банковские выписки, налоги, отчётность
  brand      — описание компании, бренд-кит, логотип, tone-of-voice
  regulation — внутренние регламенты, инструкции, FAQ, регламенты процессов
  contacts   — контакты сотрудников, оргструктура, роли, телефоны
  other      — fallback

Стоимость: ~$0.001-0.003 за классификацию (Haiku, ≤200 input + 10 output tokens).
"""
from __future__ import annotations

import logging
import re
from typing import Optional

log = logging.getLogger(__name__)

CATEGORIES = ("pricing", "legal", "finance", "brand", "regulation", "contacts", "other")
DEFAULT_CATEGORY = "other"

_CATEGORY_DESCRIPTIONS = {
    "pricing":    "прайсы, тарифы, коммерческие условия, цены",
    "legal":      "договоры, оферты, реквизиты юр-лица, юридические документы",
    "finance":    "P&L, банковские выписки, налоги, отчётность, финансы",
    "brand":      "описание компании, бренд-кит, логотип, tone-of-voice",
    "regulation": "внутренние регламенты, инструкции, FAQ, процессы",
    "contacts":   "контакты сотрудников, оргструктура, роли, телефоны",
    "other":      "не подходит ни в одну из категорий",
}

_PROMPT_SYSTEM = (
    "Ты классификатор файлов базы знаний компании. Отвечай ОДНИМ словом — "
    "точным названием категории из списка. Никаких объяснений."
)


def _build_user_prompt(filename: str, mime: str | None, text_preview: str) -> str:
    cat_lines = "\n".join(f"  {k} — {v}" for k, v in _CATEGORY_DESCRIPTIONS.items())
    preview = (text_preview or "")[:1500].strip()
    return (
        f"Файл: {filename}\n"
        f"MIME: {mime or 'неизвестен'}\n\n"
        f"Категории:\n{cat_lines}\n\n"
        f"Превью текста:\n{preview if preview else '[не извлечён]'}\n\n"
        f"Ответь одним словом из списка: {', '.join(CATEGORIES)}"
    )


def _normalize(raw: str) -> str:
    """Достаём первое слово из ответа Haiku, валидируем по белому списку."""
    if not raw:
        return DEFAULT_CATEGORY
    # Берём первое слово в нижнем регистре, убираем пунктуацию
    m = re.search(r"[a-zA-Zа-яА-Я_]+", raw)
    word = (m.group(0) if m else "").lower().strip()
    if word in CATEGORIES:
        return word
    # Иногда Haiku отвечает русским словом — мапим обратно
    ru_to_en = {
        "цены": "pricing", "прайс": "pricing", "тарифы": "pricing",
        "юридические": "legal", "юр": "legal", "договоры": "legal",
        "финансы": "finance", "отчётность": "finance",
        "бренд": "brand",
        "регламенты": "regulation", "инструкции": "regulation",
        "контакты": "contacts",
        "другое": "other", "прочее": "other",
    }
    return ru_to_en.get(word, DEFAULT_CATEGORY)


def classify(filename: str, mime: Optional[str], text_preview: str) -> str:
    """Один Haiku-вызов — возвращает категорию из CATEGORIES.

    Никогда не бросает исключения: при любой ошибке возвращает 'other'.
    Caller уже сохраняет category в KnowledgeFile.
    """
    try:
        from server.ai import generate_response
        result = generate_response(
            "claude-haiku",
            [
                {"role": "system", "content": _PROMPT_SYSTEM},
                {"role": "user", "content": _build_user_prompt(filename, mime, text_preview)},
            ],
            extra={"max_tokens": 10, "temperature": 0},
        )
        raw = result.get("content", "") if isinstance(result, dict) else str(result)
        category = _normalize(raw)
        log.info(f"[KB classify] {filename!r} → {category} (raw={raw!r})")
        return category
    except Exception as e:
        log.warning(f"[KB classify] failed for {filename!r}: {e}")
        return DEFAULT_CATEGORY
