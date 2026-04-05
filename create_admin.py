"""
Запусти один раз: python create_admin.py
Создаёт администратора Vidyakov с паролем 28371988
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from db import SessionLocal, engine
import models
from auth import hash_password
import uuid

models.Base.metadata.create_all(bind=engine)

db = SessionLocal()

email = "vidyakov@obsidian.ai"
existing = db.query(models.User).filter_by(email=email).first()

if existing:
    existing.password_hash = hash_password("28371988")
    existing.is_verified = True
    existing.is_active = True
    db.commit()
    print(f"✅ Пароль обновлён для {email}")
else:
    user = models.User(
        email=email,
        password_hash=hash_password("28371988"),
        name="Vidyakov",
        tokens_balance=999_999_999,
        is_active=True,
        is_verified=True,
        agreed_to_terms=True,
        referral_code=uuid.uuid4().hex[:8].upper(),
    )
    db.add(user)
    db.commit()
    print(f"✅ Админ создан: {email} / 28371988")

# Убедимся что email в ADMIN_EMAILS
env_path = ".env"
if os.path.exists(env_path):
    lines = open(env_path).readlines()
    has_admin = any("ADMIN_EMAILS" in l for l in lines)
    if not has_admin:
        with open(env_path, "a") as f:
            f.write(f"\nADMIN_EMAILS={email}\n")
        print(f"✅ Добавлен ADMIN_EMAILS={email} в .env")
    else:
        # append to existing
        new_lines = []
        for l in lines:
            if l.startswith("ADMIN_EMAILS=") and email not in l:
                l = l.rstrip() + f",{email}\n"
            new_lines.append(l)
        open(env_path,"w").writelines(new_lines)
        print(f"✅ ADMIN_EMAILS обновлён в .env")
else:
    with open(env_path, "w") as f:
        f.write(f"ADMIN_EMAILS={email}\n")
    print(f"✅ Создан .env с ADMIN_EMAILS={email}")

db.close()
print(f"\n🔑 Логин: {email}")
print(f"🔑 Пароль: 28371988")
