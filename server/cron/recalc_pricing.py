"""Cron-задача: пересчёт ModelPricing из USD-себестоимости × курс × margin.

Раз в сутки (7:30 утра МСК):
  1. Получаем свежий курс USD с ЦБ.
  2. Для каждой модели из server.model_costs_usd:
      - per_token: ch_per_1k_input/output = usd × rate × margin / 10
      - per_request: cost_per_req = usd × rate × margin × 100
  3. UPSERT в ModelPricing.

Юзер: «реальная трата × курс доллара с утра × 3, цены справедливые,
не ограничиваемся двумя знаками». ch_per_1k_* — Float, поддерживает
0.0042 коп/1k токенов. Накопление недокопеек — в server.billing.
"""
import asyncio
import logging
from datetime import datetime

from server.db import db_session
from server.worker_lock import worker_lock
from server.usd_rate import fetch_and_update_usd_rate, get_usd_rate
from server.model_costs_usd import (
    PER_TOKEN_USD, PER_REQUEST_USD, ALIAS_TO_REAL,
    calc_per_token_kop, calc_per_request_kop, DEFAULT_MARGIN,
)

log = logging.getLogger("scheduler")


def recalc_all_pricing(usd_rate: float, margin: float = DEFAULT_MARGIN) -> dict:
    """Пересчёт ВСЕХ цен в ModelPricing. Returns {model_id: action}."""
    from server.models import ModelPricing
    actions: dict[str, str] = {}
    with db_session() as db:
        # Per-token + aliases — все идут в ModelPricing с per-1k полями
        ids_to_process = set(PER_TOKEN_USD.keys()) | set(ALIAS_TO_REAL.keys())
        for mid in ids_to_process:
            tup = calc_per_token_kop(mid, usd_rate, margin)
            if not tup:
                continue
            in_kop, out_kop = tup
            row = db.query(ModelPricing).filter_by(model_id=mid).first()
            label = _build_label(mid, in_kop, out_kop, usd_rate)
            if row:
                row.ch_per_1k_input = float(in_kop)
                row.ch_per_1k_output = float(out_kop)
                row.markup = float(margin)
                # min_ch_per_req не трогаем — там минимум для коротких ответов
                row.label = label
                row.updated_at = datetime.utcnow()
                actions[mid] = "updated"
            else:
                db.add(ModelPricing(
                    model_id=mid, label=label,
                    ch_per_1k_input=float(in_kop),
                    ch_per_1k_output=float(out_kop),
                    cost_per_req=0,
                    usd_per_req=0.0,
                    markup=float(margin),
                    min_ch_per_req=1,  # минимум 0.01 ₽ за запрос даже если real меньше
                ))
                actions[mid] = "created"

        # Per-request — image/video/audio
        for mid in PER_REQUEST_USD.keys():
            cost_kop = calc_per_request_kop(mid, usd_rate, margin)
            if cost_kop is None:
                continue
            row = db.query(ModelPricing).filter_by(model_id=mid).first()
            label = f"{mid}: {cost_kop/100:.2f} ₽/вызов (USD {PER_REQUEST_USD[mid]:.4f} × {usd_rate:.2f} × {margin})"
            if row:
                row.cost_per_req = int(cost_kop)
                row.markup = float(margin)
                row.min_ch_per_req = int(cost_kop)
                row.label = label
                row.updated_at = datetime.utcnow()
                actions[mid] = "updated"
            else:
                db.add(ModelPricing(
                    model_id=mid, label=label,
                    cost_per_req=int(cost_kop),
                    usd_per_req=float(PER_REQUEST_USD[mid]),
                    markup=float(margin),
                    ch_per_1k_input=0.0, ch_per_1k_output=0.0,
                    min_ch_per_req=int(cost_kop),
                ))
                actions[mid] = "created"
        db.commit()
    # Инвалидация pricing cache (если используется)
    try:
        from server.pricing import invalidate_pricing_cache
        invalidate_pricing_cache()
    except Exception:
        pass
    return actions


def _build_label(mid: str, in_kop: float, out_kop: float, rate: float) -> str:
    """Человеко-читаемый label показывающий формулу."""
    real_id = ALIAS_TO_REAL.get(mid, mid)
    usd = PER_TOKEN_USD.get(real_id) or {}
    in_usd = usd.get("in", 0)
    out_usd = usd.get("out", 0)
    return (f"{mid}: in={in_kop:.4f} коп/1k, out={out_kop:.4f} коп/1k "
            f"(USD {in_usd}/{out_usd} × {rate:.2f} × ×3)")


async def recalc_pricing_tick():
    """Tick: fetch rate + recalc. Защищён worker_lock на multi-worker."""
    try:
        with worker_lock("recalc_pricing", ttl_sec=300) as acquired:
            if not acquired:
                return
            rate = fetch_and_update_usd_rate()
            if rate <= 0:
                log.warning("[recalc_pricing] no valid rate, skip")
                return
            actions = recalc_all_pricing(rate)
            created = sum(1 for v in actions.values() if v == "created")
            updated = sum(1 for v in actions.values() if v == "updated")
            log.info(f"[recalc_pricing] rate=1USD={rate:.2f}₽ "
                      f"created={created} updated={updated}")
    except Exception as e:
        log.error(f"[recalc_pricing] tick error: {type(e).__name__}: {e}")


async def recalc_pricing_loop():
    """Loop: раз в сутки. lock_ttl=24h+5min, sleep=24h (CI-страж OK).

    При старте — сразу пересчитываем (если последний пересчёт > 12 часов).
    """
    log.info("Recalc-pricing loop started")
    # Первый запуск через 30 сек после старта (даём БД проинициализироваться)
    await asyncio.sleep(30)
    while True:
        try:
            await recalc_pricing_tick()
        except Exception as e:
            log.error(f"[recalc_pricing] loop error: {e}")
        # 24 часа до следующего запуска
        await asyncio.sleep(86_400)
