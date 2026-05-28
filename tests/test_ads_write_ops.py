"""Тесты write-операций Я.Директ + VK Ads.

Все сетевые вызовы мокаются — реальной авторизации/трафика нет.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Yandex Direct ────────────────────────────────────────────────────────────


class TestYandexDirectPause:

    def test_pause_success(self, monkeypatch):
        from server import yandex_direct as yd
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json = MagicMock(return_value={
            "result": {"SuspendResults": [{"Id": 12345678}]}
        })
        mock_client = MagicMock()
        mock_client.post = MagicMock(return_value=mock_resp)
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=None)
        monkeypatch.setattr(yd.httpx, "Client", lambda **kw: mock_client)

        r = yd.pause_campaign("token-ok", 12345678)
        assert r["ok"] is True

    def test_pause_with_errors_in_response(self, monkeypatch):
        from server import yandex_direct as yd
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json = MagicMock(return_value={
            "result": {"SuspendResults": [{
                "Errors": [{"Message": "Campaign not found"}],
            }]}
        })
        mock_client = MagicMock()
        mock_client.post = MagicMock(return_value=mock_resp)
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=None)
        monkeypatch.setattr(yd.httpx, "Client", lambda **kw: mock_client)

        r = yd.pause_campaign("token-ok", 99999)
        assert r["ok"] is False
        assert "Campaign not found" in (r["error"] or "")

    def test_pause_401_unauthorized(self, monkeypatch):
        from server import yandex_direct as yd
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.json = MagicMock(return_value={
            "error": {"error_string": "Unauthorized"}
        })
        mock_client = MagicMock()
        mock_client.post = MagicMock(return_value=mock_resp)
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=None)
        monkeypatch.setattr(yd.httpx, "Client", lambda **kw: mock_client)

        r = yd.pause_campaign("bad-token", 123)
        assert r["ok"] is False
        # _post возвращает error c error_string при 401
        assert "авторизован" in (r["error"] or "").lower()


class TestYandexDirectSetDailyBudget:

    def test_set_budget_success(self, monkeypatch):
        from server import yandex_direct as yd
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json = MagicMock(return_value={
            "result": {"UpdateResults": [{"Id": 12345678}]}
        })
        mock_client = MagicMock()
        mock_client.post = MagicMock(return_value=mock_resp)
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=None)
        monkeypatch.setattr(yd.httpx, "Client", lambda **kw: mock_client)

        r = yd.set_daily_budget("token", 12345678, 1500.0)
        assert r["ok"] is True

        # Проверим что отправили amount в micros (1500 RUB = 1.5B micros)
        sent_body = mock_client.post.call_args.kwargs.get("content")
        assert b"1500000000" in sent_body  # 1500 * 1_000_000

    def test_zero_budget_rejected(self):
        from server.yandex_direct import set_daily_budget
        r = set_daily_budget("token", 1, 0)
        assert r["ok"] is False

    def test_negative_budget_rejected(self):
        from server.yandex_direct import set_daily_budget
        r = set_daily_budget("token", 1, -100)
        assert r["ok"] is False


# ── VK Ads ──────────────────────────────────────────────────────────────────


class TestVkAdsUpdateStatus:

    def test_pause_success(self, monkeypatch):
        from server import vk_ads as va
        mock_resp = MagicMock()
        mock_resp.content = b"{}"
        mock_resp.json = MagicMock(return_value={"response": [0]})
        mock_client = MagicMock()
        mock_client.post = MagicMock(return_value=mock_resp)
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=None)
        monkeypatch.setattr(va.httpx, "Client", lambda **kw: mock_client)

        r = va.update_campaign_status("ads-token", 12345, 67890, status=0)
        assert r["ok"] is True

    def test_invalid_status_rejected(self):
        from server.vk_ads import update_campaign_status
        r = update_campaign_status("token", 1, 2, status=5)
        assert r["ok"] is False
        assert "status" in (r["error"] or "").lower()

    def test_set_day_limit_success(self, monkeypatch):
        from server import vk_ads as va
        mock_resp = MagicMock()
        mock_resp.content = b"{}"
        mock_resp.json = MagicMock(return_value={"response": [0]})
        mock_client = MagicMock()
        mock_client.post = MagicMock(return_value=mock_resp)
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=None)
        monkeypatch.setattr(va.httpx, "Client", lambda **kw: mock_client)

        r = va.set_campaign_day_limit("ads-token", 1, 2, day_limit_rub=500)
        assert r["ok"] is True
        # day_limit передаётся в копейках = 500 * 100 = 50000
        sent_payload = mock_client.post.call_args.kwargs.get("data") or {}
        # data передаётся как строка через _call
        import json as _json
        data_str = sent_payload.get("data")
        if data_str:
            parsed = _json.loads(data_str)
            assert parsed[0]["day_limit"] == 50000


# ── Executor с проверкой токена в settings ──────────────────────────────────


class TestExecutorRequiresToken:

    def test_yandex_pause_without_token_friendly_error(self):
        """Если в custom_settings нет oauth_token — executor вернёт понятную ошибку."""
        from server.agent_actions import execute_action
        r = execute_action("yandex_direct_pause_campaign",
                           {"campaign_id": 1}, user_id=999_999_999)
        assert r["ok"] is False
        assert "OAuth-токен" in (r.get("error") or "") or \
            "Я.Директ" in (r.get("error") or "")

    def test_vk_ads_pause_without_token_friendly_error(self):
        from server.agent_actions import execute_action
        r = execute_action("vk_ads_pause_campaign",
                           {"campaign_id": 1}, user_id=999_999_999)
        assert r["ok"] is False
        assert "VK Ads" in (r.get("error") or "") or "ads_token" in (r.get("error") or "")

    def test_yandex_huge_budget_rejected_safety(self):
        from server.agent_actions import execute_action
        # 5 млн ₽/день — явная ошибка LLM, отказываем
        r = execute_action("yandex_direct_set_daily_budget",
                           {"campaign_id": 123, "new_daily_budget_rub": 5_000_000},
                           user_id=999)
        assert r["ok"] is False
        assert "чрезмерным" in (r.get("error") or "")
