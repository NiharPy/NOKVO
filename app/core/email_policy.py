from __future__ import annotations

from pydantic import EmailStr


PERSONAL_EMAIL_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "yahoo.com",
    "yahoo.co.in",
    "yahoo.co.uk",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "msn.com",
    "icloud.com",
    "me.com",
    "mac.com",
    "aol.com",
    "proton.me",
    "protonmail.com",
    "pm.me",
    "zoho.com",
    "gmx.com",
    "gmx.de",
    "yandex.com",
    "mail.com",
    "rediffmail.com",
}


def normalize_email(email: str | EmailStr) -> str:
    return str(email).strip().lower()


def extract_email_domain(email: str | EmailStr) -> str:
    normalized = normalize_email(email)
    local_part, separator, domain = normalized.partition("@")
    if not separator or not local_part or not domain:
        raise ValueError("A valid email address is required")
    return domain


def is_work_email(email: str | EmailStr) -> bool:
    domain = extract_email_domain(email)
    return domain not in PERSONAL_EMAIL_DOMAINS


def validate_work_email(email: str | EmailStr) -> str:
    """Validate + normalize a signup email. Personal providers (gmail, etc.) are
    NOW ALLOWED — any well-formed email is accepted. (Name + extract_email_domain
    call are kept so the format check still rejects malformed input and every
    schema/API caller needs no change.)"""
    normalized = normalize_email(email)
    extract_email_domain(normalized)  # raises ValueError on a malformed address
    return normalized
