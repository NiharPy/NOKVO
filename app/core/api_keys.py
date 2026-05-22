"""Nokvo Connect API key minting + verification.

Format
======
    nk_{mode}_{prefix}{secret}
       │      │       └─ argon2-hashed in DB. Returned to the caller exactly
       │      │          once when the key is minted.
       │      └─ 6 chars of base62. Persisted as ``key_prefix`` for indexed
       │         lookup; safe to show in dashboards.
       └─ "live" or "test". Lets sandbox keys exist alongside production ones
          without colliding billing or rate-limit buckets.

The full key the client passes back is ``nk_{mode}_{prefix}{secret}`` so we
extract ``mode`` + the 6-char prefix, do a single primary-key lookup on
``organization_api_keys.key_prefix``, then argon2-verify the rest of the
string against ``secret_hash``. No timing-oracle on the prefix because it's
publicly visible by design.
"""
from __future__ import annotations

import re
import secrets
import string
from dataclasses import dataclass
from typing import Literal

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError


_ALPHABET = string.ascii_letters + string.digits  # base62
_PREFIX_LEN = 6
_SECRET_LEN = 36
_KEY_RE = re.compile(rf"^nk_(?P<mode>live|test)_(?P<prefix>[A-Za-z0-9]{{{_PREFIX_LEN}}})(?P<secret>[A-Za-z0-9]{{{_SECRET_LEN}}})$")

_hasher = PasswordHasher()


@dataclass
class MintedApiKey:
    raw: str
    """Full ``nk_..._...`` string. Show to the user once, never re-derive."""

    key_prefix: str
    """Public prefix (``nk_{mode}_{6chars}``). Safe to display in dashboards."""

    secret_hash: str
    """Argon2 hash of the secret half. Persist; treat as a password digest."""

    mode: Literal["live", "test"]


def mint(mode: Literal["live", "test"] = "live") -> MintedApiKey:
    prefix = "".join(secrets.choice(_ALPHABET) for _ in range(_PREFIX_LEN))
    secret = "".join(secrets.choice(_ALPHABET) for _ in range(_SECRET_LEN))
    raw = f"nk_{mode}_{prefix}{secret}"
    return MintedApiKey(
        raw=raw,
        key_prefix=f"nk_{mode}_{prefix}",
        secret_hash=_hasher.hash(secret),
        mode=mode,
    )


@dataclass
class ParsedApiKey:
    mode: Literal["live", "test"]
    key_prefix: str
    secret: str


def parse(raw: str | None) -> ParsedApiKey | None:
    """Validate the shape and split the raw key into (mode, prefix, secret).
    Returns ``None`` for malformed values — callers should treat that the
    same as an unknown key (404/401) rather than surfacing a parse error."""
    if not raw:
        return None
    match = _KEY_RE.match(raw.strip())
    if not match:
        return None
    return ParsedApiKey(
        mode=match.group("mode"),  # type: ignore[arg-type]
        key_prefix=f"nk_{match.group('mode')}_{match.group('prefix')}",
        secret=match.group("secret"),
    )


def verify(parsed: ParsedApiKey, secret_hash: str) -> bool:
    try:
        return _hasher.verify(secret_hash, parsed.secret)
    except VerifyMismatchError:
        return False


def mint_webhook_secret() -> tuple[str, str]:
    """Symmetric webhook signing secret. Returns ``(raw, hash)``. The raw is
    the value the caller stores client-side to verify ``X-Nokvo-Signature``;
    the hash is what we persist so we can rotate without re-hashing on every
    signature check (argon2 is too slow for that — the *signing* path uses the
    raw value held in memory at delivery time)."""
    raw = "whsec_" + "".join(secrets.choice(_ALPHABET) for _ in range(32))
    return raw, _hasher.hash(raw)
