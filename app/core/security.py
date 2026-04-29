from datetime import datetime, timedelta, timezone
from typing import Any, Union, Tuple
import pyotp
import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from app.core.config import settings
import secrets
import hashlib

ph = PasswordHasher()

def get_password_hash(password: str) -> str:
    return ph.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        ph.verify(hashed_password, plain_password)
        if ph.check_needs_rehash(hashed_password):
            # Normally we'd rehash and save, but returning True is enough for verification
            pass
        return True
    except VerifyMismatchError:
        return False

def create_access_token(subject: Union[str, Any], mfa_completed: bool = False, expires_delta: timedelta = None, session_id: str = None) -> str:
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode = {"exp": expire, "sub": str(subject), "mfa_completed": mfa_completed}
    if session_id:
        to_encode["sid"] = session_id
        
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt

def create_refresh_token() -> Tuple[str, str]:
    """Generates a raw refresh token and its hash to store in DB."""
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    return raw_token, token_hash

def verify_refresh_token_hash(raw_token: str, stored_hash: str) -> bool:
    raw_token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    return secrets.compare_digest(raw_token_hash, stored_hash)

def generate_totp_secret() -> str:
    return pyotp.random_base32()

def verify_totp(secret: str, token: str) -> bool:
    totp = pyotp.TOTP(secret)
    return totp.verify(token)
