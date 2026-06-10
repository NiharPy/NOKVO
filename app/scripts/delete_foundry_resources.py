"""HARD-delete Azure AI Foundry resources (Cognitive Services / Azure OpenAI
accounts) in the signed-in account.

"Hard delete" = delete the account AND purge its soft-deleted copy, so it is gone
permanently and the name is freed (a plain delete leaves it recoverable ~48h and
keeps the name reserved).

Targets ``Microsoft.CognitiveServices/accounts`` of the given --kinds (default
``OpenAI`` + ``AIServices`` — the Foundry/Azure-OpenAI kinds). Optionally also
the Foundry hubs/projects (``Microsoft.MachineLearningServices/workspaces``).

⚠️  DANGER — "all Foundry resources" INCLUDES the resources this app now relies on:
    the shared GPT-5-mini **pool** deployments, the **global** Azure OpenAI account,
    and the **realtime** (gpt-realtime-mini) account. Purging those breaks the live
    agent. Use --exclude to protect them (matched as a case-insensitive substring
    against the account name), and ALWAYS run the dry-run first.

USAGE
-----
Dry run (default — lists what WOULD be hard-deleted, changes nothing):

    source venv/bin/activate
    python -m app.scripts.delete_foundry_resources

Protect the pool/global/realtime accounts and dry-run:

    python -m app.scripts.delete_foundry_resources --exclude pool --exclude global --exclude realtime

All subscriptions, include Foundry hubs:

    python -m app.scripts.delete_foundry_resources --all-subscriptions --include-hubs

Execute (requires the flag AND a typed confirmation phrase):

    python -m app.scripts.delete_foundry_resources --execute --exclude pool --exclude global --exclude realtime

REQUIREMENTS
------------
- ``az`` CLI logged in (``az login``) with rights to delete/purge Cognitive
  Services accounts in the target subscription(s).
- Dry-run by default; ``--execute`` + typing the exact phrase is required to mutate.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

CONFIRMATION_PHRASE = "HARD DELETE FOUNDRY RESOURCES"


def _log(msg: str) -> None:
    print(msg, flush=True)


def _az(*args: str, check: bool = True) -> tuple[int, str, str]:
    """Run an az command, return (rc, stdout, stderr). az is invoked with
    --only-show-errors to keep output clean."""
    proc = subprocess.run(
        ["az", *args, "--only-show-errors"],
        capture_output=True, text=True,
    )
    if check and proc.returncode != 0:
        _log(f"  az {' '.join(args)} -> FAILED rc={proc.returncode}: {proc.stderr.strip()[:300]}")
    return proc.returncode, proc.stdout, proc.stderr


def _subscriptions(all_subs: bool) -> list[str]:
    if not all_subs:
        rc, out, _ = _az("account", "show", "--query", "id", "-o", "tsv")
        return [out.strip()] if rc == 0 and out.strip() else []
    rc, out, _ = _az("account", "list", "--query", "[?state=='Enabled'].id", "-o", "tsv")
    return [s for s in out.splitlines() if s.strip()] if rc == 0 else []


def _list_cognitive_accounts(sub: str, kinds: set[str]) -> list[dict]:
    rc, out, _ = _az("cognitiveservices", "account", "list", "--subscription", sub, "-o", "json")
    if rc != 0 or not out.strip():
        return []
    try:
        accounts = json.loads(out)
    except json.JSONDecodeError:
        return []
    result = []
    for a in accounts:
        kind = str(a.get("kind") or "")
        if kinds and kind not in kinds:
            continue
        # resourceGroup isn't always top-level; derive from the id if needed.
        rg = a.get("resourceGroup")
        if not rg:
            parts = str(a.get("id") or "").split("/")
            rg = parts[parts.index("resourceGroups") + 1] if "resourceGroups" in parts else None
        result.append({
            "name": a.get("name"), "rg": rg, "kind": kind,
            "location": a.get("location"), "sub": sub,
        })
    return result


def _list_foundry_hubs(sub: str) -> list[dict]:
    """Foundry hubs/projects are ML workspaces (kind Hub/Project). Best-effort —
    needs the `ml` extension; skipped silently if unavailable."""
    rc, out, _ = _az("resource", "list", "--subscription", sub,
                      "--resource-type", "Microsoft.MachineLearningServices/workspaces", "-o", "json")
    if rc != 0 or not out.strip():
        return []
    try:
        items = json.loads(out)
    except json.JSONDecodeError:
        return []
    hubs = []
    for w in items:
        if str(w.get("kind") or "").lower() in {"hub", "project", "default"}:
            hubs.append({"name": w.get("name"), "rg": w.get("resourceGroup"),
                         "kind": w.get("kind"), "location": w.get("location"),
                         "sub": sub, "ml_workspace": True})
    return hubs


def _excluded(name: str, patterns: list[str]) -> str | None:
    low = (name or "").lower()
    for p in patterns:
        if p.lower() in low:
            return p
    return None


def _hard_delete_account(acc: dict) -> bool:
    name, rg, loc, sub = acc["name"], acc["rg"], acc["location"], acc["sub"]
    rc, _, _ = _az("cognitiveservices", "account", "delete",
                   "--name", name, "--resource-group", rg, "--subscription", sub)
    if rc != 0:
        return False
    # Purge the soft-deleted copy → the actual HARD delete.
    rc, _, _ = _az("cognitiveservices", "account", "purge",
                   "--name", name, "--resource-group", rg, "--location", loc, "--subscription", sub)
    if rc != 0:
        _log(f"    deleted but PURGE failed for {name} (still soft-deleted — purge manually)")
        return False
    return True


def _delete_hub(hub: dict) -> bool:
    rc, _, _ = _az("resource", "delete", "--ids",
                   f"/subscriptions/{hub['sub']}/resourceGroups/{hub['rg']}"
                   f"/providers/Microsoft.MachineLearningServices/workspaces/{hub['name']}")
    return rc == 0


def run(args: argparse.Namespace) -> None:
    kinds = set(k.strip() for k in args.kinds.split(",") if k.strip())
    subs = _subscriptions(args.all_subscriptions)
    if not subs:
        _log("No subscription context. Run `az login` first.")
        sys.exit(1)

    targets: list[dict] = []
    protected: list[dict] = []
    for sub in subs:
        for acc in _list_cognitive_accounts(sub, kinds):
            ex = _excluded(acc["name"] or "", args.exclude)
            (protected if ex else targets).append({**acc, "_excluded_by": ex})
        if args.include_hubs:
            for hub in _list_foundry_hubs(sub):
                ex = _excluded(hub["name"] or "", args.exclude)
                (protected if ex else targets).append({**hub, "_excluded_by": ex})

    _log("=" * 78)
    _log(f"Subscriptions scanned: {len(subs)} | kinds: {sorted(kinds)} | include_hubs={args.include_hubs}")
    _log(f"TO HARD-DELETE: {len(targets)}   PROTECTED (--exclude): {len(protected)}")
    _log("=" * 78)
    for p in protected:
        _log(f"  KEEP  {p['name']:40} kind={p.get('kind'):12} (matched --exclude '{p['_excluded_by']}')")
    for t in targets:
        tag = "ml-workspace" if t.get("ml_workspace") else t.get("kind")
        _log(f"  DEL   {t['name']:40} kind={tag:12} rg={t.get('rg')} loc={t.get('location')}")

    if not targets:
        _log("\nNothing to delete.")
        return
    if not args.execute:
        _log("\nDRY RUN. Re-run with --execute to hard-delete the DEL rows above.")
        _log("STRONGLY consider --exclude pool --exclude global --exclude realtime first.")
        return

    _log("\nThis PERMANENTLY deletes + purges the resources marked DEL above.")
    _log("This is IRREVERSIBLE. If the app's pool/global/realtime accounts are in the")
    _log("DEL list, the live agent will break.\n")
    _log(f"Type exactly:  {CONFIRMATION_PHRASE}")
    if input("> ").strip() != CONFIRMATION_PHRASE:
        _log("Aborted (phrase did not match).")
        return

    ok = fail = 0
    for t in targets:
        _log(f"\nhard-deleting {t['name']} ({t.get('kind')})...")
        success = _delete_hub(t) if t.get("ml_workspace") else _hard_delete_account(t)
        if success:
            ok += 1
            _log("  done (deleted + purged)")
        else:
            fail += 1
    _log("\n" + "=" * 78)
    _log(f"SUMMARY: {ok} hard-deleted, {fail} failed")
    _log("=" * 78)
    if fail:
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--execute", action="store_true",
                        help="Actually delete+purge (default: dry-run). Also requires the typed phrase.")
    parser.add_argument("--all-subscriptions", action="store_true",
                        help="Scan every enabled subscription (default: current only).")
    parser.add_argument("--kinds", default="OpenAI,AIServices",
                        help="Comma-separated Cognitive Services kinds to target (default: OpenAI,AIServices).")
    parser.add_argument("--exclude", action="append", default=[],
                        help="Protect accounts whose name contains this substring (repeatable). "
                             "e.g. --exclude pool --exclude global --exclude realtime")
    parser.add_argument("--include-hubs", action="store_true",
                        help="Also target Azure AI Foundry hubs/projects (ML workspaces).")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
