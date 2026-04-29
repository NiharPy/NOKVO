from pydantic import BaseModel, EmailStr
from uuid import UUID
from datetime import datetime
from typing import Optional

class SuperAdminUserBase(BaseModel):
    email: EmailStr
    full_name: Optional[str] = None

class SuperAdminUserCreate(SuperAdminUserBase):
    password: str
    role: str = "founder"

class SuperAdminUserResponse(SuperAdminUserBase):
    id: UUID
    role: str
    status: str
    mfa_required: bool
    webauthn_enabled: bool
    last_login_at: Optional[datetime]
    created_at: datetime

    class Config:
        from_attributes = True

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class TOTPVerifyRequest(BaseModel):
    token: str
