"""Manual load test for the shared LLM pool under concurrent calls.

Simulates N simultaneous calls each doing a reserve→reconcile cycle against the
REAL pool + Redis, then prints a summary (successes, rejections, per-box draw,
remaining budgets). Use it to eyeball behaviour under contention heavier than
the unit test — e.g. "what happens at 50 concurrent calls on the live pool".

    venv/bin/python -m app.scripts.loadtest_llm_pool --calls 20 --est 800

Reservations are reconciled back to a small "actual" so the run leaves the live
budget roughly as it found it. It still touches real Redis budget keys for the
current window, so prefer running it against a staging/dev Redis.
"""
from __future__ import annotations

import argparse
import asyncio
import time
from collections import Counter

from app.services.llm_pool import LLMPool, LLMPoolError


async def _one_call(est: int, actual: int, *, pool: str, sticky: bool):
    member = await LLMPool.reserve(est, pool=pool, sticky=sticky)
    if member is None:
        return None
    # Hand most of the reservation back so a load run doesn't drain the window.
    await LLMPool.reconcile(member, est, actual, pool=pool)
    return member.key_id


async def _main(args) -> int:
    client = LLMPool.client()
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001
        print(f"Redis not reachable at the configured REDIS_URL: {exc}")
        return 2

    try:
        members = LLMPool.members(args.pool)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not build pool '{args.pool}': {exc}")
        return 2
    if not members:
        print(f"No members configured for pool '{args.pool}'.")
        return 2

    print(f"Pool '{args.pool}': {len(members)} box(es) — "
          + ", ".join(f"{m.key_id}(tpm={m.tpm})" for m in members))
    print(f"Firing {args.calls} concurrent calls, est={args.est}, actual={args.actual}, "
          f"sticky={args.sticky}\n")

    t0 = time.monotonic()
    try:
        results = await asyncio.gather(
            *(_one_call(args.est, args.actual, pool=args.pool, sticky=args.sticky)
              for _ in range(args.calls))
        )
    except LLMPoolError as exc:
        print(f"Pool error: {exc}")
        return 1
    elapsed = (time.monotonic() - t0) * 1000

    granted = [r for r in results if r is not None]
    rejected = len(results) - len(granted)
    draw = Counter(granted)

    print(f"Granted: {len(granted)}/{args.calls}   Rejected (pool full): {rejected}   "
          f"Wall: {elapsed:.0f} ms")
    if draw:
        print("Per-box draw:")
        for key_id, n in draw.most_common():
            print(f"  {key_id:>20}: {n}")

    print("Remaining budget per box (current window):")
    for m in members:
        key = LLMPool._window_key(m.key_id, pool=args.pool)
        raw = await client.get(key)
        remaining = m.tpm if raw is None else int(raw)
        print(f"  {m.key_id:>20}: {remaining}/{m.tpm}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--calls", type=int, default=10, help="concurrent calls to simulate")
    p.add_argument("--est", type=int, default=800, help="estimated tokens reserved per call")
    p.add_argument("--actual", type=int, default=200, help="reconciled actual tokens per call")
    p.add_argument("--pool", default="mini", choices=["mini", "nano"], help="named pool")
    p.add_argument("--no-sticky", dest="sticky", action="store_false",
                   help="disable sticky home-box routing (spread across boxes)")
    p.set_defaults(sticky=True)
    raise SystemExit(asyncio.run(_main(p.parse_args())))


if __name__ == "__main__":
    main()
