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

    def test_make_key_default_per_user_now(self):
        """SECURITY: с фиксом #29 по дефолту все вызовы с _user_id —
        per-user namespace. Защита от cross-user leak через кэшированный
        RAG-ответ. Опт-аут — явный _cache_scope='global'."""
        from server.llm_cache import _make_cache_key
        msgs = [{"role": "user", "content": "сколько будет 2+2"}]
        # По умолчанию — per-user
        k_u1 = _make_cache_key("claude-sonnet", msgs,
                               {"_purpose": "chat", "_user_id": 1})
        k_u2 = _make_cache_key("claude-sonnet", msgs,
                               {"_purpose": "chat", "_user_id": 2})
        assert k_u1 != k_u2
        # Явный global scope — снова shared
        k_g1 = _make_cache_key("claude-sonnet", msgs,
                               {"_purpose": "chat", "_user_id": 1,
                                "_cache_scope": "global"})
        k_g2 = _make_cache_key("claude-sonnet", msgs,
                               {"_purpose": "chat", "_user_id": 2,
                                "_cache_scope": "global"})
        assert k_g1 == k_g2

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


# ── Cron parser (agents-modules runtime) ──────────────────────────────────────


class TestCronParser:
    def test_match_field_star(self):
        from server.cron.agents_modules import _match_field
        assert _match_field(5, "*")
        assert _match_field(0, "*")
        assert _match_field(59, "*")

    def test_match_field_exact(self):
        from server.cron.agents_modules import _match_field
        assert _match_field(9, "9")
        assert not _match_field(10, "9")

    def test_match_field_range(self):
        from server.cron.agents_modules import _match_field
        assert _match_field(1, "1-5")
        assert _match_field(5, "1-5")
        assert _match_field(3, "1-5")
        assert not _match_field(6, "1-5")
        assert not _match_field(0, "1-5")

    def test_match_field_list(self):
        from server.cron.agents_modules import _match_field
        assert _match_field(1, "1,3,5")
        assert _match_field(5, "1,3,5")
        assert not _match_field(2, "1,3,5")

    def test_match_field_step(self):
        from server.cron.agents_modules import _match_field
        assert _match_field(0, "*/15")
        assert _match_field(15, "*/15")
        assert _match_field(30, "*/15")
        assert not _match_field(7, "*/15")

    def test_cron_daily_9am(self):
        """`0 9 * * *` — каждый день в 9:00."""
        from datetime import datetime
        from server.cron.agents_modules import cron_should_fire
        # 9:00 — fire
        now = datetime(2026, 5, 16, 9, 0)
        assert cron_should_fire("0 9 * * *", now, None)
        # 9:01 — нет (не та минута)
        assert not cron_should_fire("0 9 * * *", datetime(2026, 5, 16, 9, 1), None)
        # 10:00 — нет (не тот час)
        assert not cron_should_fire("0 9 * * *", datetime(2026, 5, 16, 10, 0), None)

    def test_cron_no_double_fire_in_minute(self):
        """Если уже стреляли в этой минуте — не повторяем."""
        from datetime import datetime
        from server.cron.agents_modules import cron_should_fire
        now = datetime(2026, 5, 16, 9, 0)
        # last_fired = now - 30s → не fire
        from datetime import timedelta
        last = now - timedelta(seconds=30)
        assert not cron_should_fire("0 9 * * *", now, last)
        # last_fired = вчера → fire
        last_old = now - timedelta(days=1)
        assert cron_should_fire("0 9 * * *", now, last_old)

    def test_cron_weekdays(self):
        """`0 9 * * 1-5` — пн-пт 9:00."""
        from datetime import datetime
        from server.cron.agents_modules import cron_should_fire
        # 2026-05-18 — понедельник (ISO weekday=1)
        mon = datetime(2026, 5, 18, 9, 0)
        assert mon.isoweekday() == 1
        assert cron_should_fire("0 9 * * 1-5", mon, None)
        # 2026-05-17 — воскресенье — не fire
        sun = datetime(2026, 5, 17, 9, 0)
        assert sun.isoweekday() == 7
        assert not cron_should_fire("0 9 * * 1-5", sun, None)

    def test_cron_every_30min(self):
        """`*/30 * * * *` — раз в 30 мин."""
        from datetime import datetime
        from server.cron.agents_modules import cron_should_fire
        assert cron_should_fire("*/30 * * * *", datetime(2026, 5, 16, 10, 0), None)
        assert cron_should_fire("*/30 * * * *", datetime(2026, 5, 16, 10, 30), None)
        assert not cron_should_fire("*/30 * * * *", datetime(2026, 5, 16, 10, 15), None)

    def test_cron_invalid_returns_false(self):
        from datetime import datetime
        from server.cron.agents_modules import cron_should_fire
        now = datetime(2026, 5, 16, 9, 0)
        assert not cron_should_fire("invalid", now, None)
        assert not cron_should_fire("", now, None)
        assert not cron_should_fire(None, now, None)
        assert not cron_should_fire("0 9 * *", now, None)  # 4 поля


# ── Atomic interaction_count increment (multi-worker race protection) ────────


class TestIncrementModuleInteraction:
    """Регрессионные тесты: interaction_count должен инкрементиться через
    SQL UPDATE, а не Python RMW. На multi-worker (prod = 4) воркеры могут
    одновременно прочитать 29 и записать 30 — терялись инкременты."""

    def _make_module(self):
        """Создать тестового юзера + агента + модуль. Возвращает (db_session_cm, m)."""
        import time
        from server.db import db_session
        from server.models import User, Agent, AgentModule

        with db_session() as db:
            u = User(
                email=f"inc-test-{time.time_ns()}@x.x",
                password_hash="hash",
                is_verified=True,
                agreed_to_terms=True,
                tokens_balance=0,
            )
            db.add(u); db.commit(); db.refresh(u)
            a = Agent(user_id=u.id, name="Test", status="active",
                      profile_json="{}", personality_json="{}")
            db.add(a); db.commit(); db.refresh(a)
            m = AgentModule(agent_id=a.id, slug="copywriter", level=0,
                            is_enabled=True, interaction_count=0)
            db.add(m); db.commit(); db.refresh(m)
            return u.id, a.id, m.id

    def _cleanup(self, user_id, agent_id, module_id):
        from server.db import db_session
        from server.models import User, Agent, AgentModule
        with db_session() as db:
            db.query(AgentModule).filter_by(id=module_id).delete()
            db.query(Agent).filter_by(id=agent_id).delete()
            db.query(User).filter_by(id=user_id).delete()
            db.commit()

    def test_increment_returns_new_value(self):
        from server.db import db_session
        from server.models import AgentModule
        from server.agent_builder import increment_module_interaction

        uid, aid, mid = self._make_module()
        try:
            with db_session() as db:
                m = db.query(AgentModule).filter_by(id=mid).first()
                assert m.interaction_count == 0
                new = increment_module_interaction(db, m)
                assert new == 1
                assert m.interaction_count == 1, "refresh должен подтянуть значение"
                new2 = increment_module_interaction(db, m)
                assert new2 == 2
        finally:
            self._cleanup(uid, aid, mid)

    def test_increment_persists_to_db(self):
        """Значение должно быть видно из другой сессии (= другого воркера)."""
        from server.db import db_session
        from server.models import AgentModule
        from server.agent_builder import increment_module_interaction

        uid, aid, mid = self._make_module()
        try:
            with db_session() as db1:
                m1 = db1.query(AgentModule).filter_by(id=mid).first()
                increment_module_interaction(db1, m1)
                increment_module_interaction(db1, m1)
                increment_module_interaction(db1, m1)
                db1.commit()

            with db_session() as db2:
                m2 = db2.query(AgentModule).filter_by(id=mid).first()
                assert m2.interaction_count == 3, (
                    f"Через другую сессию должно быть видно 3, видно {m2.interaction_count}"
                )
        finally:
            self._cleanup(uid, aid, mid)

    def test_increment_does_not_lose_other_changes(self):
        """Pending changes на модуль (last_used_at, memory) — не теряются при flush+refresh."""
        from datetime import datetime
        from server.db import db_session
        from server.models import AgentModule
        from server.agent_builder import increment_module_interaction

        uid, aid, mid = self._make_module()
        try:
            with db_session() as db:
                m = db.query(AgentModule).filter_by(id=mid).first()
                # Эти изменения нужно зафлэшить ДО UPDATE, чтобы не потерять
                marker = datetime(2026, 5, 18, 12, 0, 0)
                m.last_used_at = marker
                m.module_memory_json = '{"learned": [{"note": "marker"}]}'
                # Атомарный +1
                new = increment_module_interaction(db, m)
                assert new == 1
                # И при этом наши изменения остались
                assert m.last_used_at == marker
                assert "marker" in (m.module_memory_json or "")
                db.commit()
            # Проверим в новой сессии
            with db_session() as db2:
                m2 = db2.query(AgentModule).filter_by(id=mid).first()
                assert m2.last_used_at == marker
                assert "marker" in (m2.module_memory_json or "")
                assert m2.interaction_count == 1
        finally:
            self._cleanup(uid, aid, mid)


class TestModuleLimit:
    """Регрессионные тесты на лимит подключённых модулей.
    Конкретный лимит из pricing_config — `agents.max_enabled_modules` (default 12).
    """

    def test_limit_default_from_pricing(self):
        from server.pricing import get_price
        v = get_price("agents.max_enabled_modules", default=12)
        # Должен быть положительный int. Дефолт = 12.
        assert isinstance(v, int) and v > 0
        assert v == 12, "default не 12 — обнови docs или этот тест"
