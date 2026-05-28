"""Низкоуровневый клиент Яндекс.Директ API v5 для модуля `direct_ads`.

Для модуля direct_ads сейчас используем только write-операции в рамках
confirm-flow (пауза, бюджет). READ-операции не нужны на этом уровне —
их юзер сам подмешивает в context или мы делаем через отдельный sync.

Документация: https://yandex.ru/dev/direct/doc/dg/about.html

Авторизация: OAuth-токен от Я.Директа (получает юзер в кабинете Direct
→ Сервисы → Управление токенами). Хранится в UserAdsConnection.access_token.
"""
from __future__ import annotations

import json
import logging

import httpx

log = logging.getLogger("yandex_direct")

API_BASE_V5 = "https://api.direct.yandex.com/json/v5"
SANDBOX_BASE_V5 = "https://api-sandbox.direct.yandex.com/json/v5"


def _post(service: str, token: str, payload: dict, *,
           sandbox: bool = False, timeout: int = 20) -> dict:
    """POST к API v5. Возвращает {result|error}."""
    base = SANDBOX_BASE_V5 if sandbox else API_BASE_V5
    url = f"{base}/{service}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept-Language": "ru",
        "Content-Type": "application/json; charset=utf-8",
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(url, headers=headers,
                            content=json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    except Exception as e:
        log.warning(f"[ya-direct] {service} net err: {type(e).__name__}")
        return {"error": {"error_string": f"Сеть: {type(e).__name__}"}}
    if r.status_code == 401:
        return {"error": {"error_string": "Не авторизован (токен истёк или невалидный)"}}
    try:
        data = r.json()
    except Exception:
        return {"error": {"error_string": f"Не-JSON ответ от Direct: {r.text[:200]}"}}
    return data


def pause_campaign(token: str, campaign_id: int | str, *,
                    sandbox: bool = False) -> dict:
    """Поставить кампанию на паузу через campaigns.suspend.

    Возвращает {ok: bool, error: str|None}.
    """
    payload = {
        "method": "suspend",
        "params": {"SelectionCriteria": {"Ids": [int(campaign_id)]}},
    }
    resp = _post("campaigns", token, payload, sandbox=sandbox)
    if "error" in resp:
        return {"ok": False,
                "error": (resp["error"].get("error_string") or "Ошибка Direct")[:300]}
    result = resp.get("result") or {}
    suspended = result.get("SuspendResults") or []
    if not suspended:
        return {"ok": False, "error": "Direct не вернул SuspendResults"}
    first = suspended[0]
    if first.get("Errors"):
        return {"ok": False,
                "error": "; ".join(e.get("Message", "") for e in first["Errors"])[:300]}
    return {"ok": True, "error": None}


def resume_campaign(token: str, campaign_id: int | str, *,
                     sandbox: bool = False) -> dict:
    """Возобновить кампанию через campaigns.resume."""
    payload = {
        "method": "resume",
        "params": {"SelectionCriteria": {"Ids": [int(campaign_id)]}},
    }
    resp = _post("campaigns", token, payload, sandbox=sandbox)
    if "error" in resp:
        return {"ok": False,
                "error": (resp["error"].get("error_string") or "Ошибка Direct")[:300]}
    result = resp.get("result") or {}
    resumed = result.get("ResumeResults") or []
    if not resumed:
        return {"ok": False, "error": "Direct не вернул ResumeResults"}
    first = resumed[0]
    if first.get("Errors"):
        return {"ok": False,
                "error": "; ".join(e.get("Message", "") for e in first["Errors"])[:300]}
    return {"ok": True, "error": None}


def set_daily_budget(token: str, campaign_id: int | str,
                      daily_budget_rub: float, *,
                      sandbox: bool = False) -> dict:
    """Установить дневной бюджет кампании через campaigns.update.

    daily_budget_rub в РУБЛЯХ (Direct API ждёт значение × 1_000_000 для
    Amount в копейках_×_10000 = micros).
    """
    if daily_budget_rub <= 0:
        return {"ok": False, "error": "daily_budget_rub должен быть > 0"}

    # Direct API представляет деньги в "micros" (1 RUB = 1_000_000)
    amount_micros = int(daily_budget_rub * 1_000_000)
    payload = {
        "method": "update",
        "params": {
            "Campaigns": [{
                "Id": int(campaign_id),
                "DailyBudget": {
                    "Amount": amount_micros,
                    "Mode": "STANDARD",
                },
            }],
        },
    }
    resp = _post("campaigns", token, payload, sandbox=sandbox)
    if "error" in resp:
        return {"ok": False,
                "error": (resp["error"].get("error_string") or "Ошибка Direct")[:300]}
    result = resp.get("result") or {}
    upd = result.get("UpdateResults") or []
    if not upd:
        return {"ok": False, "error": "Direct не вернул UpdateResults"}
    first = upd[0]
    if first.get("Errors"):
        return {"ok": False,
                "error": "; ".join(e.get("Message", "") for e in first["Errors"])[:300]}
    return {"ok": True, "error": None}
