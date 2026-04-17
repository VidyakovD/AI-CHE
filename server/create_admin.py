"""
Запусти один раз: python -m server.create_admin
Создаёт администратора. Пароль берётся из env ADMIN_PASSWORD или вводится интерактивно.
"""
import sys, os, secrets

from dotenv import load_dotenv
load_dotenv()

from server.db import SessionLocal, engine
from server import models
from server.auth import hash_password
import uuid

models.Base.metadata.create_all(bind=engine)

db = SessionLocal()

email = os.getenv("ADMIN_EMAIL", "vidyakov@obsidian.ai")
password = os.getenv("ADMIN_PASSWORD")
if not password:
    import getpass
    password = getpass.getpass(f"Введите пароль для {email}: ").strip()
    if not password:
        password = secrets.token_urlsafe(16)
        print(f"⚠️ Пароль сгенерирован автоматически: {password}")

if len(password) < 8:
    print("❌ Пароль должен быть не менее 8 символов")
    db.close()
    sys.exit(1)

existing = db.query(models.User).filter_by(email=email).first()

if existing:
    existing.password_hash = hash_password(password)
    existing.is_verified = True
    existing.is_active = True
    db.commit()
    print(f"✅ Пароль обновлён для {email}")
else:
    user = models.User(
        email=email,
        password_hash=hash_password(password),
        name="Admin",
        tokens_balance=999_999_999,
        is_active=True,
        is_verified=True,
        agreed_to_terms=True,
        referral_code=uuid.uuid4().hex[:8].upper(),
    )
    db.add(user)
    db.commit()
    print(f"✅ Админ создан: {email}")

# Убедимся что email в ADMIN_EMAILS
env_path = ".env"
if os.path.exists(env_path):
    with open(env_path, "r") as f:
        lines = f.readlines()
    has_admin = any("ADMIN_EMAILS" in l for l in lines)
    if not has_admin:
        with open(env_path, "a") as f:
            f.write(f"\nADMIN_EMAILS={email}\n")
        print(f"✅ Добавлен ADMIN_EMAILS={email} в .env")
    else:
        new_lines = []
        for l in lines:
            if l.startswith("ADMIN_EMAILS=") and email not in l:
                l = l.rstrip() + f",{email}\n"
            new_lines.append(l)
        with open(env_path, "w") as f:
            f.writelines(new_lines)
        print(f"✅ ADMIN_EMAILS обновлён в .env")
else:
    with open(env_path, "w") as f:
        f.write(f"ADMIN_EMAILS={email}\n")
    print(f"✅ Создан .env с ADMIN_EMAILS={email}")

db.close()
print(f"\n🔑 Логин: {email}")
