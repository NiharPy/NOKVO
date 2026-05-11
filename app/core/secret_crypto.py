"""Symmetric (Fernet) encryption for at-rest secrets.

Used for any sensitive string we store in Postgres without a Key Vault dependency —
TOTP secrets and per-tenant Azure OpenAI API keys are the current callers.

Key derivation:
  - If NOKVO_TOTP_ENCRYPTION_KEY is set, that string is the seed.
  - Else SECRET_KEY (used by JWT signing) is the seed.
The seed is SHA-256'd and base64url-encoded to produce the Fernet key, which means
rotating SECRET_KEY without explicitly setting NOKVO_TOTP_ENCRYPTION_KEY will
invalidate all stored ciphertexts. Set NOKVO_TOTP_ENCRYPTION_KEY explicitly to
decouple the two.
"""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


class SecretDecryptionError(Exception):
    pass


def _derive_fernet_key() -> bytes:
    raw = os.environ.get("NOKVO_TOTP_ENCRYPTION_KEY") or settings.SECRET_KEY
    if not raw:
        raise RuntimeError(
            "NOKVO_TOTP_ENCRYPTION_KEY or SECRET_KEY must be set to encrypt at-rest secrets"
        )
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


_FERNET = Fernet(_derive_fernet_key())


def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        raise ValueError("Secret cannot be empty")
    return _FERNET.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(ciphertext: str) -> str:
    try:
        return _FERNET.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise SecretDecryptionError("Stored secret could not be decrypted") from exc
