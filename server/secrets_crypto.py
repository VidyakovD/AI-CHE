"""
Симметричное шифрование секретов в БД (IMAP пароли, OAuth client_secret).
Ключ выводится из JWT_SECRET через HKDF — отдельный ключ хранить не нужно.

Хранение обратно-совместимое:
  - enc:<base64> → шифр
  - всё остальное → plaintext (legacy)
"""
import base64
import hashlib
import os
from cryptography.fernet import Fernet, InvalidToken

_PREFIX = "enc:"
_fernet: Fernet | None = None


def _get_fernet() -> Fernet | None:
    global _fernet
    if _fernet is not None:
        return _fernet
    base = os.getenv("JWT_SECRET", "")
    if not base:
        return None
    # 32-байтный ключ из JWT_SECRET (HKDF-Extract упрощённый)
    key_raw = hashlib.sha256(b"secrets-crypto:" + base.encode()).digest()
    key_b64 = base64.urlsafe_b64encode(key_raw)
    _fernet = Fernet(key_b64)
    return _fernet


def encrypt(plaintext: str) -> str:
    """Шифрует. При отсутствии JWT_SECRET возвращает plaintext (с warning в логах)."""
    if not plaintext:
        return plaintext
    f = _get_fernet()
    if f is None:
        import logging
        logging.getLogger(__name__).warning(
            "secrets_crypto: JWT_SECRET не задан, секреты хранятся в открытом виде"
        )
        return plaintext
    token = f.encrypt(plaintext.encode("utf-8")).decode("ascii")
    return _PREFIX + token


def decrypt(value: str) -> str:
    """Расшифровывает. Если значение без префикса (legacy plaintext) — возвращает как есть."""
    if not value or not value.startswith(_PREFIX):
        return value or ""
    f = _get_fernet()
    if f is None:
        return ""
    try:
        return f.decrypt(value[len(_PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken:
        return ""
