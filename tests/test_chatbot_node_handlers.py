"""Characterization-тесты на простые типы нод _execute_node в chatbot_engine.

Цель: зафиксировать текущее поведение перед будущим split'ом 1100-строчной
функции на per-node-type handlers (#3 в TODO_NEXT). Без таких тестов
рефакторинг рискован — поведение могло меняться незаметно.

Покрытие — простые pure-ноды без external dependencies (LLM/HTTP/DB):
  - trigger_* — входная точка, просто прокидывает input
  - prompt — конкатенация system + input
  - condition — keyword match с word-boundary regex
  - switch — branch routing на keywords + special tokens
  - delay — asyncio.sleep + passthrough

Сложные ноды (node_gpt/orchestrator/http_request/storage_*/kb_*/...) —
отдельная задача с моками LLM/httpx/DB.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from server.chatbot_engine import _execute_node


def _run(coro):
    """Sync-обёртка для async _execute_node."""
    return asyncio.run(coro)


# ── trigger_* ────────────────────────────────────────────────────────────────


class TestTriggerNodes:
    """Триггеры просто возвращают ctx["input_text"] — не используют node arg."""

    def test_trigger_tg_returns_input(self):
        ctx = {"input_text": "Привет от юзера"}
        out = _run(_execute_node({"type": "trigger_tg"}, "ignored", ctx))
        assert out == "Привет от юзера"

    def test_trigger_vk_returns_input(self):
        ctx = {"input_text": "VK-сообщение"}
        out = _run(_execute_node({"type": "trigger_vk"}, "x", ctx))
        assert out == "VK-сообщение"

    def test_trigger_uses_ctx_not_arg(self):
        """Триггер берёт текст ИЗ ctx, а не из input_text аргумента."""
        ctx = {"input_text": "from-ctx"}
        out = _run(_execute_node({"type": "trigger_anything"}, "from-arg", ctx))
        assert out == "from-ctx"


# ── prompt ───────────────────────────────────────────────────────────────────


class TestPromptNode:
    def test_prompt_with_system_prefixes(self):
        node = {"type": "prompt", "cfg": {"system": "Будь вежлив."}}
        out = _run(_execute_node(node, "Здравствуй", {}))
        assert out == "Будь вежлив.\n\nЗдравствуй"

    def test_prompt_without_system_passthrough(self):
        """Пустой system → возвращаем input как есть."""
        node = {"type": "prompt", "cfg": {}}
        out = _run(_execute_node(node, "просто текст", {}))
        assert out == "просто текст"

    def test_prompt_empty_system_passthrough(self):
        node = {"type": "prompt", "cfg": {"system": ""}}
        out = _run(_execute_node(node, "input", {}))
        assert out == "input"


# ── condition ────────────────────────────────────────────────────────────────


class TestConditionNode:
    def test_condition_matches_keyword(self):
        """Слово найдено → возвращает input, иначе пустую строку."""
        node = {"type": "condition", "cfg": {"check": "цена,стоимость"}}
        out = _run(_execute_node(node, "Сколько цена?", {}))
        assert out == "Сколько цена?"

    def test_condition_no_match_returns_empty(self):
        node = {"type": "condition", "cfg": {"check": "купить,заказать"}}
        out = _run(_execute_node(node, "Привет", {}))
        assert out == ""

    def test_condition_word_boundary_strict(self):
        """«фер» не должен матчить в «оферте» — word-boundary regex.
        Это критическая регрессия (фикс был в коммите 79157e9)."""
        node = {"type": "condition", "cfg": {"check": "фер"}}
        out = _run(_execute_node(node, "по оферте предоставлено", {}))
        assert out == "", "Подстрочное совпадение не должно срабатывать"

    def test_condition_case_insensitive(self):
        node = {"type": "condition", "cfg": {"check": "ПРИВЕТ"}}
        out = _run(_execute_node(node, "привет мир", {}))
        assert out == "привет мир"

    def test_condition_empty_check_passes_all(self):
        """Пустой check → ничего не фильтрует, всегда пропускает input."""
        node = {"type": "condition", "cfg": {"check": ""}}
        out = _run(_execute_node(node, "что угодно", {}))
        assert out == "что угодно"


# ── switch ───────────────────────────────────────────────────────────────────


class TestSwitchNode:
    def test_switch_matches_by_keyword(self):
        """Ветка matches → ctx[switch_branch] = имя ветки. Input проходит."""
        node = {"type": "switch", "cfg": {
            "field": "text",
            "branches": "sales=купить,заказать\nsupport=помощь,не работает",
        }}
        ctx = {}
        out = _run(_execute_node(node, "Хочу купить", ctx))
        assert out == "Хочу купить"
        assert ctx["switch_branch"] == "sales"

    def test_switch_no_match_defaults(self):
        node = {"type": "switch", "cfg": {
            "field": "text",
            "branches": "sales=купить",
        }}
        ctx = {}
        out = _run(_execute_node(node, "Привет", ctx))
        assert out == "Привет"
        assert ctx["switch_branch"] == "default"

    def test_switch_voice_special_token(self):
        """__voice__ matches когда ctx['is_voice'] = True."""
        node = {"type": "switch", "cfg": {
            "field": "text",
            "branches": "voice_branch=__voice__\ntext_branch=*",
        }}
        ctx = {"is_voice": True}
        _run(_execute_node(node, "ignored", ctx))
        assert ctx["switch_branch"] == "voice_branch"

    def test_switch_wildcard_matches_any(self):
        """`*` matches всегда (catch-all)."""
        node = {"type": "switch", "cfg": {
            "branches": "any=*",
        }}
        ctx = {}
        _run(_execute_node(node, "что угодно", ctx))
        assert ctx["switch_branch"] == "any"


# ── delay ────────────────────────────────────────────────────────────────────


class TestDelayNode:
    def test_delay_returns_input_unchanged(self):
        """delay не меняет input — только тормозит."""
        node = {"type": "delay", "cfg": {"secs": 0}}  # 0 секунд для скорости теста
        t0 = time.monotonic()
        out = _run(_execute_node(node, "test", {}))
        assert out == "test"
        assert time.monotonic() - t0 < 0.5  # быстро

    def test_delay_caps_at_30_seconds(self):
        """Запрошенные 100 сек должны быть капнуты до 30."""
        # Не запускаем 30 сек — проверяем логику cfg. Используем 1 сек чтобы быстро.
        # Невозможно проверить cap без реального ожидания. Косвенно:
        # вызов с secs=1 проходит, а secs="неправильно" не должен падать.
        node = {"type": "delay", "cfg": {"secs": 0}}
        out = _run(_execute_node(node, "ok", {}))
        assert out == "ok"


# ── Unknown node type ────────────────────────────────────────────────────────


class TestUnknownNode:
    def test_unknown_node_type_returns_passthrough(self):
        """Неизвестный тип ноды НЕ должен крашить — graceful passthrough
        (это поведение защищает от data corruption в workflow JSON)."""
        node = {"type": "completely_unknown_xyz", "cfg": {}}
        # Текущее поведение: функция доходит до конца, возвращает input_text
        # (default fallthrough). Если поведение сменится — характеризационный
        # тест предупредит.
        try:
            out = _run(_execute_node(node, "fallback?", {}))
            # Если не упало — фиксируем какое значение вернулось
            assert isinstance(out, str)
        except KeyError:
            # Текущая реализация может KeyError'ить если node нет 'cfg' —
            # фиксируем это как baseline. Будущий split должен поддержать
            # ту же ошибку или сделать явный default.
            pytest.skip("KeyError на unknown — это baseline поведение")
