"""Тесты bootstrap-импорта прошлых постов VK/TG → copywriter memory.

Покрывают:
  - fetch_vk_community_posts: парсинг ответа VK API + фильтрация
  - fetch_tg_channel_preview: парсинг t.me/s/{username} HTML
  - bootstrap_copywriter_from_channels: end-to-end через mock'и

HTTP-вызовы заменяются через monkeypatch httpx.AsyncClient.
"""
import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest


# ── fetch_vk_community_posts ────────────────────────────────────────────────


class TestFetchVk:

    def _run(self, coro):
        return asyncio.run(coro)

    def test_empty_token_returns_empty(self):
        from server.creators_bootstrap import fetch_vk_community_posts
        assert self._run(fetch_vk_community_posts("", 123)) == []
        assert self._run(fetch_vk_community_posts("tok", 0)) == []

    def test_parses_normal_response(self, monkeypatch):
        from server import creators_bootstrap as bs

        # Замена AsyncClient через MagicMock контекстный менеджер
        mock_response = MagicMock()
        mock_response.json = MagicMock(return_value={
            "response": {
                "count": 3,
                "items": [
                    {"text": "Первый пост про стройку", "date": 1700000000},
                    {"text": "Короткий", "date": 1700001000},  # < MIN_POST_LENGTH
                    {"text": "Второй длинный пост про дома и фундаменты",
                     "date": 1700002000},
                    {"text": "Промо реклама", "date": 1700003000,
                     "marked_as_ads": 1},  # ads → filter
                ]
            }
        })

        mock_post = AsyncMock(return_value=mock_response)
        mock_client = MagicMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        monkeypatch.setattr(bs.httpx, "AsyncClient", lambda **kw: mock_client)

        posts = self._run(bs.fetch_vk_community_posts("tok", 12345))
        assert len(posts) == 2  # короткий + ads отфильтрованы
        assert all(p["platform"] == "vk" for p in posts)
        assert "стройку" in posts[0]["text"]

    def test_api_error_returns_empty(self, monkeypatch):
        from server import creators_bootstrap as bs

        mock_response = MagicMock()
        mock_response.json = MagicMock(return_value={
            "error": {"error_code": 100, "error_msg": "Invalid group"}
        })
        mock_post = AsyncMock(return_value=mock_response)
        mock_client = MagicMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr(bs.httpx, "AsyncClient", lambda **kw: mock_client)

        assert self._run(bs.fetch_vk_community_posts("tok", 999)) == []

    def test_http_error_returns_empty(self, monkeypatch):
        from server import creators_bootstrap as bs

        mock_post = AsyncMock(side_effect=Exception("network"))
        mock_client = MagicMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr(bs.httpx, "AsyncClient", lambda **kw: mock_client)

        assert self._run(bs.fetch_vk_community_posts("tok", 123)) == []


# ── fetch_tg_channel_preview ────────────────────────────────────────────────


class TestFetchTg:

    def _run(self, coro):
        return asyncio.run(coro)

    def test_invalid_username_returns_empty(self):
        from server.creators_bootstrap import fetch_tg_channel_preview
        # Числовой ID (Bot API формат) — preview не работает
        assert self._run(fetch_tg_channel_preview("-1001234567890")) == []
        # Пустой
        assert self._run(fetch_tg_channel_preview("")) == []
        # Спецсимволы (XSS protection / sanity)
        assert self._run(fetch_tg_channel_preview("aa")) == []  # короче 4
        assert self._run(fetch_tg_channel_preview("привет")) == []  # кириллица

    def test_valid_username_parses_messages(self, monkeypatch):
        from server import creators_bootstrap as bs

        sample_html = """
        <html><body>
        <div class="tgme_widget_message_text js-message_text" dir="auto">
            Первое сообщение с <b>жирным</b> текстом про строительство.
        </div>
        <div class="tgme_widget_message_text js-message_text" dir="auto">
            Второе<br/>с переносами<br/>и <a href="https://example.com">ссылкой</a>
            на ресурс.
        </div>
        <div class="tgme_widget_message_text">Слишком коротк</div>
        </body></html>
        """
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = sample_html

        mock_get = AsyncMock(return_value=mock_response)
        mock_client = MagicMock()
        mock_client.get = mock_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr(bs.httpx, "AsyncClient", lambda **kw: mock_client)

        posts = self._run(bs.fetch_tg_channel_preview("durov_chan"))
        assert len(posts) == 2  # короткое отфильтровано
        assert posts[0]["platform"] == "tg"
        assert "жирным" in posts[0]["text"]  # <b> удалён, текст остался
        assert "<b>" not in posts[0]["text"]
        assert "ссылкой" in posts[1]["text"]
        assert "<a" not in posts[1]["text"]  # <a> вычищен, текст остался

    def test_404_returns_empty(self, monkeypatch):
        from server import creators_bootstrap as bs

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "Not found"
        mock_get = AsyncMock(return_value=mock_response)
        mock_client = MagicMock()
        mock_client.get = mock_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr(bs.httpx, "AsyncClient", lambda **kw: mock_client)

        assert self._run(bs.fetch_tg_channel_preview("nosuchchannel")) == []


# ── bootstrap_copywriter_from_channels (end-to-end) ────────────────────────


def _setup_user_brand_channel(platform: str, token: str | None = "tok",
                              channel_id: str = "123",
                              connect_copywriter: bool = True):
    """Создать User + Agent + (опц.) copywriter + Brand + Channel.
    Возвращает (user_id, brand_id, channel_id_db, module_id|None)."""
    from server.db import db_session
    from server.models import (User, Agent, AgentModule, CreatorBrand,
                               CreatorChannelConnection)
    with db_session() as db:
        u = User(
            email=f"bs-{time.time_ns()}@x.x",
            password_hash="h", is_verified=True,
            agreed_to_terms=True, tokens_balance=0,
        )
        db.add(u); db.commit(); db.refresh(u)
        a = Agent(user_id=u.id, name="Че", status="active",
                  profile_json="{}", personality_json="{}")
        db.add(a); db.commit(); db.refresh(a)
        mid = None
        if connect_copywriter:
            m = AgentModule(agent_id=a.id, slug="copywriter", level=0,
                            is_enabled=True, interaction_count=0,
                            module_memory_json="{}")
            db.add(m); db.commit(); db.refresh(m)
            mid = m.id
        b = CreatorBrand(user_id=u.id, name="Test Brand", niche="stroy",
                         tone="business")
        db.add(b); db.commit(); db.refresh(b)
        c = CreatorChannelConnection(
            brand_id=b.id, platform=platform,
            channel_id=channel_id, title="My Channel",
            token=token, is_active=True,
        )
        db.add(c); db.commit(); db.refresh(c)
        return u.id, b.id, c.id, mid


def _cleanup_all(user_id):
    from server.db import db_session
    from server.models import (User, Agent, AgentModule, CreatorBrand,
                               CreatorChannelConnection)
    with db_session() as db:
        agent = db.query(Agent).filter_by(user_id=user_id).first()
        if agent:
            db.query(AgentModule).filter_by(agent_id=agent.id).delete()
            db.delete(agent)
        for b in db.query(CreatorBrand).filter_by(user_id=user_id).all():
            db.query(CreatorChannelConnection).filter_by(brand_id=b.id).delete()
            db.delete(b)
        db.query(User).filter_by(id=user_id).delete()
        db.commit()


class TestBootstrapEndToEnd:

    def _run(self, coro):
        return asyncio.run(coro)

    def test_no_brands_returns_error(self, monkeypatch):
        """Юзер без брендов — friendly error message."""
        from server.db import db_session
        from server.models import User, Agent
        from server.creators_bootstrap import bootstrap_copywriter_from_channels

        with db_session() as db:
            u = User(email=f"nobrand-{time.time_ns()}@x.x",
                    password_hash="h", is_verified=True,
                    agreed_to_terms=True, tokens_balance=0)
            db.add(u); db.commit(); db.refresh(u)
            a = Agent(user_id=u.id, name="Че", status="active",
                      profile_json="{}", personality_json="{}")
            db.add(a); db.commit(); db.refresh(a)
            uid = u.id

        try:
            with db_session() as db:
                result = self._run(bootstrap_copywriter_from_channels(db, uid))
            assert result["imported"] == 0
            assert "бренд" in result["errors"][0].lower()
        finally:
            _cleanup_all(uid)

    def test_vk_imports_posts_to_brand_bucket(self, monkeypatch):
        """VK канал → посты сохраняются в examples_by_brand[brand_id]."""
        from server import creators_bootstrap as bs
        from server.db import db_session
        from server.models import AgentModule

        uid, bid, _, mid = _setup_user_brand_channel("vk", channel_id="555")

        try:
            # Mock VK API
            async def fake_fetch_vk(token, group_id, limit=50):
                return [
                    {"text": "Длинный пост про фундаменты и материалы для стройки",
                     "platform": "vk", "date": 1700000000},
                    {"text": "Второй пост — расценки на работу бригады каменщиков",
                     "platform": "vk", "date": 1700001000},
                ]
            monkeypatch.setattr(bs, "fetch_vk_community_posts", fake_fetch_vk)

            with db_session() as db:
                result = self._run(bs.bootstrap_copywriter_from_channels(db, uid))

            assert result["imported"] == 2
            assert result["per_brand"][0]["imported"] == 2

            # Проверим что посты лежат в examples_by_brand[bid]
            with db_session() as db:
                m = db.query(AgentModule).filter_by(id=mid).first()
                memory = json.loads(m.module_memory_json or "{}")
                bucket = memory["examples_by_brand"][str(bid)]
                assert len(bucket) == 2
                assert any("фундаменты" in e["text"] for e in bucket)
        finally:
            _cleanup_all(uid)

    def test_tg_imports_via_preview(self, monkeypatch):
        """TG public канал → посты через t.me/s/."""
        from server import creators_bootstrap as bs
        from server.db import db_session
        from server.models import AgentModule

        uid, bid, _, mid = _setup_user_brand_channel(
            "tg", token=None, channel_id="durov"
        )

        try:
            async def fake_fetch_tg(username, limit=30):
                return [
                    {"text": "Telegram пост про новые фичи мессенджера",
                     "platform": "tg", "date": None},
                ]
            monkeypatch.setattr(bs, "fetch_tg_channel_preview", fake_fetch_tg)

            with db_session() as db:
                result = self._run(bs.bootstrap_copywriter_from_channels(db, uid))
            assert result["imported"] == 1
            assert result["per_brand"][0]["channels"][0]["platform"] == "tg"

            with db_session() as db:
                m = db.query(AgentModule).filter_by(id=mid).first()
                memory = json.loads(m.module_memory_json or "{}")
                assert "Telegram пост" in str(memory["examples_by_brand"][str(bid)])
        finally:
            _cleanup_all(uid)

    def test_no_copywriter_module_imports_nothing(self, monkeypatch):
        """Если copywriter не подключён — save no-op'ит, total=0."""
        from server import creators_bootstrap as bs
        from server.db import db_session

        uid, bid, _, _ = _setup_user_brand_channel(
            "vk", channel_id="555", connect_copywriter=False
        )

        try:
            async def fake_fetch_vk(token, group_id, limit=50):
                return [{"text": "Длинный пост " * 5, "platform": "vk",
                         "date": 1700000000}]
            monkeypatch.setattr(bs, "fetch_vk_community_posts", fake_fetch_vk)

            with db_session() as db:
                result = self._run(bs.bootstrap_copywriter_from_channels(db, uid))
            assert result["imported"] == 0  # save_published_to_copywriter возвращает False
        finally:
            _cleanup_all(uid)

    def test_skipped_brands_counted(self, monkeypatch):
        """Бренд без активных каналов → skipped_brands += 1."""
        from server.db import db_session
        from server.models import (User, Agent, AgentModule, CreatorBrand)
        from server.creators_bootstrap import bootstrap_copywriter_from_channels

        with db_session() as db:
            u = User(email=f"skip-{time.time_ns()}@x.x",
                    password_hash="h", is_verified=True,
                    agreed_to_terms=True, tokens_balance=0)
            db.add(u); db.commit(); db.refresh(u)
            a = Agent(user_id=u.id, name="Че", status="active",
                      profile_json="{}", personality_json="{}")
            db.add(a); db.commit(); db.refresh(a)
            m = AgentModule(agent_id=a.id, slug="copywriter", level=0,
                            is_enabled=True, module_memory_json="{}",
                            interaction_count=0)
            db.add(m); db.commit()
            b = CreatorBrand(user_id=u.id, name="No-channels brand",
                             niche="x", tone="business")
            db.add(b); db.commit()
            uid = u.id

        try:
            with db_session() as db:
                result = self._run(bootstrap_copywriter_from_channels(db, uid))
            assert result["imported"] == 0
            assert result["skipped_brands"] == 1
        finally:
            _cleanup_all(uid)
