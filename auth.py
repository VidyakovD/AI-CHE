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
ACCESS_TTL = 60 * 24 * 30   # 30 days in minutes

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    return pwd_ctx.hash(plain)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_ctx.verify(plain, hashed)

def create_token(user_id: int, email: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TTL)
    return jwt.encode(
        {"sub": str(user_id), "email": email, "exp": expire},
        SECRET_KEY, algorithm=ALGORITHM
    )

def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None

def generate_code(length: int = 6) -> str:
    """Generate numeric verification code."""
    return "".join(secrets.choice(string.digits) for _ in range(length))

VERIFY_TTL_MINUTES = 15
