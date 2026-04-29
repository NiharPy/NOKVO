import pytest
import pyotp
import jwt
from datetime import datetime, timedelta, timezone
from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.user import SuperAdminUser
from sqlalchemy import select

pytestmark = pytest.mark.asyncio

async def test_login_success_and_mfa(client):
    # 1. Initial Login
    res = await client.post("/api/auth/login", json={
        "email": "test_superadmin@nokvo.ai",
        "password": "TestPassword123!"
    })
    assert res.status_code == 200
    data = res.json()
    assert "access_token" in data
    assert data["refresh_token"] == "pending_mfa"
    
    temp_token = data["access_token"]
    
    # 2. MFA Setup
    res = await client.post("/api/auth/mfa/totp/setup", headers={"Authorization": f"Bearer {temp_token}"})
    assert res.status_code == 200
    setup_data = res.json()
    assert "secret" in setup_data
    secret = setup_data["secret"]
    
    # 3. MFA Verification (Success)
    totp = pyotp.TOTP(secret)
    valid_code = totp.now()
    
    res = await client.post("/api/auth/mfa/totp/verify", json={"token": valid_code}, headers={"Authorization": f"Bearer {temp_token}"})
    assert res.status_code == 200
    final_data = res.json()
    assert "access_token" in final_data
    assert final_data["refresh_token"] != "pending_mfa"

async def test_wrong_password(client):
    res = await client.post("/api/auth/login", json={
        "email": "test_superadmin@nokvo.ai",
        "password": "WrongPassword!"
    })
    assert res.status_code == 401

async def test_mfa_validation_failure(client):
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(SuperAdminUser).where(SuperAdminUser.email == "test_superadmin@nokvo.ai"))
        u = res.scalars().first()
        u.totp_secret_encrypted = pyotp.random_base32()
        u.status = "active"
        db.add(u)
        await db.commit()
    
    res = await client.post("/api/auth/login", json={"email": "test_superadmin@nokvo.ai", "password": "TestPassword123!"})
    temp_token = res.json()["access_token"]
    
    res = await client.post("/api/auth/mfa/totp/verify", json={"token": "000000"}, headers={"Authorization": f"Bearer {temp_token}"})
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid TOTP token"

async def test_expired_token(client):
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(SuperAdminUser).where(SuperAdminUser.email == "test_superadmin@nokvo.ai"))
        uid = res.scalars().first().id
            
    expire = datetime.now(timezone.utc) - timedelta(minutes=5)
    to_encode = {"exp": expire, "sub": str(uid), "mfa_completed": True}
    expired_token = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    
    res = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {expired_token}"})
    assert res.status_code == 401

async def test_jwt_tampering(client):
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(SuperAdminUser).where(SuperAdminUser.email == "test_superadmin@nokvo.ai"))
        uid = res.scalars().first().id
    
    expire = datetime.now(timezone.utc) + timedelta(minutes=5)
    to_encode = {"exp": expire, "sub": str(uid), "mfa_completed": True}
    valid_token = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    
    header, payload, signature = valid_token.split(".")
    import base64
    import json
    
    decoded_payload = json.loads(base64.urlsafe_b64decode(payload + "==").decode())
    decoded_payload["sub"] = "00000000-0000-0000-0000-000000000000"
    tampered_payload = base64.urlsafe_b64encode(json.dumps(decoded_payload).encode()).decode().rstrip("=")
    
    tampered_token = f"{header}.{tampered_payload}.{signature}"
    
    res = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {tampered_token}"})
    assert res.status_code == 401

async def test_role_escalation_and_unauthorized_access(client):
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(SuperAdminUser).where(SuperAdminUser.email == "test_superadmin@nokvo.ai"))
        u = res.scalars().first()
        u.role = "readonly"
        u.status = "active"
        db.add(u)
        await db.commit()
        uid = u.id
    
    from app.core.security import create_access_token
    from app.models.session import SuperAdminSession
    import uuid
    
    async with AsyncSessionLocal() as db:
        session = SuperAdminSession(
            id=uuid.uuid4(),
            superadmin_id=uid,
            refresh_token_hash="dummy",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=4)
        )
        db.add(session)
        await db.commit()
        sid = session.id
    
    token = create_access_token(uid, mfa_completed=True, session_id=str(sid))
    
    res = await client.get("/api/auth/superadmin/test", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 403
    assert "Operation not permitted" in res.json()["detail"]

async def test_brute_force_protection(client):
    for i in range(5):
        res = await client.post("/api/auth/login", json={
            "email": "test_superadmin@nokvo.ai",
            "password": "WrongPassword!"
        })
        if i < 4:
            assert res.status_code == 401
        else:
            assert res.status_code == 403
            
    res = await client.post("/api/auth/login", json={
        "email": "test_superadmin@nokvo.ai",
        "password": "TestPassword123!"
    })
    assert res.status_code == 403
    
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(SuperAdminUser).where(SuperAdminUser.email == "test_superadmin@nokvo.ai"))
        u = res.scalars().first()
        assert u.status == "locked"
        assert u.failed_login_count == 5
        assert u.locked_until is not None
