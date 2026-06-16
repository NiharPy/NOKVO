"""Reconstruct the full conversation of the most recent outbound voice call(s)
from LangSmith, in chronological order, so we can read it like a transcript.

Usage:
    venv/bin/python app/scripts/pull_outbound_convo.py [N]

N = how many recent calls to dump (default 1).
"""
import os
import sys
from dotenv import load_dotenv

load_dotenv()
from langsmith import Client

client = Client()
project = os.environ.get("LANGSMITH_PROJECT")

n_calls = int(sys.argv[1]) if len(sys.argv) > 1 else 1


def _first(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d.get(k)
    return None


# Root runs = whole calls. voice_call is the root span name.
roots = list(
    client.list_runs(
        project_name=project,
        is_root=True,
        limit=n_calls * 3,  # over-fetch; some roots may be non-call chains
        order="desc",
    )
)

calls = [r for r in roots if (r.name or "") == "voice_call"][:n_calls]
if not calls:
    # Fall back to whatever roots exist so we at least see something.
    calls = roots[:n_calls]

for ci, call in enumerate(calls):
    print("\n" + "=" * 90)
    print(f"CALL {ci+1}/{len(calls)}  name={call.name}  id={call.id}")
    meta = (call.extra or {}).get("metadata", {}) if call.extra else {}
    print(f"  start={call.start_time}  end={call.end_time}")
    print(f"  metadata={meta}")
    if call.error:
        print(f"  ROOT ERROR: {call.error}")

    # NOTE: we deliberately do NOT filter by trace_id. When the tracing
    # contextvar parent is lost mid-call (cancelled task / new asyncio task),
    # later turns spawn as their OWN root trace and get orphaned from the
    # voice_call root. So we gather every turn/llm run inside the call's
    # wall-clock window instead and stitch by start_time.
    from datetime import timedelta

    win_start = call.start_time
    win_end = (call.end_time or call.start_time) + timedelta(seconds=5)
    window = list(
        client.list_runs(
            project_name=project,
            start_time=win_start,
            limit=100,
            order="desc",
        )
    )
    trace_runs = [
        r
        for r in window
        if (r.start_time and win_start <= r.start_time <= win_end)
        and ((r.run_type == "llm") or (r.name or "").startswith("turn") or r.id == call.id)
    ]
    # Sort by start time so the conversation reads top-to-bottom.
    trace_runs.sort(key=lambda r: (r.start_time or call.start_time))

    # Show the system prompt once (first llm run) so we can see the brief/persona.
    shown_system = False
    for r in trace_runs:
        if (r.run_type or "") != "llm":
            continue
        msgs = (r.inputs or {}).get("messages") or []
        sys_msg = next(
            (m for m in msgs if isinstance(m, dict) and m.get("role") == "system"),
            None,
        )
        if sys_msg:
            print("\n----- SYSTEM PROMPT (turn 1) -----")
            print(str(sys_msg.get("content"))[:6000])
            shown_system = True
            break

    print("\n----- CONVERSATION -----")
    turn_no = 0
    for r in trace_runs:
        name = r.name or ""
        rtype = r.run_type or ""

        # Turn spans carry the user_text cleanly.
        if rtype == "chain" and name.startswith("turn"):
            turn_no += 1
            ut = _first(r.inputs or {}, "user_text")
            mode = _first(r.inputs or {}, "mode")
            tag = f"  ({mode})" if mode else ""
            print(f"\n[turn {turn_no}]{tag}")
            if ut is not None:
                print(f"  USER : {str(ut).replace(chr(10), ' ')}")

        # LLM leaf gives the agent reply.
        if rtype == "llm":
            out = r.outputs or {}
            resp = _first(out, "response", "output", "content") or "N/A"
            status = out.get("status")
            if status == "cancelled_barge_in":
                resp = f"[BARGED IN] {resp}"
            label = name if name not in ("ChatOpenAI", "llm") else "agent"
            # Skip non-conversational llm helpers (intent/outcome/condenser) in
            # the main flow unless they error — mark them so they're visible.
            print(f"  AGENT: {str(resp).replace(chr(10), ' ')}")
            if r.error:
                print(f"  !! LLM ERROR: {r.error}")

    # Outputs on the root (final summary / outcome, if attached).
    if call.outputs:
        print("\n----- CALL OUTPUTS (root) -----")
        for k, v in call.outputs.items():
            print(f"  {k}: {str(v)[:500]}")
