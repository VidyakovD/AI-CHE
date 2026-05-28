"""Низкоуровневый клиент VK Ads API для модуля `vk_ads`.

Использует user-токен с scope `ads`. Community-токен НЕ подойдёт
(возвращает ошибку 27 «Group authorization failed»).

Юзер получает токен через VKHost (vkhost.github.io) или OAuth-flow с
scope=ads,offline → токен живёт без срока действия.

Documentation: https://dev.vk.com/ru/method/ads
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("vk_ads")

VK_API_BASE = "https://api.vk.com/method"
VK_API_VERSION = "5.131"


def _call(method: str, token: str, **params) -> dict:
    """Синхронный POST к VK API. Возвращает {response} или {error}."""
    if not token:
        return {"error": {"error_code": -1, "error_msg": "Нет ads-токена"}}
    payload = {
        "access_token": token.strip(),
        "v": VK_API_VERSION,
        **{k: str(v) for k, v in params.items() if v is not None and v != ""},
    }
    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.post(f"{VK_API_BASE}/{method}", data=payload)
            return r.json() if r.content else {"error": {"error_msg": "empty response"}}
    except Exception as e:
        log.warning(f"[vk_ads] {method} failed: {type(e).__name__}: {e}")
        return {"error": {"error_msg": f"Сеть: {type(e).__name__}"}}


def list_accounts(token: str) -> list[dict]:
    """ads.getAccounts — список рекламных аккаунтов юзера.

    Returns:
        [{account_id, account_type, account_status, account_name}, ...]
        Пустой list при ошибке (детали в логе).
    """
    d = _call("ads.getAccounts", token)
    if "error" in d:
        log.warning(f"[vk_ads.getAccounts] {d['error']}")
        return []
    return d.get("response") or []


def list_campaigns(token: str, account_id: int | str,
                    include_deleted: bool = False) -> list[dict]:
    """ads.getCampaigns — кампании в рекламном аккаунте.

    Returns: [{id, type, name, status, day_limit, all_limit, start_time, stop_time}]
    status: 0=stopped, 1=running, 2=deleted
    """
    d = _call("ads.getCampaigns", token,
              account_id=account_id,
              include_deleted=1 if include_deleted else 0)
    if "error" in d:
        log.warning(f"[vk_ads.getCampaigns] account={account_id}: {d['error']}")
        return []
    return d.get("response") or []


def get_campaign_stats(token: str, account_id: int | str,
                       campaign_ids: list[int], period: str = "month",
                       date_from: str = "0", date_to: str = "0") -> list[dict]:
    """ads.getStatistics — статистика по кампаниям.

    period: 'day' | 'week' | 'month' | 'overall'
    date_from/date_to: '0' = текущий период, иначе 'YYYY-MM-DD'

    Returns: [{id, type, stats: [{spent, impressions, clicks, ctr, ...}, ...]}, ...]
    """
    if not campaign_ids:
        return []
    d = _call("ads.getStatistics", token,
              account_id=account_id,
              ids_type="campaign",
              ids=",".join(str(c) for c in campaign_ids[:100]),
              period=period,
              date_from=date_from,
              date_to=date_to)
    if "error" in d:
        log.warning(f"[vk_ads.getStatistics] account={account_id}: {d['error']}")
        return []
    return d.get("response") or []


def list_ads(token: str, account_id: int | str,
              campaign_ids: list[int] | None = None) -> list[dict]:
    """ads.getAds — объявления (внутри кампаний).

    Returns: [{id, campaign_id, name, status, impressions, clicks}]
    """
    extra = {}
    if campaign_ids:
        extra["campaign_ids"] = ",".join(str(c) for c in campaign_ids[:100])
    d = _call("ads.getAds", token, account_id=account_id, **extra)
    if "error" in d:
        log.warning(f"[vk_ads.getAds] account={account_id}: {d['error']}")
        return []
    return d.get("response") or []


def build_ads_summary(token: str, account_id: int | str) -> str:
    """Markdown-сводка кампаний для подмешивания в system_prompt модуля.

    Используется в `_fetch_vk_ads_context_for_user` — даёт LLM представление
    о текущем состоянии рекламы юзера БЕЗ необходимости отдельных tool-вызовов.

    Возвращает строку (markdown). Если кампаний нет / токен невалидный —
    короткий placeholder.
    """
    accounts = list_accounts(token)
    if not accounts:
        return ("\n═══ ВК РЕКЛАМА ═══\n"
                "📭 Рекламных аккаунтов не найдено или токен невалидный.\n"
                "Юзеру: получи user-токен с scope=ads на vkhost.github.io")

    # Если account_id не задан явно — берём первый
    acc = None
    for a in accounts:
        if str(a.get("account_id")) == str(account_id):
            acc = a
            break
    if acc is None:
        acc = accounts[0]
    aid = acc.get("account_id")

    lines = ["", "═══ ВК РЕКЛАМА (контекст модуля) ═══"]
    lines.append(f"📊 Аккаунт: {acc.get('account_name', '?')} (id={aid}, "
                 f"type={acc.get('account_type', '?')})")

    campaigns = list_campaigns(token, aid)
    if not campaigns:
        lines.append("📭 Кампаний в аккаунте нет.")
        return "\n".join(lines)

    lines.append(f"\n🎯 Кампаний всего: {len(campaigns)}")
    active = [c for c in campaigns if c.get("status") == 1]
    stopped = [c for c in campaigns if c.get("status") == 0]
    lines.append(f"  • Активных: {len(active)}, остановленных: {len(stopped)}")

    # Статистика за последний месяц
    camp_ids = [int(c["id"]) for c in campaigns[:30] if c.get("id")]
    stats = get_campaign_stats(token, aid, camp_ids, period="month") if camp_ids else []
    stats_by_id = {}
    for s in stats:
        sid = s.get("id")
        srows = s.get("stats") or []
        if srows:
            stats_by_id[sid] = srows[0]  # период один → одна строка

    lines.append("\n📈 ТОП кампаний (по тратам за месяц):")
    enriched = []
    for c in campaigns:
        cid = int(c.get("id") or 0)
        st = stats_by_id.get(cid) or {}
        enriched.append({
            "id": cid,
            "name": (c.get("name") or "")[:40],
            "status": "🟢" if c.get("status") == 1 else "⏸",
            "spent": float(st.get("spent") or 0),
            "impressions": int(st.get("impressions") or 0),
            "clicks": int(st.get("clicks") or 0),
            "ctr": float(st.get("ctr") or 0),
        })
    enriched.sort(key=lambda x: x["spent"], reverse=True)
    for e in enriched[:10]:
        spent_rub = e["spent"]
        cpc = (spent_rub / e["clicks"]) if e["clicks"] > 0 else 0
        lines.append(
            f"  {e['status']} #{e['id']} «{e['name']}»: "
            f"расход={spent_rub:.0f}₽ показы={e['impressions']} "
            f"клики={e['clicks']} CTR={e['ctr']:.2f}% "
            + (f"CPC={cpc:.1f}₽" if cpc > 0 else "")
        )

    total_spent = sum(e["spent"] for e in enriched)
    total_clicks = sum(e["clicks"] for e in enriched)
    total_imp = sum(e["impressions"] for e in enriched)
    avg_ctr = (total_clicks / total_imp * 100) if total_imp else 0
    avg_cpc = (total_spent / total_clicks) if total_clicks else 0
    lines.append(
        f"\n💰 Итого за месяц: расход={total_spent:.0f}₽ "
        f"показы={total_imp} клики={total_clicks} "
        f"avgCTR={avg_ctr:.2f}% avgCPC={avg_cpc:.1f}₽"
    )

    return "\n".join(lines)


def update_campaign_status(token: str, account_id: int | str,
                             campaign_id: int | str, status: int) -> dict:
    """ads.updateCampaigns — изменить статус кампании.

    status: 0 = stopped (пауза), 1 = running (старт)
    Возвращает {ok: bool, error: str|None}.
    """
    import json as _json
    if status not in (0, 1):
        return {"ok": False, "error": "status должен быть 0 (стоп) или 1 (старт)"}
    data = [{"campaign_id": int(campaign_id), "status": int(status)}]
    d = _call("ads.updateCampaigns", token,
              account_id=account_id, data=_json.dumps(data))
    if "error" in d:
        return {"ok": False, "error": str(d["error"])[:300]}
    resp = d.get("response") or []
    # VK возвращает массив 0/1 для каждой кампании в запросе
    success = bool(resp) and (resp[0] == 0 or resp[0] is True)
    if not success:
        return {"ok": False, "error": f"VK вернул {resp!r}"}
    return {"ok": True, "error": None}


def set_campaign_day_limit(token: str, account_id: int | str,
                             campaign_id: int | str,
                             day_limit_rub: int) -> dict:
    """ads.updateCampaigns с day_limit. VK ждёт суммы в КОПЕЙКАХ ₽."""
    import json as _json
    if not isinstance(day_limit_rub, (int, float)) or day_limit_rub < 0:
        return {"ok": False, "error": "day_limit_rub должен быть ≥ 0"}
    data = [{"campaign_id": int(campaign_id),
             "day_limit": int(day_limit_rub * 100)}]
    d = _call("ads.updateCampaigns", token,
              account_id=account_id, data=_json.dumps(data))
    if "error" in d:
        return {"ok": False, "error": str(d["error"])[:300]}
    resp = d.get("response") or []
    success = bool(resp) and (resp[0] == 0 or resp[0] is True)
    return {"ok": success,
            "error": None if success else f"VK вернул {resp!r}"}


def get_targeting_stats(token: str, account_id: int | str,
                         criteria: dict) -> dict:
    """ads.getTargetingStats — оценка размера аудитории по таргетингу.

    criteria — словарь полей таргетинга (sex, age_from, age_to, country, cities,
    interests_categories, и т.д.). См. https://dev.vk.com/ru/method/ads.getTargetingStats

    Returns: {audience_count, recommended_cpm, recommended_cpc}
    """
    d = _call("ads.getTargetingStats", token,
              account_id=account_id,
              criteria=str(criteria) if not isinstance(criteria, str) else criteria,
              link_url="https://vk.com",  # требуется как dummy для оценки
              ad_format=1)  # 1=image-and-text
    if "error" in d:
        log.warning(f"[vk_ads.getTargetingStats] {d['error']}")
        return {}
    return d.get("response") or {}
