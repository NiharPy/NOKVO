"""Deep per-turn diagnostic for ONE call: timing gaps, history continuity, errors.

Usage: venv/bin/python app/scripts/diag_call.py <call_id> [search_limit]
"""
import os
import sys
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()
from langsmith import Client

client = Client()
project = os.environ.get("LANGSMITH_PROJECT")
target = sys.argv[1]
search_limit = int(sys.argv[2]) if len(sys.argv) > 2 else 400


def _meta(r):
    return (r.extra or {}).get("metadata", {}) if r.extra else {}


roots = []
for r in client.list_runs(project_name=project, is_root=True, order="desc"):
    roots.append(r)
    if len(roots) >= search_limit:
        break
match = next(
    (r for r in roots if (r.name or "") == "voice_call" and str(_meta(r).get("call_id") or "") == target),
    None,
)
if match is None:
    match = next((r for r in roots if str(_meta(r).get("call_id") or "") == target), None)
if match is None:
    print("not found")
    sys.exit(2)

call = match
print(f"CALL {call.id}  {call.start_time} -> {call.end_time}  dur={(call.end_time-call.start_time).total_seconds():.1f}s")
if call.error:
    print(f"ROOT ERROR: {call.error}")

win_start = call.start_time
win_end = (call.end_time or call.start_time) + timedelta(seconds=5)
window = list(client.list_runs(project_name=project, start_time=win_start, limit=100, order="desc"))
runs = [r for r in window if r.start_time and win_start <= r.start_time <= win_end]
runs.sort(key=lambda r: (r.start_time or call.start_time))

prev_end = call.start_time
for r in runs:
    rtype = r.run_type or ""
    name = r.name or ""
    if rtype == "llm":
        gap = (r.start_time - prev_end).total_seconds() if r.start_time else 0
        dur = (r.end_time - r.start_time).total_seconds() if r.end_time else 0
        msgs = (r.inputs or {}).get("messages") or []
        # Count non-system messages = history depth the model saw this turn.
        hist = [m for m in msgs if isinstance(m, dict) and m.get("role") != "system"]
        roles = "".join("U" if m.get("role") == "user" else "A" if m.get("role") == "assistant" else "?" for m in hist)
        out = r.outputs or {}
        resp = out.get("response") or out.get("output") or out.get("content") or "N/A"
        status = out.get("status")
        last_user = next((m.get("content") for m in reversed(hist) if m.get("role") == "user"), None)
        print(f"\n--- LLM run  start=+{(r.start_time-call.start_time).total_seconds():.1f}s  gap_since_prev={gap:.1f}s  dur={dur:.2f}s  name={name}")
        print(f"    history_depth={len(hist)}  roles=[{roles}]  status={status}")
        print(f"    last_user_in_prompt: {str(last_user)[:120]!r}")
        print(f"    response: {str(resp)[:200]!r}")
        if r.error:
            print(f"    !! ERROR: {r.error}")
        prev_end = r.end_time or r.start_time
    elif name.startswith("turn") or rtype == "chain":
        if name.startswith("turn"):
            print(f"\n[{name}]  inputs_keys={list((r.inputs or {}).keys())}  +{(r.start_time-call.start_time).total_seconds():.1f}s")
            for k in ("user_text", "mode", "source", "language"):
                v = (r.inputs or {}).get(k)
                if v not in (None, ""):
                    print(f"    {k}={v!r}")
            if r.error:
                print(f"    !! TURN ERROR: {r.error}")
