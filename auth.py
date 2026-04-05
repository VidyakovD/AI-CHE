"""JWT auth + verification token helpers."""
import os, secrets, string
from datetime import datetime, timedelta
from passlib.context import CryptContext
from jose import JWTError, jwt

SECRET_KEY = os.getenv("JWT_SECRET", secrets.token_hex(32))
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
