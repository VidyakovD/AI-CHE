#!/usr/bin/env python3
"""
Расшифровка зашифрованного бэкапа chat.db.

Использование:
    python scripts/restore_backup.py backups/chat.db.2026-05-01.enc chat.db.restored

Требует ключ. Приоритет:
  1. ENV BACKUP_ENCRYPTION_KEY (hex или base64, 32 байта)
  2. Файл .backup_encryption_key в корне проекта

Формат файла: 12 байт nonce || ciphertext || 16 байт auth tag (AES-256-GCM,
associated_data=b"aiche-db-backup-v1").
"""
import sys, os, base64
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def load_key() -> bytes:
    env_key = os.getenv("BACKUP_ENCRYPTION_KEY", "").strip()
    if env_key:
        if len(env_key) == 64 and all(c in "0123456789abcdefABCDEF" for c in env_key):
            return bytes.fromhex(env_key)
        return base64.b64decode(env_key)
    # Файловый ключ — ищем относительно scripts/
    here = os.path.dirname(os.path.abspath(__file__))
    key_path = os.path.join(os.path.dirname(here), ".backup_encryption_key")
    if not os.path.exists(key_path):
        sys.exit(f"❌ Ключ не найден: ни в env BACKUP_ENCRYPTION_KEY, ни в {key_path}")
    with open(key_path, "rb") as f:
        k = f.read().strip()
    if len(k) == 64:
        return bytes.fromhex(k.decode("ascii"))
    if len(k) == 32:
        return k
    return base64.b64decode(k)


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(f"Usage: {sys.argv[0]} <encrypted_backup> <output_db>")
    src, dst = sys.argv[1], sys.argv[2]
    if not os.path.exists(src):
        sys.exit(f"❌ Файл не найден: {src}")
    if os.path.exists(dst):
        sys.exit(f"❌ Целевой файл уже существует: {dst} (защита от перезаписи)")
    key = load_key()
    if len(key) != 32:
        sys.exit(f"❌ Ключ не 32 байта ({len(key)})")
    with open(src, "rb") as f:
        blob = f.read()
    if len(blob) < 12 + 16:
        sys.exit("❌ Файл слишком короткий — возможно повреждён")
    nonce = blob[:12]
    ciphertext = blob[12:]
    aesgcm = AESGCM(key)
    try:
        plaintext = aesgcm.decrypt(nonce, ciphertext,
                                    associated_data=b"aiche-db-backup-v1")
    except Exception as e:
        sys.exit(f"❌ Расшифровка не удалась (неверный ключ или повреждение): {e}")
    with open(dst, "wb") as f:
        f.write(plaintext)
    print(f"✅ Восстановлено: {dst} ({len(plaintext) / 1024 / 1024:.1f} MB)")
    # Проверка SQLite-целостности
    try:
        import sqlite3
        conn = sqlite3.connect(dst)
        ok = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        if ok and ok[0] == "ok":
            print("✅ SQLite integrity_check: ok")
        else:
            print(f"⚠ integrity_check: {ok}")
    except Exception as e:
        print(f"⚠ integrity_check вызвал ошибку: {e}")


if __name__ == "__main__":
    main()
