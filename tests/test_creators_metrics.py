"""Тесты для server/creators_metrics.py — VK API + TG preview parsing.

VK через wall.getById (mock httpx.AsyncClient.post).
TG через t.me/<channel>/<msg_id> embed-preview HTML парсинг.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestParseTgViewsCount:

    def test_plain_number(self):
        from server.creators_metrics import _parse_tg_views_count
        assert _parse_tg_views_count("123") == 123

    def test_k_suffix(self):
        from server.creators_metrics import _parse_tg_views_count
        assert _parse_tg_views_count("1.2K") == 1200
        assert _parse_tg_views_count("5.3k") == 5300

    def test_m_suffix(self):
        from server.creators_metrics import _parse_tg_views_count
        assert _parse_tg_views_count("2.5M") == 2_500_000

    def test_cyrillic_k_m(self):
        """TG локально показывает К (русская) / М (русская)."""
        from server.creators_metrics import _parse_tg_views_count
        assert _parse_tg_views_count("3К") == 3000
        assert _parse_tg_views_count("1.5М") == 1_500_000

    def test_invalid(self):
        from server.creators_metrics import _parse_tg_views_count
        assert _parse_tg_views_count("") == 0
        assert _parse_tg_views_count("not-a-number") == 0
        assert _parse_tg_views_count(None) == 0


class TestFetchVkPostStats:

    def _run(self, coro):
        return asyncio.run(coro)

    def test_empty_inputs(self):
        from server.creators_metrics import fetch_vk_post_stats
        assert self._run(fetch_vk_post_stats("", 123, 456)) is None
        assert self._run(fetch_vk_post_stats("tok", "", 456)) is None
        assert self._run(fetch_vk_post_stats("tok", 123, None)) is None

    def test_parses_valid_response(self, monkeypatch):
        from server import creators_metrics as cm

        mock_response = MagicMock()
        mock_response.json = MagicMock(return_value={
            "response": [{
                "views":    {"count": 1500},
                "likes":    {"count": 42},
                "comments": {"count": 7},
                "reposts":  {"count": 3},
            }]
        })
        mock_post = AsyncMock(return_value=mock_response)
        mock_client = MagicMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr(cm.httpx, "AsyncClient", lambda **kw: mock_client)

        result = self._run(cm.fetch_vk_post_stats("tok", 12345, 99))
        assert result is not None
        assert result["views"] == 1500
        assert result["likes"] == 42
        assert result["comments"] == 7
        assert result["shares"] == 3

    def test_deleted_post_returns_zeros_with_flag(self, monkeypatch):
        """VK error code 100 (объект удалён) — возвращаем нули с deleted=True."""
        from server import creators_metrics as cm

        mock_response = MagicMock()
        mock_response.json = MagicMock(return_value={
            "error": {"error_code": 100, "error_msg": "Object not found"}
        })
        mock_post = AsyncMock(return_value=mock_response)
        mock_client = MagicMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr(cm.httpx, "AsyncClient", lambda **kw: mock_client)

        result = self._run(cm.fetch_vk_post_stats("tok", 12345, 99))
        assert result is not None
        assert result["views"] == 0
        assert result.get("deleted") is True

    def test_network_error_returns_none(self, monkeypatch):
        from server import creators_metrics as cm

        mock_post = AsyncMock(side_effect=Exception("network down"))
        mock_client = MagicMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr(cm.httpx, "AsyncClient", lambda **kw: mock_client)

        assert self._run(cm.fetch_vk_post_stats("tok", 123, 456)) is None

    def test_owner_id_negated_for_community(self, monkeypatch):
        """Community ID → -owner_id для wall.getById."""
        from server import creators_metrics as cm
        captured = {}

        async def fake_post(url, data=None, **kw):
            captured["data"] = data
            resp = MagicMock()
            resp.json = MagicMock(return_value={"response": [{
                "views": {"count": 0}, "likes": {"count": 0},
                "comments": {"count": 0}, "reposts": {"count": 0},
            }]})
            return resp

        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=fake_post)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr(cm.httpx, "AsyncClient", lambda **kw: mock_client)

        self._run(cm.fetch_vk_post_stats("tok", 555, 99))
        assert captured["data"]["posts"] == "-555_99"


class TestFetchTgPostViews:

    def _run(self, coro):
        return asyncio.run(coro)

    def test_invalid_channel(self):
        from server.creators_metrics import fetch_tg_post_views
        assert self._run(fetch_tg_post_views("", "123")) is None
        assert self._run(fetch_tg_post_views("-1001234567890", "1")) is None
        assert self._run(fetch_tg_post_views("ch", None)) is None

    def test_parses_views_from_html(self, monkeypatch):
        from server import creators_metrics as cm

        # Снимок из реального t.me embed (упрощённый)
        sample_html = """
        <html><body>
        <div class="tgme_widget_message_wrap js-widget_message_wrap">
          <div class="tgme_widget_message_text js-message_text">Текст поста...</div>
          <span class="tgme_widget_message_views">3.7K</span>
        </div>
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
        monkeypatch.setattr(cm.httpx, "AsyncClient", lambda **kw: mock_client)

        result = self._run(cm.fetch_tg_post_views("durov", "42"))
        assert result is not None
        assert result["views"] == 3700
        # TG не даёт лайки/комменты в preview
        assert result["likes"] == 0

    def test_404_returns_none(self, monkeypatch):
        from server import creators_metrics as cm

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = ""
        mock_get = AsyncMock(return_value=mock_response)
        mock_client = MagicMock()
        mock_client.get = mock_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr(cm.httpx, "AsyncClient", lambda **kw: mock_client)

        assert self._run(cm.fetch_tg_post_views("durov", "1")) is None

    def test_no_views_in_html_returns_zeros(self, monkeypatch):
        """Если preview есть но counter views нет — возвращаем нули
        (видимо пост удалён или это не channel-сообщение)."""
        from server import creators_metrics as cm

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "<html><body>nothing here</body></html>"
        mock_get = AsyncMock(return_value=mock_response)
        mock_client = MagicMock()
        mock_client.get = mock_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr(cm.httpx, "AsyncClient", lambda **kw: mock_client)

        result = self._run(cm.fetch_tg_post_views("durov", "1"))
        assert result is not None
        assert result["views"] == 0
