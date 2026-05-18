"""Тесты двустороннего моста Креаторы ↔ модуль copywriter ИИ-Агента.

Проверяют:
  - load_copywriter_examples возвращает последние N постов из module_memory
  - save_published_to_copywriter добавляет пост в memory + cap'ит до 10
  - build_style_block формирует валидную секцию для system prompt
  - Если copywriter-модуль не подключён — функции no-op'ят
  - Сохранение и загрузка работают сквозь сессии (другой воркер видит изменения)
"""
import json
import time
import pytest


def _make_user_agent_with_optional_copywriter(connect_copywriter: bool):
    """Создать User + Agent. Если connect_copywriter — добавить AgentModule copywriter.
    Возвращает (user_id, agent_id, module_id|None) — для cleanup."""
    from server.db import db_session
    from server.models import User, Agent, AgentModule

    with db_session() as db:
        u = User(
            email=f"bridge-{time.time_ns()}@x.x",
            password_hash="hash", is_verified=True,
            agreed_to_terms=True, tokens_balance=0,
        )
        db.add(u); db.commit(); db.refresh(u)
        a = Agent(user_id=u.id, name="Test", status="active",
                  profile_json="{}", personality_json="{}")
        db.add(a); db.commit(); db.refresh(a)
        mid = None
        if connect_copywriter:
            m = AgentModule(agent_id=a.id, slug="copywriter", level=0,
                            is_enabled=True, interaction_count=0,
                            module_memory_json=json.dumps({}, ensure_ascii=False))
            db.add(m); db.commit(); db.refresh(m)
            mid = m.id
        return u.id, a.id, mid


def _cleanup(user_id, agent_id, module_id):
    from server.db import db_session
    from server.models import User, Agent, AgentModule
    with db_session() as db:
        if module_id is not None:
            db.query(AgentModule).filter_by(id=module_id).delete()
        db.query(Agent).filter_by(id=agent_id).delete()
        db.query(User).filter_by(id=user_id).delete()
        db.commit()


class TestBuildStyleBlock:
    """build_style_block — независимый от БД."""

    def test_empty_returns_empty_string(self):
        from server.creators_copywriter_bridge import build_style_block
        assert build_style_block([]) == ""
        assert build_style_block(None) == ""  # type: ignore

    def test_with_examples_contains_text(self):
        from server.creators_copywriter_bridge import build_style_block
        block = build_style_block([
            {"text": "Привет, друзья! Сегодня новый рецепт 🍰",
             "platform": "tg", "ts": "2026-05-18T10:00:00"},
            {"text": "Уважаемые клиенты, мы открываем филиал.",
             "platform": "vk", "ts": "2026-05-17T15:00:00"},
        ])
        assert "ПРИМЕРЫ СТИЛЯ" in block
        assert "новый рецепт" in block
        assert "филиал" in block
        assert "tg" in block and "vk" in block


class TestLoadCopywriterExamples:
    def test_no_module_returns_empty(self):
        from server.db import db_session
        from server.creators_copywriter_bridge import load_copywriter_examples

        uid, aid, _ = _make_user_agent_with_optional_copywriter(connect_copywriter=False)
        try:
            with db_session() as db:
                examples = load_copywriter_examples(db, uid)
                assert examples == []
        finally:
            _cleanup(uid, aid, None)

    def test_empty_module_returns_empty(self):
        """Модуль подключён, но в memory ещё ничего нет."""
        from server.db import db_session
        from server.creators_copywriter_bridge import load_copywriter_examples

        uid, aid, mid = _make_user_agent_with_optional_copywriter(connect_copywriter=True)
        try:
            with db_session() as db:
                examples = load_copywriter_examples(db, uid)
                assert examples == []
        finally:
            _cleanup(uid, aid, mid)

    def test_returns_recent_first_with_limit(self):
        """Возвращает последние N сортируя по ts desc."""
        from server.db import db_session
        from server.models import AgentModule
        from server.creators_copywriter_bridge import load_copywriter_examples

        uid, aid, mid = _make_user_agent_with_optional_copywriter(connect_copywriter=True)
        try:
            # Засеиваем memory вручную
            with db_session() as db:
                m = db.query(AgentModule).filter_by(id=mid).first()
                m.module_memory_json = json.dumps({"examples": [
                    {"text": "старый", "platform": "tg", "ts": "2026-01-01T00:00:00"},
                    {"text": "средний", "platform": "vk", "ts": "2026-03-01T00:00:00"},
                    {"text": "свежий", "platform": "tg", "ts": "2026-05-18T10:00:00"},
                    {"text": "ещё свежее", "platform": "tg", "ts": "2026-05-19T00:00:00"},
                ]}, ensure_ascii=False)
                db.commit()

            with db_session() as db:
                ex = load_copywriter_examples(db, uid, limit=2)
                assert len(ex) == 2
                assert ex[0]["text"] == "ещё свежее"
                assert ex[1]["text"] == "свежий"
        finally:
            _cleanup(uid, aid, mid)


class TestSavePublishedToCopywriter:
    def test_no_module_returns_false(self):
        from server.db import db_session
        from server.creators_copywriter_bridge import save_published_to_copywriter

        uid, aid, _ = _make_user_agent_with_optional_copywriter(connect_copywriter=False)
        try:
            with db_session() as db:
                ok = save_published_to_copywriter(db, uid, "пост", "tg")
                assert ok is False
        finally:
            _cleanup(uid, aid, None)

    def test_empty_text_returns_false(self):
        from server.db import db_session
        from server.creators_copywriter_bridge import save_published_to_copywriter

        uid, aid, mid = _make_user_agent_with_optional_copywriter(connect_copywriter=True)
        try:
            with db_session() as db:
                assert save_published_to_copywriter(db, uid, "", "tg") is False
                assert save_published_to_copywriter(db, uid, "   ", "tg") is False
        finally:
            _cleanup(uid, aid, mid)

    def test_save_persists_across_sessions(self):
        """Сохранили в одной сессии — другая видит."""
        from server.db import db_session
        from server.models import AgentModule
        from server.creators_copywriter_bridge import (
            save_published_to_copywriter, load_copywriter_examples,
        )

        uid, aid, mid = _make_user_agent_with_optional_copywriter(connect_copywriter=True)
        try:
            with db_session() as db:
                ok = save_published_to_copywriter(db, uid, "Привет, друзья!", "tg")
                assert ok is True

            with db_session() as db2:
                ex = load_copywriter_examples(db2, uid)
                assert len(ex) == 1
                assert ex[0]["text"] == "Привет, друзья!"
                assert ex[0]["platform"] == "tg"
                assert ex[0]["ts"]  # timestamp проставлен
        finally:
            _cleanup(uid, aid, mid)

    def test_save_caps_at_examples_cap(self):
        """Хранится максимум EXAMPLES_CAP (10) — самые свежие. Per-brand."""
        from server.db import db_session
        from server.models import AgentModule
        from server.creators_copywriter_bridge import (
            save_published_to_copywriter, EXAMPLES_CAP,
        )

        uid, aid, mid = _make_user_agent_with_optional_copywriter(connect_copywriter=True)
        try:
            with db_session() as db:
                for i in range(EXAMPLES_CAP + 5):
                    save_published_to_copywriter(db, uid, f"пост {i}", "tg")

            with db_session() as db2:
                m = db2.query(AgentModule).filter_by(id=mid).first()
                memory = json.loads(m.module_memory_json or "{}")
                # B-3: записи в examples_by_brand._default (brand_id не указан)
                by_brand = memory.get("examples_by_brand") or {}
                bucket = by_brand.get("_default") or []
                assert len(bucket) == EXAMPLES_CAP
                texts = [e["text"] for e in bucket]
                assert f"пост {EXAMPLES_CAP + 4}" in texts
                assert "пост 0" not in texts
        finally:
            _cleanup(uid, aid, mid)

    def test_save_truncates_long_post(self):
        """Длинные посты обрезаются (EXAMPLE_TEXT_TRUNC)."""
        from server.db import db_session
        from server.creators_copywriter_bridge import (
            save_published_to_copywriter, load_copywriter_examples,
            EXAMPLE_TEXT_TRUNC,
        )

        uid, aid, mid = _make_user_agent_with_optional_copywriter(connect_copywriter=True)
        try:
            long_text = "X" * (EXAMPLE_TEXT_TRUNC + 500)
            with db_session() as db:
                save_published_to_copywriter(db, uid, long_text, "vk")

            with db_session() as db2:
                ex = load_copywriter_examples(db2, uid, limit=1)
                assert len(ex[0]["text"]) <= EXAMPLE_TEXT_TRUNC
        finally:
            _cleanup(uid, aid, mid)


class TestPerBrandLearning:
    """B-3: per-brand изоляция examples (один tone на бренд)."""

    def test_save_with_brand_id_isolated_buckets(self):
        """Посты разных брендов хранятся в разных bucket'ах."""
        from server.db import db_session
        from server.models import AgentModule
        from server.creators_copywriter_bridge import save_published_to_copywriter

        uid, aid, mid = _make_user_agent_with_optional_copywriter(connect_copywriter=True)
        try:
            with db_session() as db:
                save_published_to_copywriter(db, uid, "стройка пост 1", "vk", brand_id=11)
                save_published_to_copywriter(db, uid, "мама-блог пост", "tg", brand_id=22)
                save_published_to_copywriter(db, uid, "стройка пост 2", "vk", brand_id=11)

            with db_session() as db2:
                m = db2.query(AgentModule).filter_by(id=mid).first()
                by_brand = json.loads(m.module_memory_json)["examples_by_brand"]
                assert "11" in by_brand and "22" in by_brand
                assert len(by_brand["11"]) == 2
                assert len(by_brand["22"]) == 1
                assert all("стройка" in e["text"] for e in by_brand["11"])
                assert "мама" in by_brand["22"][0]["text"]
        finally:
            _cleanup(uid, aid, mid)

    def test_load_filters_by_brand(self):
        """load_copywriter_examples(brand_id=X) вернёт ТОЛЬКО посты бренда X."""
        from server.db import db_session
        from server.creators_copywriter_bridge import (
            save_published_to_copywriter, load_copywriter_examples,
        )

        uid, aid, mid = _make_user_agent_with_optional_copywriter(connect_copywriter=True)
        try:
            with db_session() as db:
                save_published_to_copywriter(db, uid, "стройка-1", "vk", brand_id=11)
                save_published_to_copywriter(db, uid, "стройка-2", "vk", brand_id=11)
                save_published_to_copywriter(db, uid, "мама-1", "tg", brand_id=22)

            with db_session() as db2:
                stroika = load_copywriter_examples(db2, uid, brand_id=11)
                mama = load_copywriter_examples(db2, uid, brand_id=22)
                assert len(stroika) == 2
                assert all("стройка" in e["text"] for e in stroika)
                assert len(mama) == 1
                assert "мама" in mama[0]["text"]
        finally:
            _cleanup(uid, aid, mid)

    def test_load_unknown_brand_falls_back_to_legacy(self):
        """Если для бренда ещё ничего нет — fallback на legacy examples."""
        from server.db import db_session
        from server.models import AgentModule
        from server.creators_copywriter_bridge import load_copywriter_examples

        uid, aid, mid = _make_user_agent_with_optional_copywriter(connect_copywriter=True)
        try:
            # Засеиваем ТОЛЬКО legacy examples
            with db_session() as db:
                m = db.query(AgentModule).filter_by(id=mid).first()
                m.module_memory_json = json.dumps({"examples": [
                    {"text": "старый пост", "platform": "tg", "ts": "2026-01-01"},
                ]}, ensure_ascii=False)
                db.commit()

            with db_session() as db2:
                # Запрос для нового бренда — должен взять legacy
                ex = load_copywriter_examples(db2, uid, brand_id=99)
                assert len(ex) == 1
                assert ex[0]["text"] == "старый пост"
        finally:
            _cleanup(uid, aid, mid)

    def test_load_brand_with_data_ignores_legacy(self):
        """Если у бренда УЖЕ есть свои посты — legacy не подмешивается."""
        from server.db import db_session
        from server.models import AgentModule
        from server.creators_copywriter_bridge import (
            save_published_to_copywriter, load_copywriter_examples,
        )

        uid, aid, mid = _make_user_agent_with_optional_copywriter(connect_copywriter=True)
        try:
            # Засеиваем legacy + новый бренд
            with db_session() as db:
                m = db.query(AgentModule).filter_by(id=mid).first()
                m.module_memory_json = json.dumps({"examples": [
                    {"text": "legacy", "platform": "tg", "ts": "2026-01-01"},
                ]}, ensure_ascii=False)
                db.commit()
            with db_session() as db:
                save_published_to_copywriter(db, uid, "новый бренд-пост", "vk", brand_id=11)

            with db_session() as db2:
                ex = load_copywriter_examples(db2, uid, brand_id=11)
                # Только новый, без legacy
                assert len(ex) == 1
                assert ex[0]["text"] == "новый бренд-пост"
        finally:
            _cleanup(uid, aid, mid)

    def test_per_brand_cap_independent(self):
        """Cap (10) применяется к каждому бренду отдельно — не суммарно."""
        from server.db import db_session
        from server.models import AgentModule
        from server.creators_copywriter_bridge import (
            save_published_to_copywriter, EXAMPLES_CAP,
        )

        uid, aid, mid = _make_user_agent_with_optional_copywriter(connect_copywriter=True)
        try:
            with db_session() as db:
                for i in range(EXAMPLES_CAP + 3):
                    save_published_to_copywriter(db, uid, f"br1-{i}", "tg", brand_id=1)
                for i in range(EXAMPLES_CAP + 3):
                    save_published_to_copywriter(db, uid, f"br2-{i}", "vk", brand_id=2)

            with db_session() as db2:
                m = db2.query(AgentModule).filter_by(id=mid).first()
                by_brand = json.loads(m.module_memory_json)["examples_by_brand"]
                assert len(by_brand["1"]) == EXAMPLES_CAP
                assert len(by_brand["2"]) == EXAMPLES_CAP
                # Самые свежие — оба
                assert any(f"br1-{EXAMPLES_CAP+2}" in e["text"] for e in by_brand["1"])
                assert any(f"br2-{EXAMPLES_CAP+2}" in e["text"] for e in by_brand["2"])
        finally:
            _cleanup(uid, aid, mid)


class TestGetBrandSummary:
    def test_no_module_returns_empty_dict(self):
        from server.db import db_session
        from server.creators_copywriter_bridge import get_brand_summary

        uid, aid, _ = _make_user_agent_with_optional_copywriter(connect_copywriter=False)
        try:
            with db_session() as db:
                assert get_brand_summary(db, uid) == {}
        finally:
            _cleanup(uid, aid, None)

    def test_returns_totals_per_brand(self):
        from server.db import db_session
        from server.creators_copywriter_bridge import (
            save_published_to_copywriter, get_brand_summary,
        )

        uid, aid, mid = _make_user_agent_with_optional_copywriter(connect_copywriter=True)
        try:
            with db_session() as db:
                save_published_to_copywriter(db, uid, "a1", "tg", brand_id=1)
                save_published_to_copywriter(db, uid, "a2", "tg", brand_id=1)
                save_published_to_copywriter(db, uid, "b1", "vk", brand_id=2)

            with db_session() as db2:
                s = get_brand_summary(db2, uid)
                assert s["total"] == 3
                # Сортировка по count desc
                assert s["brands"][0]["brand_id"] == 1
                assert s["brands"][0]["count"] == 2
                assert s["brands"][1]["brand_id"] == 2
                assert s["brands"][1]["count"] == 1
                assert s["has_legacy"] is False
        finally:
            _cleanup(uid, aid, mid)
