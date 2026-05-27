from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

import redis.asyncio as redis

from app.core.config import settings


@asynccontextmanager
async def scheduler_leader(name: str, *, ttl_seconds: int) -> AsyncIterator[bool]:
    """Best-effort Redis leader lock for in-process schedulers.

    Every uvicorn worker may start the background task, so each drain pass must
    acquire this short lock before doing work. If Redis is unavailable we skip
    the pass instead of risking duplicate retries.
    """
    client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    key = f"nokvo:scheduler:{name}:leader"
    token = f"{os.getpid()}:{uuid.uuid4().hex}"
    acquired = False
    try:
        acquired = bool(await client.set(key, token, nx=True, ex=max(5, int(ttl_seconds))))
        yield acquired
    except Exception:
        yield False
    finally:
        if acquired:
            try:
                current = await client.get(key)
                if current == token:
                    await client.delete(key)
            except Exception:
                pass
        try:
            await client.aclose()
        except Exception:
            pass
