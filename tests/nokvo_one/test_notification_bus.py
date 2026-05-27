"""In-process notification bus unit tests.

We exercise the fanout layer in isolation — no DB, no FastAPI — to lock
the queue/subscriber contract that the live WebSocket route depends on.
The bus is the only piece of the notifications stack that has to behave
correctly under back-pressure (a slow browser tab) and across multiple
subscribers (a user with two tabs open). Both of those scenarios need a
deterministic, single-process test rather than an integration run.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from app.services.notification_service import _NotificationBus


@pytest.mark.asyncio
async def test_publish_reaches_every_subscriber_for_org() -> None:
    bus = _NotificationBus()
    org_id = str(uuid.uuid4())
    q1 = bus.subscribe(org_id)
    q2 = bus.subscribe(org_id)
    bus.publish(org_id, {"type": "notification", "data": {"id": "abc"}})
    msg1 = await asyncio.wait_for(q1.get(), timeout=0.5)
    msg2 = await asyncio.wait_for(q2.get(), timeout=0.5)
    assert msg1["data"]["id"] == "abc"
    assert msg2["data"]["id"] == "abc"


@pytest.mark.asyncio
async def test_publish_isolated_per_org() -> None:
    bus = _NotificationBus()
    org_a = str(uuid.uuid4())
    org_b = str(uuid.uuid4())
    qa = bus.subscribe(org_a)
    qb = bus.subscribe(org_b)
    bus.publish(org_a, {"type": "notification", "data": {"id": "a"}})
    msg = await asyncio.wait_for(qa.get(), timeout=0.5)
    assert msg["data"]["id"] == "a"
    # B must not have received the A message.
    assert qb.empty()


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery() -> None:
    bus = _NotificationBus()
    org_id = str(uuid.uuid4())
    queue = bus.subscribe(org_id)
    bus.unsubscribe(org_id, queue)
    bus.publish(org_id, {"type": "notification", "data": {"id": "x"}})
    assert queue.empty()


@pytest.mark.asyncio
async def test_publish_with_no_subscribers_is_a_noop() -> None:
    bus = _NotificationBus()
    # Must not raise. Equally, must not create a phantom subscriber set.
    bus.publish(str(uuid.uuid4()), {"type": "notification", "data": {}})


@pytest.mark.asyncio
async def test_full_queue_drops_oldest_and_keeps_newest() -> None:
    """Back-pressure semantics. A slow tab can't grow memory: when the
    queue is full, the oldest item is evicted so the freshest event still
    lands. This matches the production fanout policy."""
    bus = _NotificationBus()
    org_id = str(uuid.uuid4())
    queue = bus.subscribe(org_id)
    # Drive the queue to its maximum so the next publish must evict.
    for i in range(_NotificationBus._QUEUE_MAX):
        queue.put_nowait({"data": {"i": i}})
    bus.publish(org_id, {"type": "notification", "data": {"i": "newest"}})
    items: list[dict] = []
    while not queue.empty():
        items.append(queue.get_nowait())
    assert items[-1]["data"]["i"] == "newest"
    assert items[0]["data"]["i"] != 0  # oldest got evicted
