"""Reconstruct ONE outbound call's conversation from LangSmith, by call_id.

Usage:
    venv/bin/python app/scripts/pull_call_by_id.py <call_id> [search_limit]

Finds the voice_call root whose metadata.call_id matches, then stitches every
turn/llm run in its wall-clock window (same trace-split-safe approach as
pull_outbound_convo.py).
"""
import os
import sys
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()
from langsmith import Client

client = Client()
project = os.environ.get("LANGSMITH_PROJECT")

target = sys.argv[1] if len(sys.argv) > 1 else None
search_limit = int(sys.argv[2]) if len(sys.argv) > 2 else 300
if not target:
    print("usage: pull_call_by_id.py <call_id> [search_limit]")
    sys.exit(1)


def _meta(r):
    return (r.extra or {}).get("metadata", {}) if r.extra else {}


def _first(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d.get(k)
    return None


# The API caps page size at 100; page through in Python up to search_limit.
roots = []
for r in client.list_runs(project_name=project, is_root=True, order="desc"):
    roots.append(r)
    if len(roots) >= search_limit:
        break
match = None
for r in roots:
    if (r.name or "") != "voice_call":
        continue
    if str(_meta(r).get("call_id") or "") == target:
        match = r
        break

if match is None:
    # Fall back: scan ALL recent roots (not just voice_call) for the call_id.
    for r in roots:
        if str(_meta(r).get("call_id") or "") == target:
            match = r
            break

if match is None:
    print(f"No root run found with call_id={target} in the last {search_limit} roots.")
    print("Recent voice_call roots (id / call_id / start):")
    for r in roots:
        if (r.name or "") == "voice_call":
            print(f"  {r.id}  call_id={_meta(r).get('call_id')}  {r.start_time}")
    sys.exit(2)

call = match
print("=" * 90)
print(f"CALL  id={call.id}  name={call.name}")
print(f"  start={call.start_time}  end={call.end_time}")
print(f"  metadata={_meta(call)}")
if call.error:
    print(f"  ROOT ERROR: {call.error}")

win_start = call.start_time
win_end = (call.end_time or call.start_time) + timedelta(seconds=5)
window = list(
    client.list_runs(project_name=project, start_time=win_start, limit=100, order="desc")
)
trace_runs = [
    r
    for r in window
    if (r.start_time and win_start <= r.start_time <= win_end)
    and ((r.run_type == "llm") or (r.name or "").startswith("turn") or r.id == call.id)
]
trace_runs.sort(key=lambda r: (r.start_time or call.start_time))

# System prompt once (first llm run).
for r in trace_runs:
    if (r.run_type or "") != "llm":
        continue
    msgs = (r.inputs or {}).get("messages") or []
    sys_msg = next((m for m in msgs if isinstance(m, dict) and m.get("role") == "system"), None)
    if sys_msg:
        print("\n----- SYSTEM PROMPT (turn 1) -----")
        print(str(sys_msg.get("content"))[:8000])
        break

print("\n----- CONVERSATION -----")
turn_no = 0
for r in trace_runs:
    name = r.name or ""
    rtype = r.run_type or ""
    if rtype == "chain" and name.startswith("turn"):
        turn_no += 1
        ut = _first(r.inputs or {}, "user_text")
        mode = _first(r.inputs or {}, "mode")
        tag = f"  ({mode})" if mode else ""
        print(f"\n[turn {turn_no}]{tag}")
        if ut is not None:
            print(f"  USER : {str(ut).replace(chr(10), ' ')}")
    if rtype == "llm":
        out = r.outputs or {}
        resp = _first(out, "response", "output", "content") or "N/A"
        status = out.get("status")
        if status == "cancelled_barge_in":
            resp = f"[BARGED IN] {resp}"
        print(f"  AGENT: {str(resp).replace(chr(10), ' ')}")
        if r.error:
            print(f"  !! LLM ERROR: {r.error}")

if call.outputs:
    print("\n----- CALL OUTPUTS (root) -----")
    for k, v in call.outputs.items():
        print(f"  {k}: {str(v)[:500]}")
