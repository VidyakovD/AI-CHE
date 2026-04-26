"""
Динамические цены из БД (таблица pricing_config) с TTL-кэшем.

Заменяет hardcoded константы вроде SITE_CREATE_FIX_COST. Админ может
поменять цену через `/admin/pricing` без редеплоя.

Использование:
    from server.pricing import get_price
    cost_kop = get_price("site.standard", default=150_000)

Кэш: 60 секунд. После UPDATE через админку — `invalidate_pricing_cache()`.
"""
from __future__ import annotations
import logging
import time
from server.db import db_session
from server.models import PricingConfig

log = logging.getLogger(__name__)

_TTL_SEC = 60
_cache: dict[str, tuple[float, int]] = {}


# Дефолты (в копейках). Используются при миссинге в БД и при first-run seed.
DEFAULTS: dict[str, tuple[int, str]] = {
    # (default_kop, label)
    "site.standard": (150_000, "Создание сайта — Стандарт (Sonnet)"),
    "site.premium":  (199_000, "Создание сайта — Премиум (Opus)"),
    "site.iter":     (    500, "Доработка сайта (одна итерация)"),
    "site.spec":     (      0, "Обсуждение ТЗ сайта"),
    "site.edit_block": (   500, "AI-правка блока сайта"),
    "presentation.standard": (5_000, "Презентация — Стандарт"),
    "presentation.premium":  (10_000, "Презентация — Премиум"),
    "kp.standard":           (5_000, "КП — Стандарт"),
    "kp.premium":            (10_000, "КП — Премиум"),
    "solution.standard":     (5_000, "Бизнес-решение — Стандарт"),
    "solution.premium":      (10_000, "Бизнес-решение — Премиум"),
    # Хранилище файлов юзеров (лидмагниты, медиа)
    "storage.per_100mb_month": (5_000, "Хранилище файлов: 50 ₽/мес за каждые 100 МБ"),
    "storage.upload_per_mb":   (    0, "Разовая плата за загрузку (₽ за МБ, 0 = бесплатно)"),
}


def get_price(key: str, default: int | None = None) -> int:
    """
    Текущая цена из БД (или дефолт). Кэш 60 секунд.
    Если default не передан и записи в БД нет — берётся DEFAULTS[key][0].
    """
    now = time.time()
    cached = _cache.get(key)
    if cached and (now - cached[0]) < _TTL_SEC:
        return cached[1]
    value: int | None = None
    try:
        with db_session() as db:
            row = db.query(PricingConfig).filter_by(key=key).first()
            if row is not None:
                value = int(row.value_kop)
    except Exception as e:
        log.warning(f"[pricing] failed to read {key}: {e}")
    if value is None:
        if default is not None:
            value = int(default)
        elif key in DEFAULTS:
            value = DEFAULTS[key][0]
        else:
            value = 0
    _cache[key] = (now, value)
    return value


def invalidate_pricing_cache(key: str | None = None) -> None:
    if key:
        _cache.pop(key, None)
    else:
        _cache.clear()


def seed_pricing_defaults() -> None:
    """
    Создать в БД записи с дефолтными ценами если их ещё нет.
    Вызывается при старте main.py.
    """
    try:
        with db_session() as db:
            existing = {r.key for r in db.query(PricingConfig).all()}
            added = 0
            for key, (kop, label) in DEFAULTS.items():
                if key in existing:
                    continue
                db.add(PricingConfig(key=key, value_kop=kop, label=label))
                added += 1
            if added:
                db.commit()
                log.info(f"[pricing] seeded {added} default prices")
    except Exception as e:
        log.warning(f"[pricing] seed failed: {e}")


def list_all_pricing() -> list[dict]:
    """Все цены для админки (упорядочены по key)."""
    out = []
    with db_session() as db:
        rows = db.query(PricingConfig).order_by(PricingConfig.key).all()
        existing = {r.key for r in rows}
        for r in rows:
            out.append({
                "key": r.key,
                "value_kop": int(r.value_kop),
                "value_rub": round(int(r.value_kop) / 100, 2),
                "label": r.label or DEFAULTS.get(r.key, (0, ""))[1],
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            })
        # Добавляем дефолтные (ещё не сохранённые) — чтобы в админке было видно полный список
        for key, (kop, label) in DEFAULTS.items():
            if key in existing:
                continue
            out.append({
                "key": key, "value_kop": kop,
                "value_rub": round(kop / 100, 2),
                "label": label, "updated_at": None,
            })
    return out


def update_price(key: str, value_kop: int, label: str | None = None) -> bool:
    """Обновить цену через админку. Возвращает True если изменено."""
    if value_kop < 0:
        return False
    with db_session() as db:
        row = db.query(PricingConfig).filter_by(key=key).first()
        if row is None:
            db.add(PricingConfig(
                key=key, value_kop=int(value_kop),
                label=label or DEFAULTS.get(key, (0, ""))[1],
            ))
        else:
            row.value_kop = int(value_kop)
            if label is not None:
                row.label = label
        db.commit()
    invalidate_pricing_cache(key)
    return True
