"""Create or reset a SuperAdmin account (idempotent by email).

Reads the account details and DB/crypto config from the environment — no
secrets are hard-coded. To target PRODUCTION, point it at prod's env file:

    DOTENV_FILE=.env.prod \
    SUPERADMIN_EMAIL=you@example.com \
    SUPERADMIN_PASSWORD='…' \
    SUPERADMIN_MFA=1 \
    venv/bin/python app/scripts/create_superadmin.py

When SUPERADMIN_MFA=1 it generates a TOTP secret, stores it encrypted with the
SAME key the app uses (so prod can verify it), and prints the enrolment secret +
otpauth URI to add to an authenticator app. Re-running for an existing email
updates the password / MFA in place.
"""
from __future__ import annotations

import os

# Load the chosen env file BEFORE importing any app module, so pydantic settings
# (DB URL + SECRET_KEY-derived crypto) read the right environment.
_dotenv_file = os.environ.get("DOTENV_FILE")
if _dotenv_file:
    from dotenv import load_dotenv

    load_dotenv(_dotenv_file, override=True)

import asyncio  # noqa: E402

import pyotp  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.core.security import get_password_hash  # noqa: E402
from app.core.totp_crypto import encrypt_totp_secret  # noqa: E402
from app.db.session import AsyncSessionLocal  # noqa: E402
from app.models.user import SuperAdminUser  # noqa: E402


async def main() -> None:
    email = os.environ["SUPERADMIN_EMAIL"].strip()
    password = os.environ["SUPERADMIN_PASSWORD"]
    full_name = os.environ.get("SUPERADMIN_FULLNAME", "").strip() or None
    role = os.environ.get("SUPERADMIN_ROLE", "founder").strip()
    enable_mfa = os.environ.get("SUPERADMIN_MFA", "1").strip() not in ("", "0", "false", "False")

    secret = pyotp.random_base32() if enable_mfa else None

    async with AsyncSessionLocal() as db:
        user = (
            await db.execute(select(SuperAdminUser).where(SuperAdminUser.email == email))
        ).scalars().first()
        action = "Updated" if user else "Created"
        if user is None:
            user = SuperAdminUser(email=email)
            db.add(user)
        user.password_hash = get_password_hash(password)
        if full_name:
            user.full_name = full_name
        user.role = role
        user.status = "active"
        user.mfa_required = enable_mfa
        if enable_mfa:
            user.totp_secret_encrypted = encrypt_totp_secret(secret)
        else:
            user.totp_secret_encrypted = None
        await db.commit()

    print(f"{action} superadmin: {email}  (role={role}, status=active, mfa={'on' if enable_mfa else 'off'})")
    if enable_mfa:
        uri = pyotp.totp.TOTP(secret).provisioning_uri(name=email, issuer_name="NOKVO SuperAdmin")
        print("\n=== MFA ENROLMENT (add to your authenticator app) ===")
        print(f"TOTP secret : {secret}")
        print(f"otpauth URI : {uri}")
        print("Scan the URI as a QR (or type the secret) in Google Authenticator/Authy, then")
        print("enter the 6-digit code at login.")


if __name__ == "__main__":
    asyncio.run(main())
