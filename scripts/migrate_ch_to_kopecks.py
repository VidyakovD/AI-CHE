"""
Одноразовая миграция: «CH (внутренняя валюта)» → «копейки».

Контракт: 1 CH стоил 0.10 ₽ = 10 копеек. Поэтому везде, где раньше хранилось
число CH, теперь должно быть × 10 (= те же деньги, но в копейках).

Идемпотентность гарантируется через флаг `migrated_ch_to_kopecks` в pricing_settings.
Запуск: python -m scripts.migrate_ch_to_kopecks
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.db import SessionLocal
from server.models import (
    User, Transaction, ModelPricing, UsageLog, TokenPackage, Solution,
    PricingSetting,
)


FLAG_KEY = "migrated_ch_to_kopecks"


def main():
    db = SessionLocal()
    try:
        flag = db.query(PricingSetting).filter_by(key=FLAG_KEY).first()
        if flag and flag.value == "1":
            print("⏭  Уже мигрировано (флаг migrated_ch_to_kopecks=1). Пропускаю.")
            return

        # ── Users.tokens_balance × 10 ───────────────────────────────────────
        users_count = 0
        for u in db.query(User).all():
            u.tokens_balance = int((u.tokens_balance or 0) * 10)
            # low_balance_threshold тоже в CH → копейки
            if u.low_balance_threshold:
                u.low_balance_threshold = int(u.low_balance_threshold * 10)
            users_count += 1
        print(f"✓ Users: обновлено {users_count} (балансы и пороги × 10)")

        # ── Transaction.tokens_delta × 10 ──────────────────────────────────
        # ВАЖНО: НЕ трогаем transactions.amount_rub — это сумма в рублях
        tx_count = db.query(Transaction).count()
        db.execute(__import__("sqlalchemy").text(
            "UPDATE transactions SET tokens_delta = tokens_delta * 10"
        ))
        print(f"✓ Transactions: tokens_delta × 10 для {tx_count} строк")

        # ── ModelPricing × 10 ──────────────────────────────────────────────
        mp_count = 0
        for p in db.query(ModelPricing).all():
            p.ch_per_1k_input = (p.ch_per_1k_input or 0) * 10
            p.ch_per_1k_output = (p.ch_per_1k_output or 0) * 10
            p.cost_per_req = int((p.cost_per_req or 0) * 10)
            p.min_ch_per_req = int((p.min_ch_per_req or 0) * 10)
            mp_count += 1
        print(f"✓ ModelPricing: тарифы × 10 для {mp_count} строк")

        # ── UsageLog.ch_charged × 10 ───────────────────────────────────────
        ul_count = db.query(UsageLog).count()
        db.execute(__import__("sqlalchemy").text(
            "UPDATE usage_logs SET ch_charged = ch_charged * 10"
        ))
        print(f"✓ UsageLog: ch_charged × 10 для {ul_count} строк")

        # ── TokenPackage.tokens × 10 ───────────────────────────────────────
        # tokens теперь = копейки, начисляемые за price_rub
        tp_count = db.query(TokenPackage).count()
        db.execute(__import__("sqlalchemy").text(
            "UPDATE token_packages SET tokens = tokens * 10"
        ))
        print(f"✓ TokenPackage: tokens × 10 для {tp_count} строк")

        # ── Solution.price_tokens × 10 ─────────────────────────────────────
        sol_count = db.query(Solution).count()
        db.execute(__import__("sqlalchemy").text(
            "UPDATE solutions SET price_tokens = price_tokens * 10"
        ))
        print(f"✓ Solutions: price_tokens × 10 для {sol_count} строк")

        # ── Ставим флаг идемпотентности ────────────────────────────────────
        if flag:
            flag.value = "1"
        else:
            db.add(PricingSetting(key=FLAG_KEY, value="1"))

        db.commit()
        print("\n✅ Миграция завершена.")
    except Exception as e:
        db.rollback()
        print(f"❌ Ошибка: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
