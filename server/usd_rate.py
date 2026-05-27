"""Курс доллара ЦБ РФ — для пересчёта USD-себестоимости моделей в рубли.

Источник: cbr-xml-daily.ru (бесплатный mirror ЦБ-API).
Обновляется раз в сутки cron-задачей (см. server/cron/recalc_pricing.py).
Кэш в pricing_config['system.usd_rate'] (целые копейки рубля × 100, т.к.
pricing.value_kop = int). При недоступности API — используется последний
известный курс из БД, при первом запуске — fallback 90.0.

Использование:
    from server.usd_rate import get_usd_rate
    rate = get_usd_rate()    # → 92.34 (RUB за 1 USD)
"""
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

USD_RATE_CBR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"
FALLBACK_RATE = 90.0
PRICING_KEY = "system.usd_rate_kop"  # курс × 100 (целые копейки за $1)
PRICING_TS_KEY = "system.usd_rate_ts"  # unix timestamp последнего обновления


def get_usd_rate() -> float:
    """Текущий кэшированный курс. Не делает сетевой запрос — для горячих путей."""
    from server.db import db_session
    from server.models import PricingConfig
    try:
        with db_session() as db:
            row = db.query(PricingConfig).filter_by(key=PRICING_KEY).first()
            if row and row.value_kop > 0:
                return float(row.value_kop) / 100.0
    except Exception as e:
        log.warning(f"[usd_rate] read failed: {e}")
    return FALLBACK_RATE


def fetch_and_update_usd_rate() -> float:
    """Запрашивает свежий курс из ЦБ, сохраняет в БД, возвращает float.

    Вызывается из cron раз в сутки. При недоступности возвращает текущий
    кэш (старый), не падает.
    """
    import httpx
    try:
        r = httpx.get(USD_RATE_CBR_URL, timeout=15.0, follow_redirects=False)
        if r.status_code != 200:
            log.warning(f"[usd_rate] CBR returned {r.status_code}")
            return get_usd_rate()
        data = r.json()
        rate = float(((data.get("Valute") or {}).get("USD") or {}).get("Value") or 0)
        if rate <= 1.0 or rate > 1000.0:
            log.warning(f"[usd_rate] подозрительный курс {rate}, игнорируем")
            return get_usd_rate()
    except Exception as e:
        log.warning(f"[usd_rate] fetch failed: {type(e).__name__}: {e}")
        return get_usd_rate()
    _save_rate(rate)
    log.info(f"[usd_rate] обновлено: 1 USD = {rate:.4f} ₽")
    return rate


def _save_rate(rate: float) -> None:
    """Сохраняет курс в pricing_config (× 100, чтобы было целое в value_kop)."""
    from server.db import db_session
    from server.models import PricingConfig
    rate_kop = int(round(rate * 100))
    ts = int(datetime.now(timezone.utc).timestamp())
    try:
        with db_session() as db:
            for key, val in ((PRICING_KEY, rate_kop), (PRICING_TS_KEY, ts)):
                row = db.query(PricingConfig).filter_by(key=key).first()
                if row:
                    row.value_kop = int(val)
                else:
                    row = PricingConfig(key=key, value_kop=int(val),
                                         label=("USD rate × 100" if key == PRICING_KEY
                                                 else "USD rate updated_at (unix)"))
                    db.add(row)
            db.commit()
        # Инвалидируем кэш pricing'а — get_price теперь увидит свежие значения
        try:
            from server.pricing import invalidate_pricing_cache
            invalidate_pricing_cache()
        except Exception:
            pass
    except Exception as e:
        log.warning(f"[usd_rate] save failed: {e}")
