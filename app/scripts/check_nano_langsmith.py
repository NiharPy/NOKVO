import os
from dotenv import load_dotenv
load_dotenv()
from langsmith import Client

client = Client()
project_name = os.environ.get("LANGSMITH_PROJECT")
print("Fetching last 20 runs to check for Nano/Summary traces...")

runs = list(client.list_runs(project_name=project_name, run_type="llm", limit=20, order="desc"))

nano_runs = 0
errors = 0

for run in runs:
    # Check if the run name or tags mention nano or summary
    name = run.name or ""
    tags = run.tags or []
    
    # Also check the system prompt inside the messages to see if it's the summary prompt
    is_summary = False
    messages = run.inputs.get("messages", []) if run.inputs else []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "system":
            content = m.get("content", "")
            if "running summary" in content.lower() or "rewrite the summary" in content.lower():
                is_summary = True
                break
                
    if is_summary or "nano" in name.lower() or "summary" in name.lower():
        nano_runs += 1
        print(f"\n--- Found Summary/Nano Run (ID: {run.id}) ---")
        print(f"Status: {run.status}")
        if run.error:
            print(f"Error: {run.error}")
            errors += 1
        
        # Print what it was trying to summarize
        for m in reversed(messages):
            if isinstance(m, dict) and m.get("role") == "user":
                print(f"Input: {str(m.get('content'))[:150]}...")
                break

print(f"\nTotal runs analyzed: {len(runs)}")
print(f"Nano/Summary runs found: {nano_runs}")
if nano_runs > 0:
    print(f"Failed Nano runs: {errors}")
