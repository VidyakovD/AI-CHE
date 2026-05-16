"""Checko.ru API — ЕГРЮЛ/ЕГРИП lookup для роли «Юрист» ИИ Агентов v2.

Один запрос /v2/company или /v2/entrepreneur по ИНН → полная карточка
контрагента: статус, ОКВЭД, директор, юр.адрес, аффилированные компании,
суды, проверки, базовая финотчётность.

Использование (внутри solutions_orchestra stage `inn_lookup`):
    from server.tools.checko_lookup import lookup_inn_md
    md = lookup_inn_md("7707083893")  # → форматированный Markdown

Кэш: in-memory TTL 24ч по ИНН — один и тот же контрагент не дёргает API
повторно в течение суток (экономия лимита Checko-плана).

Документация Checko: https://checko.ru/api
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

CHECKO_BASE_URL = "https://api.checko.ru/v2"
CACHE_TTL_SEC = 24 * 3600  # 24 часа

# {inn: (timestamp, data_dict)} — простой in-memory кэш per-worker
_cache: dict[str, tuple[float, dict]] = {}


class CheckoError(RuntimeError):
    """Любая ошибка Checko API: не настроен ключ, rate limit, неверный ИНН, сеть."""


def _get_api_key() -> str:
    key = os.getenv("CHECKO_API_KEY", "").strip()
    if not key:
        raise CheckoError(
            "CHECKO_API_KEY не настроен в .env. Получить ключ: https://checko.ru/api"
        )
    return key


def _detect_entity_kind(inn: str) -> str:
    """ИНН 10 цифр → юр.лицо, 12 цифр → ИП/физлицо."""
    inn = (inn or "").strip()
    if len(inn) == 10 and inn.isdigit():
        return "company"
    if len(inn) == 12 and inn.isdigit():
        return "entrepreneur"
    raise CheckoError(f"Некорректный ИНН (должно быть 10 цифр для ООО или 12 для ИП): {inn!r}")


def _fetch(endpoint: str, inn: str) -> dict:
    """GET /v2/{endpoint}?key=...&inn=... с обработкой ошибок."""
    key = _get_api_key()
    url = f"{CHECKO_BASE_URL}/{endpoint}"
    try:
        r = httpx.get(url, params={"key": key, "inn": inn}, timeout=20.0)
    except httpx.RequestError as e:
        raise CheckoError(f"Сеть до Checko недоступна: {e}") from e
    if r.status_code == 401:
        raise CheckoError("Checko API: ключ невалиден (401). Проверьте CHECKO_API_KEY.")
    if r.status_code == 402:
        raise CheckoError("Checko API: исчерпан лимит тарифа (402). Пополните план.")
    if r.status_code == 429:
        raise CheckoError("Checko API: rate limit (429). Подождите и повторите.")
    if r.status_code == 404:
        raise CheckoError(f"Контрагент по ИНН {inn} в Checko не найден.")
    if r.status_code >= 500:
        raise CheckoError(f"Checko API недоступен ({r.status_code}). Попробуйте позже.")
    if r.status_code != 200:
        raise CheckoError(f"Checko API: HTTP {r.status_code}")
    try:
        payload = r.json()
    except Exception as e:
        raise CheckoError(f"Checko вернул не-JSON: {e}") from e
    data = payload.get("data")
    if data is None:
        meta = payload.get("meta", {})
        if meta.get("status") == 404:
            raise CheckoError(f"Контрагент по ИНН {inn} не найден в реестрах.")
        raise CheckoError(f"Checko: пустой ответ — {payload}")
    return data


def lookup(inn: str) -> dict:
    """Получить структурированные данные по ИНН (с кэшем). Кидает CheckoError."""
    inn = (inn or "").strip()
    if not inn:
        raise CheckoError("Пустой ИНН")
    # Кэш
    cached = _cache.get(inn)
    if cached and (time.time() - cached[0]) < CACHE_TTL_SEC:
        log.info(f"[checko] cache hit inn={inn}")
        return cached[1]

    kind = _detect_entity_kind(inn)
    data = _fetch(kind, inn)
    _cache[inn] = (time.time(), data)
    log.info(f"[checko] fetched inn={inn} kind={kind} keys={list(data.keys())[:8]}")
    return data


# ── Форматирование в Markdown для подачи в LLM-stage ─────────────────────────

def _fmt_status(data: dict) -> str:
    st = data.get("Статус") or {}
    if isinstance(st, dict):
        return f"{st.get('Наим', '—')} (код {st.get('Код', '?')})"
    return str(st or "—")


def _fmt_address(data: dict) -> str:
    a = data.get("ЮрАдрес") or data.get("Адрес") or {}
    if isinstance(a, dict):
        return a.get("АдресРФ") or a.get("АдрСтрока") or "—"
    return str(a or "—")


def _fmt_okved(data: dict) -> str:
    okved = data.get("ОКВЭД") or {}
    if isinstance(okved, dict):
        code = okved.get("Код", "")
        name = okved.get("Наим", "")
        return f"{code} — {name}".strip(" —") or "—"
    return "—"


def _fmt_director(data: dict) -> str:
    rk = data.get("Руководитель") or data.get("Управление") or {}
    if isinstance(rk, dict):
        fio = rk.get("ФИО") or rk.get("Наим") or "—"
        post = rk.get("НаимДолжн") or rk.get("Должность") or ""
        return f"{fio}" + (f" ({post})" if post else "")
    return "—"


def _fmt_finances(data: dict) -> str:
    """Базовая финансовая сводка если есть в /company response."""
    fin = data.get("Финансы") or data.get("ФинОтч") or {}
    if not isinstance(fin, dict) or not fin:
        return "_данные финотчётности не предоставлены_"
    lines = []
    for year, vals in sorted(fin.items(), reverse=True)[:3]:
        if not isinstance(vals, dict):
            continue
        rev = vals.get("Выруч") or vals.get("Выручка") or vals.get("2110")
        prof = vals.get("ЧистПриб") or vals.get("ЧистаяПрибыль") or vals.get("2400")
        if rev or prof:
            lines.append(f"- **{year}**: выручка {rev or '—'}, чистая прибыль {prof or '—'}")
    return "\n".join(lines) if lines else "_данные финотчётности не предоставлены_"


def _fmt_court_cases(data: dict) -> str:
    cc = data.get("СудДела") or data.get("Арбитраж") or {}
    if not isinstance(cc, dict):
        return ""
    total = cc.get("ВсегоДел") or cc.get("Всего")
    if total is None:
        return ""
    return f"\n**Судебные дела:** {total} (по данным арбитража)\n"


def format_md(data: dict, inn: str) -> str:
    """Превращаем JSON Checko в читаемый Markdown — это идёт прямо в context
    стадии synthesize для LLM. LLM сама решает что важно подсветить."""
    is_ip = (data.get("ВидПред") or "").lower().startswith("инд") or len(inn) == 12
    name = (data.get("НаимПолн") or data.get("НаимСокр")
            or data.get("ФИО") or "—")
    ogrn = data.get("ОГРН") or data.get("ОГРНИП") or "—"
    kpp = data.get("КПП", "—") if not is_ip else "—"
    date_reg = data.get("ДатаРег") or data.get("ДатаОГРН") or "—"
    region = (data.get("Регион") or {}).get("Наим") if isinstance(data.get("Регион"), dict) else "—"

    lines = [
        f"### Карточка контрагента (источник: Checko, ИНН {inn})",
        "",
        f"- **Тип:** {'ИП' if is_ip else 'Юр. лицо'}",
        f"- **Наименование:** {name}",
        f"- **ОГРН{('ИП' if is_ip else '')}:** {ogrn}",
        f"- **КПП:** {kpp}",
        f"- **Дата регистрации:** {date_reg}",
        f"- **Регион:** {region or '—'}",
        f"- **Статус:** {_fmt_status(data)}",
        f"- **Адрес:** {_fmt_address(data)}",
        f"- **Основной ОКВЭД:** {_fmt_okved(data)}",
        f"- **Руководитель:** {_fmt_director(data)}",
        "",
        "### Финансовая отчётность",
        _fmt_finances(data),
    ]
    cc = _fmt_court_cases(data)
    if cc:
        lines.append(cc)
    return "\n".join(lines)


def lookup_inn_md(inn: str) -> str:
    """High-level entry point для orchestra-stage: получить + форматировать.
    При ошибке возвращает markdown-блок с описанием проблемы (не кидает),
    чтобы pipeline мог идти дальше с частичными данными."""
    try:
        data = lookup(inn)
        return format_md(data, inn)
    except CheckoError as e:
        log.warning(f"[checko] lookup failed for {inn}: {e}")
        return (
            f"### Карточка контрагента (ИНН {inn})\n\n"
            f"⚠ Не удалось получить данные из Checko: {e}\n\n"
            "_Юрист продолжит работу на основе ресёрча, но без актуальной выписки._"
        )
    except Exception as e:
        log.exception(f"[checko] unexpected error for {inn}: {e}")
        return (
            f"### Карточка контрагента (ИНН {inn})\n\n"
            f"⚠ Неожиданная ошибка: {e}"
        )
