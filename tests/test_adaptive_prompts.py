"""Тесты Adaptive System Prompts (расширение [LEARNED:] маркеров).

Покрывают:
  - _parse_learned_marker: разбор разных форматов
  - apply_module_memory_updates: дедупликация + promotion в profile
  - _format_adaptive_rules: группировка по типу, нумерация, заголовки
  - Backward compat: старые [LEARNED: txt] и legacy {style,preferences,constraints}
"""
import pytest


# ── _parse_learned_marker ──────────────────────────────────────────────────


class TestParseLearnedMarker:
    def test_simple_note_legacy(self):
        from server.agent_builder import _parse_learned_marker
        r = _parse_learned_marker("просто текст")
        assert r == {"scope": "module", "type": "note", "note": "просто текст"}

    def test_typed_style(self):
        from server.agent_builder import _parse_learned_marker
        r = _parse_learned_marker("style: пишет деловым тоном")
        assert r["type"] == "style"
        assert r["scope"] == "module"
        assert r["note"] == "пишет деловым тоном"

    def test_global_scope(self):
        from server.agent_builder import _parse_learned_marker
        r = _parse_learned_marker("global: важный факт")
        assert r["scope"] == "global"
        assert r["type"] == "note"
        assert r["note"] == "важный факт"

    def test_global_with_type(self):
        from server.agent_builder import _parse_learned_marker
        r = _parse_learned_marker("global:fact: ведёт b2b-стройку")
        assert r["scope"] == "global"
        assert r["type"] == "fact"
        assert r["note"] == "ведёт b2b-стройку"

    def test_type_then_global(self):
        from server.agent_builder import _parse_learned_marker
        # Порядок флагов не важен
        r = _parse_learned_marker("fact:global: что-то")
        assert r["scope"] == "global"
        assert r["type"] == "fact"
        assert r["note"] == "что-то"

    def test_note_with_colon_inside(self):
        from server.agent_builder import _parse_learned_marker
        # Префиксы съедены до style, дальше — note с двоеточием внутри
        r = _parse_learned_marker("style: тон: деловой, без эмодзи")
        assert r["type"] == "style"
        # Реализация: после consumed=1 склеивает parts[1:] обратно через ":"
        assert "тон" in r["note"]
        assert "деловой" in r["note"]

    def test_unknown_prefix_keeps_as_note(self):
        from server.agent_builder import _parse_learned_marker
        # "unknown" — не валидный префикс → вся строка идёт как note
        r = _parse_learned_marker("unknown: что-то")
        assert r["scope"] == "module"
        assert r["type"] == "note"
        # Префикс не съели → note содержит "unknown: что-то"
        assert "unknown" in r["note"]

    def test_empty(self):
        from server.agent_builder import _parse_learned_marker
        assert _parse_learned_marker("") is None
        assert _parse_learned_marker("   ") is None


# ── apply_module_memory_updates ────────────────────────────────────────────


class TestApplyMemoryUpdates:
    def test_legacy_format_compat(self):
        """Старый формат updates={"new_notes": [...]} продолжает работать."""
        from server.agent_builder import apply_module_memory_updates
        mem = {}
        apply_module_memory_updates(mem, {"new_notes": ["первая заметка"]})
        assert len(mem["learned"]) == 1
        assert mem["learned"][0]["note"] == "первая заметка"
        assert mem["learned"][0]["type"] == "note"

    def test_new_items_format(self):
        from server.agent_builder import apply_module_memory_updates
        mem = {}
        apply_module_memory_updates(mem, {"items": [
            {"scope": "module", "type": "style", "note": "деловой тон"},
            {"scope": "module", "type": "constraint", "note": "не упоминать X"},
        ]})
        assert len(mem["learned"]) == 2
        types = sorted(L["type"] for L in mem["learned"])
        assert types == ["constraint", "style"]

    def test_dedup_same_note(self):
        from server.agent_builder import apply_module_memory_updates
        mem = {"learned": [
            {"note": "Деловой тон", "type": "style", "ts": "old"},
        ]}
        # Дубль (case-insensitive) — не должен добавиться
        apply_module_memory_updates(mem, {"items": [
            {"scope": "module", "type": "style", "note": "деловой ТОН"},
        ]})
        assert len(mem["learned"]) == 1

    def test_dedup_with_punctuation(self):
        from server.agent_builder import apply_module_memory_updates
        mem = {"learned": [
            {"note": "Юзер любит списки!", "type": "preference", "ts": "old"},
        ]}
        # Дубль с другой пунктуацией — также skip
        apply_module_memory_updates(mem, {"items": [
            {"scope": "module", "type": "preference", "note": "Юзер любит списки."},
        ]})
        assert len(mem["learned"]) == 1

    def test_promotion_to_profile_facts(self):
        from server.agent_builder import apply_module_memory_updates
        mem = {}
        profile = {"facts": []}
        apply_module_memory_updates(mem, {"items": [
            {"scope": "global", "type": "fact", "note": "ведёт b2b-стройку"},
        ]}, profile=profile)
        # И в memory, и в profile.facts
        assert len(mem["learned"]) == 1
        assert len(profile["facts"]) == 1
        assert profile["facts"][0]["key"] == "fact"
        assert "b2b-стройку" in profile["facts"][0]["value"]
        assert profile["facts"][0]["source"] == "module_learned"

    def test_no_promotion_without_profile(self):
        from server.agent_builder import apply_module_memory_updates
        mem = {}
        # profile не передан → global сохраняется в memory как обычно, но не promoted
        apply_module_memory_updates(mem, {"items": [
            {"scope": "global", "type": "fact", "note": "X"},
        ]})
        assert len(mem["learned"]) == 1

    def test_profile_dedup(self):
        from server.agent_builder import apply_module_memory_updates
        profile = {"facts": [
            {"key": "fact", "value": "Ведёт стройку", "ts": "old"}
        ]}
        mem = {}
        apply_module_memory_updates(mem, {"items": [
            {"scope": "global", "type": "fact", "note": "ведёт стройку"},
        ]}, profile=profile)
        # В profile.facts уже было — не дублируется (case-insensitive)
        assert len(profile["facts"]) == 1

    def test_limit_5_per_call(self):
        from server.agent_builder import apply_module_memory_updates
        mem = {}
        apply_module_memory_updates(mem, {"items": [
            {"scope": "module", "type": "note", "note": f"note_{i}"}
            for i in range(10)
        ]})
        assert len(mem["learned"]) == 5

    def test_cap_50_total(self):
        from server.agent_builder import apply_module_memory_updates
        # Заполняем memory до 49, добавляем 5 — должно остаться 50 (отсекли старые)
        mem = {"learned": [
            {"note": f"old_{i}", "type": "note", "ts": f"t{i:03d}"}
            for i in range(49)
        ]}
        apply_module_memory_updates(mem, {"items": [
            {"scope": "module", "type": "note", "note": f"new_{i}"}
            for i in range(5)
        ]})
        # +5 - 4 отсечено = 50
        assert len(mem["learned"]) == 50
        # Самые свежие («new_*») должны остаться
        notes_str = " ".join(L["note"] for L in mem["learned"])
        assert "new_4" in notes_str


# ── _format_adaptive_rules ─────────────────────────────────────────────────


class TestFormatRules:
    def test_empty_memory(self):
        from server.agent_builder import _format_adaptive_rules
        out = _format_adaptive_rules({})
        assert "ничего ещё не выучено" in out.lower()

    def test_grouping_by_type(self):
        from server.agent_builder import _format_adaptive_rules
        mem = {"learned": [
            {"note": "деловой тон", "type": "style", "ts": "2026-05-01"},
            {"note": "не упоминать конкурентов", "type": "constraint", "ts": "2026-05-02"},
            {"note": "ведёт b2b-стройку", "type": "fact", "ts": "2026-05-03"},
            {"note": "короткие списки", "type": "preference", "ts": "2026-05-04"},
        ]}
        out = _format_adaptive_rules(mem)
        # Все заголовки должны присутствовать (в правильном порядке)
        assert "📝 Стиль" in out
        assert "💛 Предпочтения" in out
        assert "🚫 Ограничения" in out
        assert "📋 Факты" in out
        # Style идёт раньше constraints
        assert out.index("Стиль") < out.index("Ограничения")
        # Numbered list
        assert "1." in out

    def test_legacy_blocks_kept(self):
        """Старые верхнеуровневые ключи style/preferences/constraints — показываются как legacy."""
        from server.agent_builder import _format_adaptive_rules
        mem = {
            "style": "Деловой",
            "preferences": "Короткие списки",
            "learned": [],
        }
        out = _format_adaptive_rules(mem)
        assert "legacy" in out.lower()
        assert "Деловой" in out

    def test_only_old_format_notes(self):
        """Backward compat: learned items без type попадают в группу 'note'."""
        from server.agent_builder import _format_adaptive_rules
        mem = {"learned": [
            {"note": "старая заметка без типа", "ts": "old"},
        ]}
        out = _format_adaptive_rules(mem)
        assert "Заметки" in out
        assert "старая заметка" in out

    def test_sorted_by_ts_desc(self):
        from server.agent_builder import _format_adaptive_rules
        mem = {"learned": [
            {"note": "старая", "type": "style", "ts": "2026-01-01"},
            {"note": "свежая", "type": "style", "ts": "2026-05-01"},
        ]}
        out = _format_adaptive_rules(mem)
        # «свежая» идёт первой (1.), «старая» — второй (2.)
        idx_svezh = out.index("свежая")
        idx_star = out.index("старая")
        assert idx_svezh < idx_star
