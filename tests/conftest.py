import pytest
import pytest_asyncio
import uuid
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.main import app as fastapi_app
from app.db import session as db_session
from app.core.config import settings
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import create_async_engine

# Recreate engine with NullPool to prevent asyncpg connection sharing issues during tests
db_session.engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, poolclass=NullPool)
db_session.AsyncSessionLocal.configure(bind=db_session.engine)

from app.models.user import SuperAdminUser
from app.models.session import SuperAdminSession
from app.models.audit import SuperAdminAuditLog
from app.core.security import get_password_hash

TEST_USER_EMAIL = "test_superadmin@nokvo.ai"
TEST_USER_PASSWORD = "TestPassword123!"

@pytest.fixture(scope="session")
def event_loop():
    import asyncio
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()

@pytest_asyncio.fixture(scope="session")
async def client():
    async with AsyncClient(transport=ASGITransport(app=fastapi_app), base_url="http://test") as ac:
        yield ac

@pytest_asyncio.fixture(scope="session", autouse=True)
async def setup_teardown_db():
    # Setup
    async with db_session.AsyncSessionLocal() as db:
        from sqlalchemy import select
        res = await db.execute(select(SuperAdminUser).where(SuperAdminUser.email == TEST_USER_EMAIL))
        existing = res.scalars().first()
        if existing:
            await db.execute(delete(SuperAdminAuditLog).where(SuperAdminAuditLog.superadmin_id == existing.id))
            await db.execute(delete(SuperAdminSession).where(SuperAdminSession.superadmin_id == existing.id))
            await db.delete(existing)
            await db.commit()
        
        user = SuperAdminUser(
            id=uuid.uuid4(),
            email=TEST_USER_EMAIL,
            password_hash=get_password_hash(TEST_USER_PASSWORD),
            full_name="Test User",
            role="founder",
            status="pending",
            mfa_required=True
        )
        db.add(user)
        await db.commit()
    
    yield
    
    # Teardown
    async with db_session.AsyncSessionLocal() as db:
        res = await db.execute(select(SuperAdminUser).where(SuperAdminUser.email == TEST_USER_EMAIL))
        existing = res.scalars().first()
        if existing:
            await db.execute(delete(SuperAdminAuditLog).where(SuperAdminAuditLog.superadmin_id == existing.id))
            await db.execute(delete(SuperAdminSession).where(SuperAdminSession.superadmin_id == existing.id))
            await db.delete(existing)
            await db.commit()
