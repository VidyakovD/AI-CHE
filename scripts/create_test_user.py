"""Создать тестового юзера для внешней проверки (например ЮKassa-аудита).

Юзер создаётся:
  - is_verified=True (email подтверждён)
  - agreed_to_terms=True
  - tokens_balance = передаётся через --balance (в копейках)
  - В Transaction-логе — bonus-запись для аудит-трейла

Использование на проде:
  ssh -i 'C:\\Users\\Денис\\.ssh\\id_ed25519' root@193.187.92.147 \\
    "cd /root/AI-CHE && /root/AI-CHE/venv/bin/python scripts/create_test_user.py \\
      --email yookassa-test@aiche.ru --password 'StrongPass123!' --balance 100000 \\
      --comment 'YooKassa live-shop audit 2026-05-17'"

После одобрения live-shop — удалить юзера через /admin/users или:
  python scripts/create_test_user.py --delete --email yookassa-test@aiche.ru
"""
from __future__ import annotations

import argparse
import os
import sys
import secrets
import string

# Подняться из scripts/ к корню проекта
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _gen_password(length: int = 16) -> str:
    """Криптостойкий пароль: буквы + цифры + спецсимволы, без неоднозначных."""
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    # Исключаем символы которые легко спутать (0/O, 1/l/I)
    chars = chars.translate(str.maketrans("", "", "0Ol1I"))
    return "".join(secrets.choice(chars) for _ in range(length))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True, help="Email тестового юзера")
    parser.add_argument("--password", default=None,
                        help="Пароль (если не указан — сгенерируем случайный)")
    parser.add_argument("--balance", type=int, default=100_000,
                        help="Стартовый баланс в копейках (default 100000 = 1000 ₽)")
    parser.add_argument("--name", default=None, help="Имя (default = local-part email)")
    parser.add_argument("--comment", default="External test account",
                        help="Описание в Transaction-логе")
    parser.add_argument("--delete", action="store_true",
                        help="Удалить юзера с этим email (вместо создания)")
    args = parser.parse_args()

    # Импорты после sys.path.insert, чтобы python видел server/
    from server.db import db_session
    from server.models import User, Transaction
    from server.auth import hash_password

    email = args.email.strip().lower()
    if "@" not in email:
        print(f"❌ Неправильный email: {email}")
        sys.exit(2)

    with db_session() as db:
        existing = db.query(User).filter_by(email=email).first()

        if args.delete:
            if not existing:
                print(f"⚠ Юзер {email} не найден — нечего удалять")
                sys.exit(0)
            uid = existing.id
            # Soft-delete: занулим email/password чтобы не мешали, сохраним
            # для audit-trail. Жёсткое delete рискованно (FK на Transactions/etc).
            existing.email = f"_deleted_{uid}_{email}"
            existing.password_hash = "_REVOKED_"
            existing.is_verified = False
            existing.is_banned = True
            db.commit()
            print(f"✅ Юзер {email} (id={uid}) деактивирован "
                  "(email замаскирован, password revoked, is_banned=True)")
            return

        if existing:
            print(f"⚠ Юзер {email} уже существует (id={existing.id}). "
                  "Используйте --delete сначала, либо смените --email.")
            sys.exit(3)

        password = args.password or _gen_password()
        local_part = email.split("@", 1)[0]
        name = args.name or local_part

        user = User(
            email=email,
            password_hash=hash_password(password),
            name=name,
            tokens_balance=int(args.balance),
            agreed_to_terms=True,
            is_verified=True,
            marketing_consent=False,
        )
        db.add(user); db.commit(); db.refresh(user)

        # Transaction для аудита
        if args.balance > 0:
            db.add(Transaction(
                user_id=user.id,
                type="bonus",
                tokens_delta=int(args.balance),
                description=f"Тестовый баланс: {args.comment}",
            ))
            db.commit()

        # Audit log
        try:
            from server.audit_log import log_action
            log_action(
                "admin.test_user_created",
                user_id=user.id,
                target_type="user",
                target_id=user.id,
                details={"email": email, "balance_kop": args.balance,
                          "comment": args.comment},
            )
        except Exception:
            pass

        print(f"""
✅ Тестовый юзер создан:
   ID:       {user.id}
   Email:    {email}
   Пароль:   {password}
   Баланс:   {args.balance} коп. ({args.balance/100:.2f} ₽)
   Подтверждён: ДА (is_verified=True)
   Комментарий: {args.comment}

⚠ ПАРОЛЬ ПОКАЗАН ОДИН РАЗ — сохрани его сейчас.
   После использования удали юзера:
     python scripts/create_test_user.py --delete --email {email}
""")


if __name__ == "__main__":
    main()
