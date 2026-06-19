from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

engine = create_async_engine(
    settings.SQLALCHEMY_DATABASE_URI,
    echo=False,
    # Sized for many small Container Apps replicas, not one big process: each
    # replica caps at pool_size + max_overflow = 20 connections, so N replicas
    # stay well under the Postgres SKU's max_connections. The per-turn hot path
    # is Redis, not Postgres, so a small pool is ample. (Scale path: front the
    # DB with Flexible Server's built-in PgBouncer in transaction mode and add
    # connect_args={"statement_cache_size": 0} for asyncpg.)
    pool_size=10,
    max_overflow=10,
    pool_recycle=300,
    pool_pre_ping=True,
)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session
