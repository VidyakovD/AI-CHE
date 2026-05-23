"""Fetch календарных событий из Google Calendar / Yandex CalDAV / ICS URL.

Без зависимостей google-api-client/caldav — всё через httpx (HTTP+XML).
Это даёт light-weight подключение к Calendar-модулю Loom без раздувания
requirements.txt.

Google Calendar API v3:
  GET https://www.googleapis.com/calendar/v3/calendars/{cal_id}/events
  Headers: Authorization: Bearer {access_token}
  Если access_token истёк — refresh через token endpoint и retry.

Yandex CalDAV (RFC 4791):
  REPORT https://caldav.yandex.ru/calendars/{user}/{cal}/  body=calendar-query
  Auth: Basic email:app-password
  Возвращает ICS multi-status XML.

ICS public URL:
  GET <url>
  Возвращает text/calendar.

Парсинг ICS — упрощённый regex-парсер только VEVENT блоков с SUMMARY,
DTSTART, DTEND, DESCRIPTION, LOCATION. Полный RFC 5545 не нужен — нам
только базовый readout для LLM-контекста.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

log = logging.getLogger("calendar-sync")


# ── ICS parsing (минимальный, для нашего use-case readonly readout) ──────────

# Извлекаем blocks BEGIN:VEVENT...END:VEVENT
_VEVENT_RE = re.compile(r"BEGIN:VEVENT(.*?)END:VEVENT", re.DOTALL | re.IGNORECASE)
# Поля внутри VEVENT: SUMMARY, DTSTART, DTEND, DESCRIPTION, LOCATION, UID.
# Формат: KEY[;params]:value. Multi-line continuation через \r\n + space.
_FIELD_RE = re.compile(r"^([A-Z][A-Z0-9-]*)(;[^:]*)?:(.*)$", re.MULTILINE)


def _unfold_ics(text: str) -> str:
    """ICS использует line-folding: продолжение строки начинается с пробела/таба.
    Восстанавливаем оригинальные строки перед парсингом."""
    return re.sub(r"\r?\n[ \t]", "", text)


def _parse_ics_datetime(raw: str) -> Optional[datetime]:
    """Парсим ICS-дату формата:
        20260530T140000Z         — UTC
        20260530T140000          — local floating
        20260530                 — date-only (all-day)
        TZID=...:20260530T140000 — local TZ (нам ок — храним как наивное)
    Возвращаем naive datetime UTC.
    """
    if not raw:
        return None
    raw = raw.strip().rstrip("Z")  # Z = UTC suffix
    fmts = ["%Y%m%dT%H%M%S", "%Y%m%d"]
    for fmt in fmts:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def parse_ics_events(ics_text: str, limit: int = 50,
                      from_dt: Optional[datetime] = None) -> list[dict]:
    """Достать VEVENT'ы из ICS-текста.

    from_dt: фильтр — события начинающиеся не раньше этой даты (UTC).
             Без фильтра — все события.
    limit:   макс. количество (свежие первые).

    Возвращает список dict с полями: title, start, end, description,
    location, source (заполняется caller'ом).
    """
    if not ics_text:
        return []
    text = _unfold_ics(ics_text)
    events: list[dict] = []
    for m in _VEVENT_RE.finditer(text):
        block = m.group(1)
        fields: dict[str, str] = {}
        for field_m in _FIELD_RE.finditer(block):
            key = field_m.group(1).upper()
            # Если ключ DTSTART;TZID=Europe/Moscow — нам только префикс
            base_key = key.split(";")[0]
            if base_key in ("DTSTART", "DTEND", "SUMMARY", "DESCRIPTION",
                             "LOCATION", "UID"):
                # Берём первое значение (без перезаписи если повторяется)
                if base_key not in fields:
                    fields[base_key] = field_m.group(3).strip()
        start_dt = _parse_ics_datetime(fields.get("DTSTART", ""))
        if not start_dt:
            continue
        if from_dt and start_dt < from_dt:
            continue
        end_dt = _parse_ics_datetime(fields.get("DTEND", ""))
        events.append({
            "title":       fields.get("SUMMARY", "").replace("\\,", ",")[:200],
            "start":       start_dt,
            "end":         end_dt,
            "description": fields.get("DESCRIPTION", "").replace("\\n", "\n")[:500],
            "location":    fields.get("LOCATION", "").replace("\\,", ",")[:200],
            "uid":         fields.get("UID", "")[:200],
        })
    events.sort(key=lambda e: e["start"])
    return events[:limit]


# ── Google Calendar API v3 ───────────────────────────────────────────────────


GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CAL_API_BASE = "https://www.googleapis.com/calendar/v3"


async def google_refresh_access_token(refresh_token: str) -> Optional[dict]:
    """Получить новый access_token через refresh_token.

    Возвращает {"access_token", "expires_in"} или None.
    """
    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    if not (client_id and client_secret and refresh_token):
        log.warning("[google-cal] refresh_token: missing client_id/secret/token")
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(GOOGLE_TOKEN_URL, data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            })
    except Exception as e:
        log.warning(f"[google-cal] refresh net error: {type(e).__name__}")
        return None
    if r.status_code != 200:
        log.warning(f"[google-cal] refresh failed: {r.status_code} {r.text[:200]}")
        return None
    try:
        return r.json()
    except Exception:
        return None


async def fetch_google_events(access_token: str, calendar_id: str = "primary",
                              days_ahead: int = 14, max_results: int = 50) -> list[dict]:
    """Получить события из Google Calendar.

    Если access_token истёк (401) — caller должен сначала вызвать
    google_refresh_access_token() и retry.

    Возвращает [{title, start, end, description, location, uid}, ...].
    """
    if not access_token:
        return []
    now = datetime.utcnow()
    horizon = now + timedelta(days=days_ahead)
    params = {
        "timeMin": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timeMax": horizon.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "maxResults": str(max_results),
        "singleEvents": "true",
        "orderBy": "startTime",
    }
    url = f"{GOOGLE_CAL_API_BASE}/calendars/{calendar_id}/events"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, params=params, headers=headers)
    except Exception as e:
        log.warning(f"[google-cal] fetch net error: {type(e).__name__}")
        return []
    if r.status_code == 401:
        # Token expired — caller refresh and retry
        raise PermissionError("google_token_expired")
    if r.status_code != 200:
        log.warning(f"[google-cal] fetch {r.status_code}: {r.text[:200]}")
        return []
    try:
        data = r.json()
    except Exception:
        return []
    items = data.get("items") or []
    out: list[dict] = []
    for ev in items:
        start_info = ev.get("start") or {}
        end_info = ev.get("end") or {}
        # Google возвращает либо dateTime (ISO с TZ), либо date (YYYY-MM-DD)
        start_str = start_info.get("dateTime") or start_info.get("date") or ""
        end_str = end_info.get("dateTime") or end_info.get("date") or ""
        start_dt = _parse_iso_to_naive_utc(start_str)
        end_dt = _parse_iso_to_naive_utc(end_str)
        if not start_dt:
            continue
        out.append({
            "title":       (ev.get("summary") or "")[:200],
            "start":       start_dt,
            "end":         end_dt,
            "description": (ev.get("description") or "")[:500],
            "location":    (ev.get("location") or "")[:200],
            "uid":         ev.get("id") or "",
        })
    return out


def _parse_iso_to_naive_utc(s: str) -> Optional[datetime]:
    """ISO-8601 → naive UTC datetime."""
    if not s:
        return None
    try:
        # date-only
        if len(s) == 10:
            return datetime.strptime(s, "%Y-%m-%d")
        # datetime с TZ или без
        s2 = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except ValueError:
        return None


# ── Yandex CalDAV ────────────────────────────────────────────────────────────


YANDEX_CALDAV_BASE = "https://caldav.yandex.ru"


async def fetch_yandex_events(email: str, app_password: str,
                              days_ahead: int = 14,
                              calendar_name: str = "events-default") -> list[dict]:
    """Yandex CalDAV REPORT calendar-query.

    Используем REPORT (метод HTTP) с XML-body, который запрашивает события
    в указанном диапазоне дат. Yandex возвращает multi-status XML с ICS-
    блоками внутри. Парсим ICS из этих блоков.

    URL: https://caldav.yandex.ru/calendars/<email>/<cal_name>/
    Auth: Basic <base64(email:app_password)>
    """
    if not email or not app_password:
        return []
    url = f"{YANDEX_CALDAV_BASE}/calendars/{email}/{calendar_name}/"
    now = datetime.utcnow()
    horizon = now + timedelta(days=days_ahead)
    body = f"""<?xml version="1.0" encoding="utf-8" ?>
<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop><C:calendar-data/></D:prop>
  <C:filter>
    <C:comp-filter name="VCALENDAR">
      <C:comp-filter name="VEVENT">
        <C:time-range start="{now.strftime('%Y%m%dT%H%M%SZ')}"
                       end="{horizon.strftime('%Y%m%dT%H%M%SZ')}"/>
      </C:comp-filter>
    </C:comp-filter>
  </C:filter>
</C:calendar-query>"""
    headers = {
        "Content-Type": "application/xml; charset=utf-8",
        "Depth": "1",
    }
    try:
        async with httpx.AsyncClient(timeout=20,
                                      auth=(email, app_password)) as client:
            r = await client.request("REPORT", url, headers=headers, content=body)
    except Exception as e:
        log.warning(f"[yandex-cal] fetch net error: {type(e).__name__}")
        return []
    if r.status_code == 401:
        raise PermissionError("yandex_caldav_auth_failed")
    if r.status_code not in (207, 200):
        log.warning(f"[yandex-cal] fetch {r.status_code}: {r.text[:200]}")
        return []
    # Multi-status XML с ICS внутри <C:calendar-data>...</C:calendar-data>
    # Namespace prefix может быть любой (C:, cal:, без prefix). Regex
    # допускает короткий prefix [\w]*: или его отсутствие.
    ics_blocks = re.findall(
        r"<(?:\w+:)?calendar-data[^>]*>(.*?)</(?:\w+:)?calendar-data>",
        r.text, re.DOTALL | re.IGNORECASE
    )
    all_events: list[dict] = []
    for ics in ics_blocks:
        # ICS может быть с XML-entities (&lt; &gt; &amp;)
        from html import unescape
        ics = unescape(ics)
        events = parse_ics_events(ics, limit=100, from_dt=now)
        all_events.extend(events)
    all_events.sort(key=lambda e: e["start"])
    return all_events[:50]


async def yandex_caldav_check_creds(email: str, app_password: str,
                                     calendar_name: str = "events-default") -> dict:
    """Проверить что email+app-password работают для CalDAV (PROPFIND on root).

    Возвращает {"ok": bool, "error": str?}.
    """
    if not email or not app_password:
        return {"ok": False, "error": "Пустой email или app-password"}
    if "@" not in email:
        return {"ok": False, "error": "Email должен содержать @"}
    url = f"{YANDEX_CALDAV_BASE}/calendars/{email}/"
    try:
        async with httpx.AsyncClient(timeout=15,
                                      auth=(email, app_password)) as client:
            r = await client.request("PROPFIND", url,
                                      headers={"Depth": "0"})
    except Exception as e:
        return {"ok": False, "error": f"Сеть: {type(e).__name__}"}
    if r.status_code == 401:
        return {"ok": False,
                "error": "Yandex отклонил логин. Нужен app-password "
                          "(id.yandex.ru/security/app-passwords), не основной пароль."}
    if r.status_code not in (207, 200):
        return {"ok": False, "error": f"Yandex CalDAV вернул {r.status_code}"}
    return {"ok": True}


# ── ICS public URL ───────────────────────────────────────────────────────────


async def fetch_ics_events(ics_url: str, days_ahead: int = 14) -> list[dict]:
    """GET ICS-календаря по публичному URL.

    SSRF-защита: ics_url приходит от юзера через UI («подключи Apple iCloud /
    Google public link»). Без валидации юзер мог бы вписать
    http://127.0.0.1:6379/ (Redis), http://169.254.169.254/ (AWS metadata),
    http://192.168.x.x/ (внутрисетевые) — мы бы тянули и отдавали content
    или error с раскрытием реакции внутреннего сервиса. validate_external_url
    блокирует private/loopback/link-local/multicast.
    """
    if not ics_url:
        return []
    try:
        from server.security import validate_external_url
        validate_external_url(ics_url)
    except Exception as e:
        log.warning(f"[ics] url rejected by SSRF guard: {type(e).__name__}: {e}")
        return []
    try:
        # follow_redirects=False — 30x могут увести на private-IP даже если
        # исходный URL был публичным. Лучше отказать чем выпустить SSRF
        # через open-redirect.
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            r = await client.get(ics_url, headers={
                "User-Agent": "AIche-Calendar/1.0",
            })
    except Exception as e:
        log.warning(f"[ics] fetch net error: {type(e).__name__}")
        return []
    if r.status_code != 200:
        log.warning(f"[ics] fetch {r.status_code}")
        return []
    # Ограничиваем размер ответа — ICS обычно <500КБ, 5МБ это safety cap
    # против юзера который специально подложил гигабайт.
    if len(r.content) > 5 * 1024 * 1024:
        log.warning(f"[ics] response too large: {len(r.content)} bytes")
        return []
    now = datetime.utcnow()
    return parse_ics_events(r.text, limit=50, from_dt=now)


# ── Main aggregator ──────────────────────────────────────────────────────────


async def fetch_all_user_events(db, user_id: int,
                                days_ahead: int = 14) -> list[dict]:
    """Все события юзера со всех его источников.

    Объединяет:
      • LocalCalendarEvent (наша БД — events созданные в /calendar.html
        или оркестратором при «внеси в календарь...»)
      • Google + Yandex + ICS (через _fetch_for_connection)

    Сортирует по start, ограничивает 50.
    Возвращает [{title, start, end, description, location, source, ...}, ...]
    где source = local|google|yandex|ics.
    """
    from server.models import UserCalendarConnection, LocalCalendarEvent
    from datetime import timedelta as _td
    all_events: list[dict] = []

    # 1. Локальные события из БД — всегда грузим, даже если нет подключений.
    now = datetime.utcnow()
    horizon = now + _td(days=days_ahead)
    local_rows = (db.query(LocalCalendarEvent)
                    .filter(LocalCalendarEvent.user_id == user_id,
                            LocalCalendarEvent.start >= now - _td(days=1),
                            LocalCalendarEvent.start <= horizon)
                    .order_by(LocalCalendarEvent.start)
                    .all())
    for r in local_rows:
        all_events.append({
            "title": r.title,
            "start": r.start,
            "end": r.end,
            "description": r.description or "",
            "location": r.location or "",
            "all_day": bool(r.all_day),
            "source": "local",
            "local_id": r.id,
            "uid": f"local-{r.id}",
        })

    # 2. Внешние подключения
    conns = (db.query(UserCalendarConnection)
               .filter_by(user_id=user_id, is_active=True)
               .all())
    for conn in conns:
        try:
            events = await _fetch_for_connection(db, conn, days_ahead)
            for ev in events:
                ev["source"] = conn.provider
                ev["connection_id"] = conn.id
            all_events.extend(events)
        except Exception as e:
            log.warning(f"[calendar] conn={conn.id} provider={conn.provider} "
                         f"failed: {type(e).__name__}: {e}")
            conn.fail_count = (conn.fail_count or 0) + 1
            conn.last_error = str(e)[:500]
    db.commit()
    all_events.sort(key=lambda e: e["start"])
    return all_events[:50]


async def _fetch_for_connection(db, conn, days_ahead: int) -> list[dict]:
    """Внутренняя — fetch для одного подключения с учётом refresh token'а."""
    if conn.provider == "google":
        # Если access_token истёк или вот-вот истечёт — refresh
        access = conn.access_token
        if (not access or
                (conn.token_expires_at and
                 conn.token_expires_at <= datetime.utcnow() + timedelta(minutes=2))):
            r = await google_refresh_access_token(conn.refresh_token or "")
            if not r:
                conn.last_error = "refresh_failed"
                return []
            access = r.get("access_token") or ""
            conn.access_token = access
            expires_in = int(r.get("expires_in") or 3600)
            conn.token_expires_at = datetime.utcnow() + timedelta(seconds=expires_in - 60)
        try:
            events = await fetch_google_events(access, conn.calendar_id or "primary",
                                                days_ahead=days_ahead)
        except PermissionError:
            # Token истёк во время вызова — refresh и retry
            r = await google_refresh_access_token(conn.refresh_token or "")
            if not r:
                conn.last_error = "refresh_failed_on_retry"
                return []
            access = r.get("access_token") or ""
            conn.access_token = access
            events = await fetch_google_events(access, conn.calendar_id or "primary",
                                                days_ahead=days_ahead)
        conn.last_synced_at = datetime.utcnow()
        conn.last_error = None
        return events
    elif conn.provider == "yandex":
        try:
            events = await fetch_yandex_events(
                conn.account_email or "", conn.access_token or "",
                days_ahead=days_ahead
            )
        except PermissionError:
            conn.last_error = "yandex_auth_failed"
            return []
        conn.last_synced_at = datetime.utcnow()
        conn.last_error = None
        return events
    elif conn.provider == "ics":
        events = await fetch_ics_events(conn.ics_url or "", days_ahead=days_ahead)
        conn.last_synced_at = datetime.utcnow()
        return events
    return []


def format_events_for_llm(events: list[dict]) -> str:
    """Превратить список событий в человекочитаемый блок для context LLM.

    Используется в invoke_module для модуля calendar — добавляется в
    system_prompt как «вот что в твоём календаре сейчас».
    """
    if not events:
        return ("📅 Календарь подключен, но событий в ближайшие 14 дней нет "
                "(или они приватные и не выдаются API).")
    parts = ["📅 Ближайшие события в твоём календаре:"]
    for ev in events[:20]:  # макс 20 в context (rest LLM не съест)
        when = ev["start"].strftime("%a %d.%m %H:%M") if ev["start"] else "?"
        title = ev["title"] or "(без названия)"
        location = f" · 📍 {ev['location']}" if ev.get("location") else ""
        source = ev.get("source", "")
        src_em = {"google": "🌐", "yandex": "🇷🇺", "ics": "🔗"}.get(source, "")
        parts.append(f"  {src_em} {when} — {title}{location}")
    return "\n".join(parts)
