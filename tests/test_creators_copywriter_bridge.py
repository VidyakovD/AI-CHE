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
        """Хранится максимум EXAMPLES_CAP (10) — самые свежие."""
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
                examples = memory.get("examples") or []
                assert len(examples) == EXAMPLES_CAP
                # Проверим что остались самые свежие — последний по индексу есть
                texts = [e["text"] for e in examples]
                assert f"пост {EXAMPLES_CAP + 4}" in texts
                # А самый старый отброшен
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
