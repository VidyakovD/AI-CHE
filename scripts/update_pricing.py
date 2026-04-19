"""
Единая реорганизация тарификации.

База: 1 CH = 0.10 ₽ (pricing_settings.ch_to_rub).
Курс USD/RUB берём из exchange_rates (обновляется автоматически с ЦБ).

Цены моделей (CH/1k tokens) рассчитаны с маржой ~100-250% над себестоимостью
API-провайдеров (Claude Sonnet 4 $3/$15, GPT-4o $2.5/$10, и т.п.)

Запуск: python -m scripts.update_pricing
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.db import SessionLocal
from server.models import ModelPricing, TokenPackage, PricingSetting


# ═══════════════════════════════════════════════════════════════════════════════
# MODEL PRICING (CH за 1000 токенов)
# ═══════════════════════════════════════════════════════════════════════════════
# При курсе ~76 ₽/USD и 0.10 ₽/CH:
#   реальная цена Claude Sonnet 4 = $3/$15 за Mtok = 0.23/1.14 ₽ за 1k
#   → маржа 248% на input (8 CH = 0.80 ₽), 163% на output (30 CH = 3.00 ₽)

MODEL_PRICING = [
    # text models: (model_id, label, ch_per_1k_input, ch_per_1k_output, cost_per_req, min_ch_per_req)
    ("gpt",              "GPT-4o mini",    0.3,  1.0,   0, 1),   # дешёвая для чата
    ("gpt-4o",           "GPT-4o",         5.0,  20.0,  0, 3),
    ("claude-sonnet",    "Claude Sonnet",  8.0,  30.0,  0, 3),
    ("claude-haiku",     "Claude Haiku",   2.0,  10.0,  0, 1),
    ("grok",             "Grok mini",      1.0,  1.5,   0, 1),
    ("grok-large",       "Grok 4",         8.0,  30.0,  0, 3),
    ("perplexity",       "Perplexity",     2.0,  2.0,   0, 1),
    ("perplexity-large", "Perplexity Pro", 8.0,  30.0,  0, 3),
    # image/video — фикс за запрос
    ("nano",             "DALL-E 3",       0.0,  0.0,   10,  1),   # 1 картинка ~ 1 ₽
    ("kling",            "Kling video",    0.0,  0.0,   250, 1),   # видео ~ 25 ₽
    ("kling-pro",        "Kling Pro",      0.0,  0.0,   500, 1),
    ("veo",              "Veo video",      0.0,  0.0,   400, 1),
]


# ═══════════════════════════════════════════════════════════════════════════════
# TOKEN PACKAGES (разовые покупки)
# ═══════════════════════════════════════════════════════════════════════════════
# Розничная цена: 1 CH = 0.10 ₽. За объём скидка 10-30%.

TOKEN_PACKAGES = [
    # (name, tokens, price_rub, sort_order)
    ("Старт",     1_000,   99,   1),   # 0.099 ₽/CH
    ("Базовый",   5_000,   450,  2),   # 0.09  ₽/CH (-10%)
    ("Про",       20_000,  1600, 3),   # 0.08  ₽/CH (-20%)
    ("Макси",     100_000, 7000, 4),   # 0.07  ₽/CH (-30%)
]


# ═══════════════════════════════════════════════════════════════════════════════
# PRICING SETTINGS
# ═══════════════════════════════════════════════════════════════════════════════
# Не трогаем ch_to_rub (0.10) — чтобы не пересчитывать существующие балансы юзеров.
# usd_to_rub удаляем из settings как legacy — актуальный курс в exchange_rates.


def upsert_model_pricing(db):
    print("\n=== ModelPricing ===")
    # Удалить дубли (например старый 'claude' без суффикса)
    duplicates = db.query(ModelPricing).filter_by(model_id="claude").all()
    for d in duplicates:
        print(f"  [delete dup] claude (in/out={d.ch_per_1k_input}/{d.ch_per_1k_output})")
        db.delete(d)
    db.commit()

    valid_ids = {row[0] for row in MODEL_PRICING}
    for model_id, label, ch_in, ch_out, per_req, min_req in MODEL_PRICING:
        p = db.query(ModelPricing).filter_by(model_id=model_id).first()
        if p:
            old = f"{p.ch_per_1k_input}/{p.ch_per_1k_output}/per_req={p.cost_per_req}/min={p.min_ch_per_req}"
            p.label = label
            p.ch_per_1k_input = ch_in
            p.ch_per_1k_output = ch_out
            p.cost_per_req = per_req
            p.min_ch_per_req = min_req
            new = f"{ch_in}/{ch_out}/per_req={per_req}/min={min_req}"
            if old != new:
                print(f"  [update] {model_id:<20} {old} → {new}")
            else:
                print(f"  [skip]   {model_id:<20} {new}")
        else:
            db.add(ModelPricing(
                model_id=model_id, label=label,
                ch_per_1k_input=ch_in, ch_per_1k_output=ch_out,
                cost_per_req=per_req, min_ch_per_req=min_req,
            ))
            print(f"  [add]    {model_id:<20} {ch_in}/{ch_out}/per_req={per_req}/min={min_req}")

    # Удалим модели которых больше нет в списке (редко — только если раньше был хлам)
    extras = db.query(ModelPricing).filter(~ModelPricing.model_id.in_(valid_ids)).all()
    for e in extras:
        print(f"  [delete unknown] {e.model_id}")
        db.delete(e)
    db.commit()


def upsert_packages(db):
    print("\n=== TokenPackages ===")
    # Деактивируем существующие и ставим новые
    existing = db.query(TokenPackage).all()
    for e in existing:
        e.is_active = False
    db.commit()

    for idx, (name, tokens, price, sort) in enumerate(TOKEN_PACKAGES, 1):
        # Попытка обновить по имени (чтобы не плодить записи)
        p = db.query(TokenPackage).filter_by(name=name).first()
        if p:
            p.tokens = tokens
            p.price_rub = price
            p.sort_order = sort
            p.is_active = True
            print(f"  [update] {name:<10} {tokens:>6} CH = {price:>5} ₽ ({price/tokens:.3f} ₽/CH)")
        else:
            db.add(TokenPackage(
                name=name, tokens=tokens, price_rub=price,
                sort_order=sort, is_active=True,
            ))
            print(f"  [add]    {name:<10} {tokens:>6} CH = {price:>5} ₽ ({price/tokens:.3f} ₽/CH)")
    db.commit()


def ensure_ch_rate(db):
    """Гарантируем что pricing_settings.ch_to_rub=0.10 (нам явно нужен)."""
    print("\n=== PricingSettings ===")
    key = "ch_to_rub"
    s = db.query(PricingSetting).filter_by(key=key).first()
    if s:
        print(f"  [keep] {key}={s.value} ₽/CH")
    else:
        db.add(PricingSetting(key=key, value="0.10",
                              description="Стоимость 1 CH в рублях (розничная)"))
        db.commit()
        print(f"  [add]  {key}=0.10 ₽/CH")


def main():
    with SessionLocal() as db:
        upsert_model_pricing(db)
        upsert_packages(db)
        ensure_ch_rate(db)
    print("\n✅ Тарификация обновлена")
    print("   1 CH = 0.10 ₽ (розничная цена)")
    print("   Маржа моделей: ~100-250% над себестоимостью API")
    print("   Пакеты: от 0.10 до 0.07 ₽/CH (скидка до 30% за объём)")


if __name__ == "__main__":
    main()
