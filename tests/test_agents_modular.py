"""Smoke-тесты для модульных ИИ Агентов (модуль 23).

Проверяют ключевую инфру:
  - LLM Router pick_model + classify_task
  - LLM Cache key (per-user namespacing)
  - Agent Builder _extract_json robust parsing
  - compute_module_level — прокачка L0→L4
  - PrivacyGuard mask/unmask round-trip
  - apply_module_memory_updates капы и дедупликация
  - _dump_meta лимит на размер meta_json

Здесь НЕТ интеграционных тестов через TestClient (требуют DB + auth setup).
LLM-вызовы замоканы / не делаются — это unit-уровень.
"""
import json
import pytest


# ── LLM Router ────────────────────────────────────────────────────────────────


class TestLLMRouter:
    def test_classify_task_keywords(self):
        from server.llm_router import classify_task
        assert classify_task("напиши код на python") == "code"
        assert classify_task("напиши пост для инсты") == "creative_writing"
        assert classify_task("проверь факт про XYZ") == "factcheck"
        assert classify_task("что сейчас в twitter") == "realtime"
        assert classify_task("найди статистику") == "research"
        assert classify_task("привет") == "default"
        assert classify_task("") == "default"

    def test_detect_complexity(self):
        from server.llm_router import detect_complexity
        assert detect_complexity("") == "simple"
        assert detect_complexity("x" * 100) == "simple"
        assert detect_complexity("x" * 1000) == "medium"
        assert detect_complexity("x" * 5000) == "complex"
        assert detect_complexity("x", has_attachments=True) == "complex"

    def test_pick_model_returns_string(self):
        from server.llm_router import pick_model
        m = pick_model("creative_writing", "medium")
        assert isinstance(m, str) and m

    def test_pick_model_invalid_falls_back(self):
        from server.llm_router import pick_model
        # Неизвестный task_type → default matrix
        m = pick_model("nonexistent_task", "medium")
        assert isinstance(m, str) and m


# ── LLM Cache ─────────────────────────────────────────────────────────────────


class TestLLMCache:
    def test_make_key_stable(self):
        from server.llm_cache import _make_cache_key
        msgs = [{"role": "user", "content": "привет"}]
        k1 = _make_cache_key("claude-sonnet", msgs, {"temperature": 0.3})
        k2 = _make_cache_key("claude-sonnet", msgs, {"temperature": 0.3})
        assert k1 == k2
        assert len(k1) == 64  # sha256 hex

    def test_make_key_per_user_for_personal_agent(self):
        """Personal-agent должен делать ключ per-user — иначе утечка между юзерами."""
        from server.llm_cache import _make_cache_key
        msgs = [{"role": "user", "content": "привет"}]
        k_u1 = _make_cache_key("claude-sonnet", msgs,
                               {"_purpose": "personal_agent", "_user_id": 1})
        k_u2 = _make_cache_key("claude-sonnet", msgs,
                               {"_purpose": "personal_agent", "_user_id": 2})
        assert k_u1 != k_u2, "Cache key должен различаться между юзерами"

    def test_make_key_per_user_for_module_purpose(self):
        """Purpose module:smm тоже должен быть per-user."""
        from server.llm_cache import _make_cache_key
        msgs = [{"role": "user", "content": "напиши пост"}]
        k_u1 = _make_cache_key("claude-sonnet", msgs,
                               {"_purpose": "module:smm", "_user_id": 1})
        k_u2 = _make_cache_key("claude-sonnet", msgs,
                               {"_purpose": "module:smm", "_user_id": 2})
        assert k_u1 != k_u2

    def test_make_key_chat_purpose_NOT_per_user(self):
        """Обычный chat-purpose — общий ключ, кэш shared между юзерами."""
        from server.llm_cache import _make_cache_key
        msgs = [{"role": "user", "content": "сколько будет 2+2"}]
        k_u1 = _make_cache_key("claude-sonnet", msgs,
                               {"_purpose": "chat", "_user_id": 1})
        k_u2 = _make_cache_key("claude-sonnet", msgs,
                               {"_purpose": "chat", "_user_id": 2})
        assert k_u1 == k_u2  # Старое поведение для обычного chat

    def test_should_not_cache_time_sensitive(self):
        from server.llm_cache import _should_cache
        assert not _should_cache("x", [{"role": "user", "content": "что сейчас?"}], {})
        assert not _should_cache("x", [{"role": "user", "content": "today's news"}], {})

    def test_should_not_cache_high_temp(self):
        from server.llm_cache import _should_cache
        msgs = [{"role": "user", "content": "hello"}]
        assert _should_cache("x", msgs, {"temperature": 0.3})
        assert not _should_cache("x", msgs, {"temperature": 0.8})


# ── Agent Builder _extract_json ───────────────────────────────────────────────


class TestExtractJson:
    def test_plain_json(self):
        from server.agent_builder import _extract_json
        r = _extract_json('{"reply":"hi","actions":[]}')
        assert r == {"reply": "hi", "actions": []}

    def test_with_markdown_wrapper(self):
        from server.agent_builder import _extract_json
        r = _extract_json('```json\n{"reply":"hi"}\n```')
        assert r == {"reply": "hi"}

    def test_with_text_before(self):
        from server.agent_builder import _extract_json
        r = _extract_json('Вот мой ответ: {"reply":"hi"}')
        assert r == {"reply": "hi"}

    def test_balanced_braces_with_nested(self):
        """Если LLM добавил мусор после JSON — берём первый сбалансированный объект."""
        from server.agent_builder import _extract_json
        r = _extract_json('{"reply":"hi","data":{"x":1}} а вот пример {другой}')
        assert r is not None
        assert r.get("reply") == "hi"
        assert r.get("data") == {"x": 1}

    def test_escaped_quotes_in_string(self):
        from server.agent_builder import _extract_json
        r = _extract_json('{"reply":"он сказал \\"привет\\""}')
        assert r == {"reply": 'он сказал "привет"'}

    def test_empty(self):
        from server.agent_builder import _extract_json
        assert _extract_json("") is None
        assert _extract_json("not json at all") is None

    def test_braces_in_string(self):
        """Скобка `{` внутри строки не должна сбивать счётчик."""
        from server.agent_builder import _extract_json
        r = _extract_json('{"reply":"json: {nested}", "x":1}')
        assert r == {"reply": "json: {nested}", "x": 1}


# ── Прокачка модулей ──────────────────────────────────────────────────────────


class TestModuleLevels:
    def test_l0_no_promotion_when_inactive(self):
        from server.agent_builder import compute_module_level
        assert compute_module_level(current_level=0, interaction_count=10,
                                    agent_status="onboarding", learned_count=5) == 0

    def test_l0_to_l1(self):
        from server.agent_builder import compute_module_level
        assert compute_module_level(current_level=0, interaction_count=5,
                                    agent_status="active", learned_count=0) == 1

    def test_l1_to_l2_requires_learned(self):
        from server.agent_builder import compute_module_level
        # 30 взаимодействий но мало заученного → остаёмся на L1
        assert compute_module_level(current_level=1, interaction_count=30,
                                    agent_status="active", learned_count=2) == 1
        # 30+ и learned >= 3 → L2
        assert compute_module_level(current_level=1, interaction_count=30,
                                    agent_status="active", learned_count=3) == 2

    def test_never_downgrades(self):
        from server.agent_builder import compute_module_level
        # Если current_level=3, всё что меньше — игнорируем (не понижаем)
        assert compute_module_level(current_level=3, interaction_count=1,
                                    agent_status="active", learned_count=0) == 3

    def test_l3_l4_not_auto(self):
        from server.agent_builder import compute_module_level
        # L4 нельзя получить автоматически
        assert compute_module_level(current_level=3, interaction_count=10_000,
                                    agent_status="active", learned_count=999) == 3


# ── PrivacyGuard round-trip ───────────────────────────────────────────────────


class TestPrivacyGuard:
    def test_phone_round_trip(self):
        from server.privacy_guard import PrivacyGuard
        g = PrivacyGuard()
        masked = g.mask("Звоните +7 999 123 45 67")
        assert "+7" not in masked
        assert "PHONE" in masked
        # unmask возвращает обратно
        restored = g.unmask(masked)
        # Может быть без пробелов — проверяем что номер обратно поставлен
        assert "+7" in restored or "999" in restored

    def test_email_round_trip(self):
        from server.privacy_guard import PrivacyGuard
        g = PrivacyGuard()
        masked = g.mask("Пиши на test@example.com")
        assert "test@example.com" not in masked
        assert "EMAIL" in masked
        assert g.unmask_response(masked).find("test@example.com") >= 0


# ── module memory updates ────────────────────────────────────────────────────


class TestModuleMemory:
    def test_apply_adds_notes(self):
        from server.agent_builder import apply_module_memory_updates
        mem = {}
        apply_module_memory_updates(mem, {"new_notes": ["юзер пишет без эмодзи"]})
        assert len(mem["learned"]) == 1
        assert mem["learned"][0]["note"] == "юзер пишет без эмодзи"

    def test_cap_at_50(self):
        from server.agent_builder import apply_module_memory_updates
        mem = {"learned": [{"note": f"old {i}"} for i in range(48)]}
        apply_module_memory_updates(mem, {"new_notes": ["a", "b", "c", "d", "e"]})
        # Должно быть не больше 50, старые срезаются
        assert len(mem["learned"]) == 50
        # Последние 5 — новые
        last_notes = [n["note"] for n in mem["learned"][-5:]]
        assert "e" in last_notes

    def test_limit_per_call(self):
        """Лимит 5 заметок за один вызов."""
        from server.agent_builder import apply_module_memory_updates
        mem = {}
        apply_module_memory_updates(mem,
            {"new_notes": ["n1", "n2", "n3", "n4", "n5", "n6", "n7"]})
        assert len(mem["learned"]) == 5


# ── _dump_meta size cap ──────────────────────────────────────────────────────


class TestDumpMeta:
    def test_small_meta_unchanged(self):
        from server.routes.agents_modular import _dump_meta
        meta = {"mode": "active", "slug": "smm"}
        s = _dump_meta(meta)
        assert json.loads(s) == meta

    def test_large_meta_trimmed(self):
        from server.routes.agents_modular import _dump_meta
        meta = {
            "mode": "active",
            "slug": "smm",
            "raw": "x" * 20000,  # огромный raw
            "applied": ["a" * 200] * 100,
        }
        s = _dump_meta(meta)
        # Не должен превышать лимит
        assert len(s.encode("utf-8")) <= 8500  # с запасом
        parsed = json.loads(s)
        # Критичные поля остались
        assert parsed.get("mode") == "active"
        assert parsed.get("slug") == "smm"


# ── Rate limit ───────────────────────────────────────────────────────────────


class TestRateLimit:
    def test_under_limit(self):
        # Импорт после установки env (через conftest)
        from server.routes import agents_modular as am
        # Свежий юзер
        uid = 999_001
        am._RL_BUCKETS.pop(uid, None)
        ok, _ = am._rate_limit_check(uid)
        assert ok

    def test_burst_blocked(self):
        from server.routes import agents_modular as am
        uid = 999_002
        am._RL_BUCKETS.pop(uid, None)
        # Пушим до лимита
        for _ in range(am._RL_MSG_PER_MIN):
            ok, _ = am._rate_limit_check(uid)
            assert ok
        # Следующий — отказ
        ok, reason = am._rate_limit_check(uid)
        assert not ok
        assert "мин" in reason
