"""
Симметричное шифрование секретов в БД (IMAP пароли, OAuth client_secret).

Ключ выводится из JWT_SECRET через HKDF — отдельный ключ хранить не нужно.
Формат шифртекста: `enc:vN:<base64>` где N — версия ключа (для ротации).

Ротация ключа (например, если JWT_SECRET скомпрометирован):
  1. Старый JWT_SECRET кладём в env LEGACY_JWT_SECRETS (через запятую)
  2. Генерируем новый JWT_SECRET
  3. При расшифровке пробуем сначала текущий ключ, потом legacy
  4. Раз в сутки запускаем re-encrypt (TODO: /admin/reencrypt-secrets)

Обратная совместимость:
  - `enc:<base64>` (без версии) → считаем v1, расшифровываем текущим ключом
  - plaintext (без префикса) → возвращаем как есть (legacy до шифрования)
"""
import base64
import hashlib
import logging
import os
from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)

_PREFIX = "enc:"
_CURRENT_VERSION = "v1"
_fernet_cache: dict[str, Fernet] = {}


def _derive_key(secret: str) -> bytes:
    """Производит 32-байтный Fernet-ключ из secret через SHA-256."""
    key_raw = hashlib.sha256(b"secrets-crypto:" + secret.encode()).digest()
    return base64.urlsafe_b64encode(key_raw)


def _get_fernet(version: str = _CURRENT_VERSION) -> Fernet | None:
    """Возвращает Fernet для указанной версии.
    v1 — текущий JWT_SECRET. Более старые версии ищутся в LEGACY_JWT_SECRETS."""
    if version in _fernet_cache:
        return _fernet_cache[version]
    if version == _CURRENT_VERSION:
        base = os.getenv("JWT_SECRET", "")
        if not base:
            return None
        f = Fernet(_derive_key(base))
        _fernet_cache[version] = f
        return f
    # v0, v-1 и т.п. — legacy
    legacy = [s.strip() for s in os.getenv("LEGACY_JWT_SECRETS", "").split(",") if s.strip()]
    # Пытаемся все legacy-секреты (по индексу) — для v0 берём legacy[0], v-1 → legacy[1], etc.
    return None  # резолв происходит в decrypt() через brute-force


def _all_fernets() -> list[tuple[str, Fernet]]:
    """Текущий ключ + все legacy-ключи для попытки расшифровки."""
    out: list[tuple[str, Fernet]] = []
    cur = _get_fernet(_CURRENT_VERSION)
    if cur is not None:
        out.append((_CURRENT_VERSION, cur))
    legacy_raw = [s.strip() for s in os.getenv("LEGACY_JWT_SECRETS", "").split(",") if s.strip()]
    for i, secret in enumerate(legacy_raw):
        try:
            out.append((f"legacy{i}", Fernet(_derive_key(secret))))
        except Exception:
            pass
    return out


def encrypt(plaintext: str) -> str:
    """Шифрует текущим ключом. Формат: enc:v1:<base64>."""
    if not plaintext:
        return plaintext
    f = _get_fernet(_CURRENT_VERSION)
    if f is None:
        log.warning("secrets_crypto: JWT_SECRET не задан, секреты хранятся в открытом виде")
        return plaintext
    token = f.encrypt(plaintext.encode("utf-8")).decode("ascii")
    return f"{_PREFIX}{_CURRENT_VERSION}:{token}"


def decrypt(value: str) -> str:
    """Расшифровывает. Поддерживает старый формат без версии и legacy-ключи."""
    if not value or not value.startswith(_PREFIX):
        return value or ""
    body = value[len(_PREFIX):]
    # Новый формат: enc:v1:<token>, старый: enc:<token>
    if ":" in body and body.split(":", 1)[0].startswith("v"):
        version, token = body.split(":", 1)
    else:
        version, token = _CURRENT_VERSION, body
    # Пытаемся ключом нужной версии
    f = _get_fernet(version) if version == _CURRENT_VERSION else None
    if f is not None:
        try:
            return f.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken:
            pass
    # Fallback: пробуем все доступные ключи (текущий + legacy)
    for vname, fernet in _all_fernets():
        try:
            return fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken:
            continue
    log.warning(f"secrets_crypto: не удалось расшифровать (версия={version})")
    return ""


def reencrypt(value: str) -> str | None:
    """Расшифровывает старым/любым ключом и шифрует текущим.
    Возвращает None если не удалось расшифровать (ключ утерян)."""
    if not value or not value.startswith(_PREFIX):
        return value
    plain = decrypt(value)
    if plain == "":
        # Возможно, пустое значение или не расшифровалось
        return None
    return encrypt(plain)
