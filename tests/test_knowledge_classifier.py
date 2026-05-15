"""Юнит-тесты для server.knowledge_classifier — нормализация ответа Haiku
не должна допускать мусор (бывает Haiku отвечает русским словом / с пунктуацией)."""
import pytest

from server.knowledge_classifier import (
    _normalize,
    DEFAULT_CATEGORY,
    CATEGORIES,
    _build_user_prompt,
)


@pytest.mark.parametrize("raw,expected", [
    ("pricing", "pricing"),
    ("PRICING", "pricing"),
    ("legal\n", "legal"),
    ("  brand  ", "brand"),
    ("contacts.", "contacts"),
    ("regulation,", "regulation"),
    ("finance — отчёт", "finance"),
    # Русский синоним
    ("цены", "pricing"),
    ("прайс", "pricing"),
    ("юр", "legal"),
    ("бренд", "brand"),
    ("регламенты", "regulation"),
    ("контакты", "contacts"),
    ("другое", "other"),
    # Мусор → fallback
    ("", DEFAULT_CATEGORY),
    ("xxxnothing", DEFAULT_CATEGORY),
    ("123", DEFAULT_CATEGORY),
    # Категория с лишним префиксом — берём первое слово
    ("pricing - тарифы", "pricing"),
])
def test_normalize(raw, expected):
    assert _normalize(raw) == expected


def test_categories_contains_default():
    assert DEFAULT_CATEGORY in CATEGORIES


def test_build_user_prompt_includes_filename_and_categories():
    p = _build_user_prompt("прайс_2026.xlsx", "application/xlsx", "Услуга, цена...")
    assert "прайс_2026.xlsx" in p
    assert "application/xlsx" in p
    # Все категории должны упоминаться
    for c in CATEGORIES:
        assert c in p


def test_build_user_prompt_truncates_preview():
    long_text = "x" * 5000
    p = _build_user_prompt("file.txt", None, long_text)
    # Превью обрезано до 1500 символов — не должно быть всех 5000 x подряд
    assert "x" * 5000 not in p


def test_build_user_prompt_handles_empty_preview():
    p = _build_user_prompt("file.txt", None, "")
    assert "[не извлечён]" in p
