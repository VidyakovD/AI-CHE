"""JWT auth + verification token helpers."""
import os, secrets, string
from datetime import datetime, timedelta
from passlib.context import CryptContext
from jose import JWTError, jwt

def _get_jwt_secret() -> str:
    """Стабильный JWT-секрет: из env или сохранённый файл (генерируется один раз)."""
    env_secret = os.getenv("JWT_SECRET")
    if env_secret:
        return env_secret
    secret_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".jwt_secret")
    if os.path.exists(secret_path):
        with open(secret_path) as f:
            return f.read().strip()
    new_secret = secrets.token_hex(32)
    try:
        with open(secret_path, "w") as f:
            f.write(new_secret)
    except Exception:
        pass
    return new_secret


SECRET_KEY = _get_jwt_secret()
ALGORITHM  = "HS256"
ACCESS_TTL  = 60 * 24        # 1 day in minutes (short-lived access token)
REFRESH_TTL = 60 * 24 * 30   # 30 days in minutes (long-lived refresh token)
JWT_ISS     = os.getenv("JWT_ISS", "aiche")
JWT_AUD     = os.getenv("JWT_AUD", "aiche-web")

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    return pwd_ctx.hash(plain)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_ctx.verify(plain, hashed)

def create_token(user_id: int, email: str) -> str:
    """Create short-lived access token (1 day)."""
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TTL)
    return jwt.encode(
        {"sub": str(user_id), "email": email, "exp": expire, "type": "access",
         "iss": JWT_ISS, "aud": JWT_AUD},
        SECRET_KEY, algorithm=ALGORITHM
    )

def create_refresh_token(user_id: int, email: str) -> str:
    """Create long-lived refresh token (30 days)."""
    expire = datetime.utcnow() + timedelta(minutes=REFRESH_TTL)
    return jwt.encode(
        {"sub": str(user_id), "email": email, "exp": expire, "type": "refresh",
         "iss": JWT_ISS, "aud": JWT_AUD},
        SECRET_KEY, algorithm=ALGORITHM
    )

def decode_token(token: str, require_type: str = None) -> dict | None:
    try:
        # Допускаем legacy-токены без aud/iss (были выданы до добавления claims).
        # Но если claim присутствует — проверяем строго (не «мягкая» проверка).
        # Явно фиксируем разрешённые алгоритмы — защита от alg:none и HS/RS confusion.
        payload = jwt.decode(
            token, SECRET_KEY, algorithms=[ALGORITHM],
            options={"verify_aud": False, "verify_iss": False},
        )
        # Токены выданные после фикса всегда имеют aud+iss — проверяем
        if payload.get("aud") is not None and payload["aud"] != JWT_AUD:
            return None
        if payload.get("iss") is not None and payload["iss"] != JWT_ISS:
            return None
        if require_type and payload.get("type") != require_type:
            return None
        return payload
    except JWTError:
        return None

def generate_code(length: int = 6) -> str:
    """Generate numeric verification code."""
    return "".join(secrets.choice(string.digits) for _ in range(length))

VERIFY_TTL_MINUTES = 15
