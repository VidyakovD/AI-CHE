"""Тесты server/calendar_sync.py — ICS parsing + Google API + Yandex CalDAV.

Все внешние HTTP-запросы мокаются (httpx.AsyncClient).
"""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest


SAMPLE_ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:abc123@google.com
SUMMARY:Встреча с Иваном
DTSTART:20260530T140000Z
DTEND:20260530T150000Z
DESCRIPTION:Обсуждение проекта Х
LOCATION:Офис на Тверской
END:VEVENT
BEGIN:VEVENT
UID:def456@google.com
SUMMARY:Daily standup
DTSTART:20260531T090000Z
DTEND:20260531T091500Z
END:VEVENT
END:VCALENDAR
"""


class TestParseIcsEvents:

    def test_parses_two_events(self):
        from server.calendar_sync import parse_ics_events
        events = parse_ics_events(SAMPLE_ICS)
        assert len(events) == 2
        assert events[0]["title"] == "Встреча с Иваном"
        assert events[0]["location"] == "Офис на Тверской"
        assert events[0]["start"].year == 2026
        assert events[0]["start"].hour == 14
        assert events[1]["title"] == "Daily standup"

    def test_empty_returns_empty(self):
        from server.calendar_sync import parse_ics_events
        assert parse_ics_events("") == []
        assert parse_ics_events(None) == []

    def test_from_dt_filter(self):
        """from_dt отфильтровывает события раньше указанной даты."""
        from server.calendar_sync import parse_ics_events
        # Фильтр на день позже первого события — должно остаться только второе
        cutoff = datetime(2026, 5, 31, 0, 0, 0)
        events = parse_ics_events(SAMPLE_ICS, from_dt=cutoff)
        assert len(events) == 1
        assert events[0]["title"] == "Daily standup"

    def test_handles_line_folding(self):
        """RFC 5545 line-folding: продолжение строки начинается с space/tab."""
        from server.calendar_sync import parse_ics_events
        ics = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
               "SUMMARY:Очень длинное название встр\n"
               " ечи (с продолжением)\n"
               "DTSTART:20260530T140000Z\n"
               "END:VEVENT\nEND:VCALENDAR\n")
        events = parse_ics_events(ics)
        assert len(events) == 1
        assert "продолжением" in events[0]["title"]

    def test_handles_date_only(self):
        """All-day event: DTSTART:20260530 (без времени)."""
        from server.calendar_sync import parse_ics_events
        ics = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
               "SUMMARY:День рождения\nDTSTART:20260615\n"
               "END:VEVENT\nEND:VCALENDAR\n")
        events = parse_ics_events(ics)
        assert len(events) == 1
        assert events[0]["start"].date() == datetime(2026, 6, 15).date()

    def test_sorts_by_start(self):
        from server.calendar_sync import parse_ics_events
        # Сначала late, потом early — должны выйти в порядке start
        ics = ("BEGIN:VCALENDAR\n"
               "BEGIN:VEVENT\nSUMMARY:Late\nDTSTART:20260601T120000Z\nEND:VEVENT\n"
               "BEGIN:VEVENT\nSUMMARY:Early\nDTSTART:20260530T120000Z\nEND:VEVENT\n"
               "END:VCALENDAR\n")
        events = parse_ics_events(ics)
        assert events[0]["title"] == "Early"
        assert events[1]["title"] == "Late"


class TestFormatEventsForLlm:

    def test_no_events_friendly_message(self):
        from server.calendar_sync import format_events_for_llm
        result = format_events_for_llm([])
        assert "нет" in result.lower()
        assert "📅" in result

    def test_renders_event_with_source(self):
        from server.calendar_sync import format_events_for_llm
        events = [{
            "title": "Встреча",
            "start": datetime(2026, 5, 30, 14, 0),
            "end": datetime(2026, 5, 30, 15, 0),
            "location": "Офис",
            "source": "google",
        }]
        result = format_events_for_llm(events)
        assert "Встреча" in result
        assert "🌐" in result  # Google source icon
        assert "📍 Офис" in result


class TestGoogleRefreshToken:

    def _run(self, coro):
        return asyncio.run(coro)

    def test_missing_env_returns_none(self, monkeypatch):
        from server.calendar_sync import google_refresh_access_token
        monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
        monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
        assert self._run(google_refresh_access_token("rt")) is None

    def test_successful_refresh(self, monkeypatch):
        from server import calendar_sync as cs
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-secret")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value={
            "access_token": "new_at_xyz",
            "expires_in": 3600,
        })
        mock_post = AsyncMock(return_value=mock_response)
        mock_client = MagicMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr(cs.httpx, "AsyncClient", lambda **kw: mock_client)

        result = self._run(cs.google_refresh_access_token("rt"))
        assert result is not None
        assert result["access_token"] == "new_at_xyz"

    def test_401_returns_none(self, monkeypatch):
        from server import calendar_sync as cs
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-secret")
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "invalid_grant"
        mock_post = AsyncMock(return_value=mock_response)
        mock_client = MagicMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr(cs.httpx, "AsyncClient", lambda **kw: mock_client)
        assert self._run(cs.google_refresh_access_token("bad-rt")) is None


class TestFetchGoogleEvents:

    def _run(self, coro):
        return asyncio.run(coro)

    def test_parses_events(self, monkeypatch):
        from server import calendar_sync as cs
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value={
            "items": [{
                "id": "ev1",
                "summary": "Sprint review",
                "start": {"dateTime": "2026-05-30T14:00:00Z"},
                "end":   {"dateTime": "2026-05-30T15:00:00Z"},
                "description": "Demo of new feature",
                "location": "Zoom",
            }]
        })
        mock_get = AsyncMock(return_value=mock_response)
        mock_client = MagicMock()
        mock_client.get = mock_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr(cs.httpx, "AsyncClient", lambda **kw: mock_client)

        events = self._run(cs.fetch_google_events("at123", "primary"))
        assert len(events) == 1
        assert events[0]["title"] == "Sprint review"
        assert events[0]["start"].hour == 14
        assert events[0]["location"] == "Zoom"

    def test_401_raises_permission_error(self, monkeypatch):
        """401 → PermissionError для refresh+retry flow."""
        from server import calendar_sync as cs
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Token expired"
        mock_get = AsyncMock(return_value=mock_response)
        mock_client = MagicMock()
        mock_client.get = mock_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr(cs.httpx, "AsyncClient", lambda **kw: mock_client)

        with pytest.raises(PermissionError):
            self._run(cs.fetch_google_events("expired_at", "primary"))


class TestCreateGoogleEvent:

    def _run(self, coro):
        return asyncio.run(coro)

    def test_no_access_token(self):
        from server.calendar_sync import create_google_event
        r = self._run(create_google_event(
            access_token="", summary="X", start="2026-06-05T14:00:00+03:00",
        ))
        assert r["ok"] is False
        assert "access_token" in (r.get("error") or "").lower()

    def test_no_summary(self):
        from server.calendar_sync import create_google_event
        r = self._run(create_google_event(
            access_token="at", summary="   ",
            start="2026-06-05T14:00:00+03:00",
        ))
        assert r["ok"] is False
        assert "summary" in (r.get("error") or "").lower() or \
            "название" in (r.get("error") or "").lower()

    def test_successful_create(self, monkeypatch):
        from server import calendar_sync as cs
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value={
            "id": "abc123event",
            "htmlLink": "https://calendar.google.com/event?eid=...",
        })
        mock_post = AsyncMock(return_value=mock_response)
        mock_client = MagicMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr(cs.httpx, "AsyncClient", lambda **kw: mock_client)

        r = self._run(cs.create_google_event(
            access_token="at", summary="Встреча с Иваном",
            start="2026-06-05T14:00:00+03:00",
            end="2026-06-05T15:00:00+03:00",
            location="Zoom", description="Обсудим план",
        ))
        assert r["ok"] is True
        assert r["event_id"] == "abc123event"
        assert "calendar.google.com" in r["html_link"]

        # Проверяем что отправили правильный payload
        called_payload = mock_post.call_args.kwargs.get("json") or {}
        assert called_payload["summary"] == "Встреча с Иваном"
        assert called_payload["start"]["dateTime"] == "2026-06-05T14:00:00+03:00"
        assert called_payload["end"]["dateTime"] == "2026-06-05T15:00:00+03:00"
        assert called_payload["location"] == "Zoom"

    def test_all_day_event_uses_date_field(self, monkeypatch):
        from server import calendar_sync as cs
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value={"id": "all-day-1"})
        mock_post = AsyncMock(return_value=mock_response)
        mock_client = MagicMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr(cs.httpx, "AsyncClient", lambda **kw: mock_client)

        r = self._run(cs.create_google_event(
            access_token="at", summary="День рождения",
            start="2026-06-05",  # date-only → all-day
        ))
        assert r["ok"] is True
        payload = mock_post.call_args.kwargs.get("json") or {}
        assert "date" in payload["start"]
        assert payload["start"]["date"] == "2026-06-05"
        assert "dateTime" not in payload["start"]

    def test_403_readonly_scope_gives_friendly_error(self, monkeypatch):
        """Старые connections с .readonly scope → 403 при попытке write."""
        from server import calendar_sync as cs
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = "Insufficient permissions"
        mock_post = AsyncMock(return_value=mock_response)
        mock_client = MagicMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr(cs.httpx, "AsyncClient", lambda **kw: mock_client)

        r = self._run(cs.create_google_event(
            access_token="readonly_token", summary="X",
            start="2026-06-05T14:00:00+03:00",
        ))
        assert r["ok"] is False
        assert "переподключи" in (r.get("error") or "").lower()

    def test_401_raises_permission_error(self, monkeypatch):
        from server import calendar_sync as cs
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Token expired"
        mock_post = AsyncMock(return_value=mock_response)
        mock_client = MagicMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr(cs.httpx, "AsyncClient", lambda **kw: mock_client)

        with pytest.raises(PermissionError):
            self._run(cs.create_google_event(
                access_token="expired", summary="X",
                start="2026-06-05T14:00:00+03:00",
            ))


class TestYandexCalDavCheck:

    def _run(self, coro):
        return asyncio.run(coro)

    def test_invalid_email(self):
        from server.calendar_sync import yandex_caldav_check_creds
        result = self._run(yandex_caldav_check_creds("not-an-email", "pass"))
        assert result["ok"] is False
        assert "@" in result["error"]

    def test_empty_inputs(self):
        from server.calendar_sync import yandex_caldav_check_creds
        result = self._run(yandex_caldav_check_creds("", "pass"))
        assert result["ok"] is False

    def test_401_returns_friendly_error(self, monkeypatch):
        """401 от Yandex → подсказка про app-password (не основной пароль)."""
        from server import calendar_sync as cs
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_request = AsyncMock(return_value=mock_response)
        mock_client = MagicMock()
        mock_client.request = mock_request
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr(cs.httpx, "AsyncClient", lambda **kw: mock_client)

        result = self._run(cs.yandex_caldav_check_creds("denis@yandex.ru", "wrong"))
        assert result["ok"] is False
        assert "app-password" in result["error"].lower()

    def test_207_multistatus_ok(self, monkeypatch):
        """Yandex CalDAV возвращает 207 Multi-Status на PROPFIND — success."""
        from server import calendar_sync as cs
        mock_response = MagicMock()
        mock_response.status_code = 207
        mock_response.text = "<?xml ...?><D:multistatus>...</D:multistatus>"
        mock_request = AsyncMock(return_value=mock_response)
        mock_client = MagicMock()
        mock_client.request = mock_request
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr(cs.httpx, "AsyncClient", lambda **kw: mock_client)

        result = self._run(cs.yandex_caldav_check_creds("denis@yandex.ru", "app-pwd"))
        assert result["ok"] is True


class TestFetchYandexEvents:

    def _run(self, coro):
        return asyncio.run(coro)

    def test_parses_multistatus_with_ics(self, monkeypatch):
        from server import calendar_sync as cs
        # Yandex отвечает Multi-Status с calendar-data блоками.
        # Дату ставим заведомо в будущем — иначе тест сломается со временем
        # (parse_ics_events фильтрует past события по from_dt=now).
        multistatus = """<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:response>
    <D:propstat>
      <D:prop>
        <C:calendar-data>BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:y-1
SUMMARY:Yandex встреча
DTSTART:20300101T140000Z
END:VEVENT
END:VCALENDAR</C:calendar-data>
      </D:prop>
    </D:propstat>
  </D:response>
</D:multistatus>"""
        mock_response = MagicMock()
        mock_response.status_code = 207
        mock_response.text = multistatus
        mock_request = AsyncMock(return_value=mock_response)
        mock_client = MagicMock()
        mock_client.request = mock_request
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr(cs.httpx, "AsyncClient", lambda **kw: mock_client)

        events = self._run(cs.fetch_yandex_events("d@yandex.ru", "pwd"))
        assert len(events) == 1
        assert events[0]["title"] == "Yandex встреча"
