import base64
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence, Tuple, Union
import pyotp
import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from app.core.config import settings
import secrets
import hashlib
import hmac
import json

ph = PasswordHasher()

JWT_TIER_SUPERADMIN = "superadmin"
JWT_TIER_ORGANIZATION = "organization"
JWT_TIER_NOKVO_ONE_SETUP = "nokvo_one_setup"

# Product isolation. Org access tokens carry a ``product`` claim so a session can
# be scoped to one product. NOKVO APEX (product_tier="nokvo_apex") → "apex";
# everything else → "nokvo_one". The org deps cross-check this claim against the
# org's product_tier, isolating APEX and Nokvo One sessions from each other.
PRODUCT_APEX = "apex"
PRODUCT_NOKVO_ONE = "nokvo_one"


def org_product_claim(product_tier: str | None) -> str:
    """Map an Organization.product_tier to its JWT ``product`` isolation tag."""
    return PRODUCT_APEX if (product_tier or "") == "nokvo_apex" else PRODUCT_NOKVO_ONE

_JWT_TIER_ENV = {
    JWT_TIER_SUPERADMIN: "SUPERADMIN_JWT_SECRET_KEY",
    JWT_TIER_ORGANIZATION: "ORGANIZATION_JWT_SECRET_KEY",
    JWT_TIER_NOKVO_ONE_SETUP: "NOKVO_ONE_SETUP_JWT_SECRET_KEY",
}

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


def _derive_secret(label: str) -> str:
    """Derive isolated HMAC keys when tier-specific env vars are not set."""
    seed = settings.SECRET_KEY.encode("utf-8")
    return hmac.new(seed, f"nokvo.jwt.{label}".encode("utf-8"), hashlib.sha256).hexdigest()


def jwt_secret_for_tier(tier: str) -> str:
    env_name = _JWT_TIER_ENV.get(tier)
    configured = str(getattr(settings, env_name, "") or "").strip() if env_name else ""
    return configured or _derive_secret(tier)


def _infer_token_tier(extra_claims: Mapping[str, Any] | None, data: Mapping[str, Any] | None) -> str:
    claims = {**dict(data or {}), **dict(extra_claims or {})}
    principal_type = claims.get("principal_type")
    if principal_type == "organization_user":
        return JWT_TIER_ORGANIZATION
    if principal_type == "nokvo_one_signup":
        return JWT_TIER_NOKVO_ONE_SETUP
    return JWT_TIER_SUPERADMIN


def _tier_candidates(expected_tiers: Sequence[str] | str | None) -> list[str]:
    if expected_tiers is None:
        return [JWT_TIER_SUPERADMIN, JWT_TIER_ORGANIZATION]
    if isinstance(expected_tiers, str):
        return [expected_tiers]
    return [str(tier) for tier in expected_tiers]


def _decode_with_secret(token: str, secret: str) -> dict[str, Any]:
    payload = jwt.decode(token, secret, algorithms=[settings.ALGORITHM])
    if not isinstance(payload, dict):
        raise jwt.InvalidTokenError("JWT payload must be an object")
    return payload


def decode_access_token(
    token: str,
    *,
    expected_tiers: Sequence[str] | str | None = None,
    allow_legacy_secret: bool | None = None,
) -> dict[str, Any]:
    tiers = _tier_candidates(expected_tiers)
    last_error: Exception | None = None
    for tier in tiers:
        try:
            payload = _decode_with_secret(token, jwt_secret_for_tier(tier))
        except jwt.PyJWTError as exc:
            last_error = exc
            continue
        token_tier = payload.get("token_tier")
        if token_tier and token_tier not in tiers:
            raise jwt.InvalidTokenError("JWT tier does not match this endpoint")
        return payload

    legacy_allowed = settings.JWT_LEGACY_SECRET_FALLBACK if allow_legacy_secret is None else allow_legacy_secret
    if legacy_allowed:
        try:
            payload = _decode_with_secret(token, settings.SECRET_KEY)
        except jwt.PyJWTError as exc:
            last_error = exc
        else:
            token_tier = payload.get("token_tier")
            if token_tier and token_tier not in tiers:
                raise jwt.InvalidTokenError("JWT tier does not match this endpoint")
            return payload

    raise jwt.InvalidTokenError("Could not validate credentials") from last_error

def create_access_token(
    subject: Union[str, Any, None] = None,
    mfa_completed: bool = False,
    expires_delta: timedelta = None,
    session_id: str = None,
    data: Mapping[str, Any] | None = None,
    extra_claims: Mapping[str, Any] | None = None,
    token_tier: str | None = None,
) -> str:
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode = dict(data or {})
    if subject is not None and "sub" not in to_encode:
        to_encode["sub"] = str(subject)
    if "sub" not in to_encode:
        raise ValueError("Token subject is required")
    to_encode.setdefault("mfa_completed", mfa_completed)
    to_encode["exp"] = expire
    if session_id:
        to_encode["sid"] = session_id
    if extra_claims:
        to_encode.update(extra_claims)
    tier = token_tier or _infer_token_tier(extra_claims, data)
    to_encode.setdefault("token_tier", tier)

    encoded_jwt = jwt.encode(to_encode, jwt_secret_for_tier(tier), algorithm=settings.ALGORITHM)
    return encoded_jwt


def create_setup_token(data: Mapping[str, Any], *, expires_delta: timedelta) -> str:
    payload = dict(data)
    payload.setdefault("principal_type", "nokvo_one_signup")
    payload.setdefault("token_tier", JWT_TIER_NOKVO_ONE_SETUP)
    payload["exp"] = datetime.now(timezone.utc) + expires_delta
    return jwt.encode(payload, jwt_secret_for_tier(JWT_TIER_NOKVO_ONE_SETUP), algorithm=settings.ALGORITHM)


def decode_setup_token(token: str) -> dict[str, Any]:
    return decode_access_token(token, expected_tiers=[JWT_TIER_NOKVO_ONE_SETUP])


def _state_secret() -> bytes:
    configured = (settings.OAUTH_STATE_SECRET_KEY or "").strip()
    return (configured or _derive_secret("oauth_state")).encode("utf-8")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("utf-8"))


def create_oauth_state(
    payload: Mapping[str, Any],
    *,
    purpose: str,
    ttl: timedelta = timedelta(minutes=20),
) -> str:
    body = dict(payload)
    body["purpose"] = purpose
    body["exp"] = int((datetime.now(timezone.utc) + ttl).timestamp())
    body.setdefault("nonce", secrets.token_urlsafe(12))
    payload_bytes = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(_state_secret(), payload_bytes, hashlib.sha256).hexdigest()
    return f"{_b64url_encode(payload_bytes)}.{signature}"


def decode_oauth_state(state: str, *, expected_purpose: str | None = None) -> dict[str, Any]:
    try:
        encoded_payload, signature = state.split(".", 1)
        payload_bytes = _b64url_decode(encoded_payload)
    except Exception as exc:
        raise jwt.InvalidTokenError("Invalid OAuth state") from exc
    expected_signature = hmac.new(_state_secret(), payload_bytes, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        raise jwt.InvalidTokenError("Invalid OAuth state signature")
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise jwt.InvalidTokenError("Invalid OAuth state payload") from exc
    if int(payload.get("exp", 0)) < int(datetime.now(timezone.utc).timestamp()):
        raise jwt.ExpiredSignatureError("OAuth state expired")
    if expected_purpose is not None and payload.get("purpose") != expected_purpose:
        raise jwt.InvalidTokenError("OAuth state purpose does not match")
    return payload

def create_refresh_token() -> Tuple[str, str]:
    """Generates a raw refresh token and its hash to store in DB."""
    raw_token = secrets.token_urlsafe(32)
    token_hash = hash_refresh_token(raw_token)
    return raw_token, token_hash


def hash_refresh_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

def verify_refresh_token_hash(raw_token: str, stored_hash: str) -> bool:
    raw_token_hash = hash_refresh_token(raw_token)
    return secrets.compare_digest(raw_token_hash, stored_hash)

def generate_totp_secret() -> str:
    return pyotp.random_base32()

def verify_totp(secret: str, token: str) -> bool:
    totp = pyotp.TOTP(secret)
    return totp.verify(token)
