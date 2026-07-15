"""NOKVO affiliate accounts (referral program).

An affiliate signs up on the public /affiliate page (name, 18+ date of birth,
email) and secures the account with TOTP — login is affiliate number +
authenticator code, no password. The ``affiliate_number`` is the public
referral code an APEX customer enters at payment; commissions accrue in
:class:`app.models.affiliate_commission.AffiliateCommission`.

``status`` lifecycle: ``pending_totp`` (signed up, QR not yet verified — the
signup reclaim path may overwrite this row) → ``active`` (code works, can log
in) → ``suspended`` (operator action: login blocked, NEW accrual and payouts
stop, ledger kept). Settlement eligibility is DERIVED, never stored:
``kyc_verified_at IS NOT NULL AND bank details complete AND status='active'``.
"""
from __future__ import annotations

import uuid

from sqlalchemy import Column, Date, DateTime, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.db.session import Base


class Affiliate(Base):
    __tablename__ = "affiliates"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # The public referral code AND login identifier: "NKV" + 7 chars from an
    # unambiguous alphabet (no I/O/0/1). Generated server-side, never chosen.
    affiliate_number = Column(String, nullable=False, unique=True, index=True)
    full_name = Column(String, nullable=False)
    # Server-validated 18+ at signup (against IST "today" — Indian market).
    date_of_birth = Column(Date, nullable=False)
    # Contact + dedup only — NOT a login identifier. Normalized lowercase.
    email = Column(String, nullable=False, unique=True, index=True)
    # Fernet-encrypted TOTP secret (app.core.secret_crypto) — same column name
    # convention as OrganizationUser. Rotated by superadmin reset-totp.
    totp_secret_encrypted_v2 = Column(String, nullable=True)
    status = Column(String, nullable=False, server_default="pending_totp", default="pending_totp")
    # Operator approval of the account (superadmin console) — the payout gate.
    # No document is collected; the operator verifies identity out-of-band
    # (e.g. bank account-holder name match). Cleared on signup reclaim since
    # the identity fields may have changed. Payouts are blocked until verified.
    kyc_verified_at = Column(DateTime(timezone=True), nullable=True)
    kyc_verified_by = Column(String, nullable=True)
    # Payout destination (operator does manual NEFT/IMPS and records the UTR).
    bank_account_holder = Column(String, nullable=True)
    bank_account_number = Column(String, nullable=True)
    bank_ifsc = Column(String, nullable=True)
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
