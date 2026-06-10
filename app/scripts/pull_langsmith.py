import os
from dotenv import load_dotenv
load_dotenv()
from langsmith import Client

client = Client()
project_name = os.environ.get("LANGSMITH_PROJECT")
runs = list(client.list_runs(project_name=project_name, run_type="llm", limit=5, order="desc"))

for i, run in enumerate(runs):
    print(f"\n--- Run {i+1} ---")
    
    # Tokens
    prompt_tokens = getattr(run, 'prompt_tokens', None)
    completion_tokens = getattr(run, 'completion_tokens', None)
    total_tokens = getattr(run, 'total_tokens', None)
    
    out = run.outputs or {}
    # Fallback to checking outputs dictionary
    if prompt_tokens is None: prompt_tokens = out.get("prompt_tokens")
    if completion_tokens is None: completion_tokens = out.get("completion_tokens")
    if total_tokens is None: total_tokens = out.get("total_tokens")
    
    print(f"Tokens: Prompt={prompt_tokens} | Completion={completion_tokens} | Total={total_tokens}")
    
    # Extract last user message
    messages = run.inputs.get("messages", []) if run.inputs else []
    last_user_msg = "N/A"
    if messages:
        for m in reversed(messages):
            if isinstance(m, dict) and m.get("role") == "user":
                last_user_msg = m.get("content")
                break
    user_str = str(last_user_msg).replace('\n', ' ')
    print(f"User: {user_str[:250]}")
    
    # Extract agent reply
    resp = out.get("response", "N/A")
    status = out.get("status", "completed")
    if status == "cancelled_barge_in":
        resp = "[BARGED IN / CANCELLED]"
    
    resp_str = str(resp).replace('\n', ' ')
    print(f"Agent: {resp_str[:300]}")
