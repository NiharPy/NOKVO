"""Backwards-compatible aliases for TOTP-specific callers.

Real implementation lives in app.core.secret_crypto so other callers (Azure OpenAI
API keys, etc.) can share the same Fernet instance.
"""
from __future__ import annotations

from app.core.secret_crypto import (
    SecretDecryptionError as TOTPDecryptionError,
    decrypt_secret as decrypt_totp_secret,
    encrypt_secret as encrypt_totp_secret,
)


__all__ = ["TOTPDecryptionError", "decrypt_totp_secret", "encrypt_totp_secret"]
