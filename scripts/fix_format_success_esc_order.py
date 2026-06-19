#!/usr/bin/env python3
"""Fix a temporal-dead-zone bug in the daily pipelines' "Format success" node.

The Telegram parse_mode patch (scripts/patch_telegram_parsemode.py) inserted the
`const esc = ...` helper just before `return [`, but the daily pipelines use
`const title = esc(...)` ABOVE the return — so `esc` is referenced before its
`const` initialization:

    ReferenceError: Cannot access 'esc' before initialization

The upload succeeds, then this Code node throws, failing the execution and firing
the error workflow on every run. This script moves the `esc` declaration to
immediately before its first use. Idempotent (a no-op once esc precedes its use)
and safety-gated: it refuses to PUT if any node other than "Format success" would
change.

Run:
  N8N_API_KEY=<key> python scripts/fix_format_success_esc_order.py --dry-run
  N8N_API_KEY=<key> python scripts/fix_format_success_esc_order.py
(N8N_API_TOKEN is also accepted.)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = os.environ.get("N8N_BASE_URL", "http://100.66.55.82:5678").rstrip("/")
API_KEY = os.environ.get("N8N_API_KEY") or os.environ.get("N8N_API_TOKEN", "")
DRY_RUN = "--dry-run" in sys.argv

# The four daily-shorts pipelines; their success-notification Code node.
WORKFLOWS = [
    {"slug": "bright-beasts", "id": "V0YZwaEfQFuM0K9j"},
    {"slug": "open-verdicts", "id": "dVoWIr8mQE90qWH3"},
    {"slug": "the-mythscape", "id": "7dnSJbYza4lxB3dX"},
    {"slug": "wordstrata",    "id": "4eg67BVX75KuXsoN"},
]
CODE_NODE = "Format success"

SETTINGS_ALLOWED = {
    "saveExecutionProgress", "saveManualExecutions", "saveDataErrorExecution",
    "saveDataSuccessExecution", "executionTimeout", "errorWorkflow",
    "timezone", "executionOrder", "availableInMCP",
}


def _req(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{BASE_URL}/api/v1{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-N8N-API-KEY", API_KEY)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        sys.exit(f"  HTTP {e.code} on {method} {path}: {e.read().decode()[:400]}")


def fix_js(js: str) -> str:
    """Move the `const esc = ...` line to immediately before its first use."""
    lines = js.split("\n")
    decl_idx = next(
        (i for i, l in enumerate(lines) if l.strip().startswith("const esc")), None)
    use_idx = next((i for i, l in enumerate(lines) if "esc(" in l), None)
    if decl_idx is None or use_idx is None or decl_idx < use_idx:
        return js  # not patched, or esc already precedes its use
    decl = lines.pop(decl_idx)
    # popping decl (which was after use) doesn't shift use_idx
    lines.insert(use_idx, decl)
    return "\n".join(lines)


def fix_workflow(spec: dict) -> None:
    slug, wf_id = spec["slug"], spec["id"]
    wf = _req("GET", f"/workflows/{wf_id}")
    before = {n["name"]: json.dumps(n, sort_keys=True) for n in wf["nodes"]}

    node = next((n for n in wf["nodes"] if n["name"] == CODE_NODE), None)
    if node is None:
        print(f"  {slug}: no '{CODE_NODE}' node — skipped")
        return
    new_js = fix_js(node["parameters"]["jsCode"])
    if new_js == node["parameters"]["jsCode"]:
        print(f"  {slug}: already correct — no change")
        return
    node["parameters"]["jsCode"] = new_js

    for n in wf["nodes"]:
        if n["name"] == CODE_NODE:
            continue
        if json.dumps(n, sort_keys=True) != before[n["name"]]:
            sys.exit(f"  {slug}: ABORT — unintended change in node '{n['name']}'")

    print(f"  {slug}: moved `const esc` before its first use")
    if DRY_RUN:
        return

    settings = {k: v for k, v in wf.get("settings", {}).items() if k in SETTINGS_ALLOWED}
    dropped = sorted(set(wf.get("settings", {})) - set(settings))
    if dropped:
        print(f"    (settings reset to default by public API: {', '.join(dropped)})")
    _req("PUT", f"/workflows/{wf_id}", {
        "name": wf["name"],
        "nodes": wf["nodes"],
        "connections": wf["connections"],
        "settings": settings,
    })
    fresh = _req("GET", f"/workflows/{wf_id}")
    fjs = next(n for n in fresh["nodes"] if n["name"] == CODE_NODE)["parameters"]["jsCode"]
    fl = fjs.split("\n")
    di = next((i for i, l in enumerate(fl) if l.strip().startswith("const esc")), None)
    ui = next((i for i, l in enumerate(fl) if "esc(" in l), None)
    ok = di is not None and ui is not None and di < ui
    print(f"    -> verified: {'OK (esc precedes use)' if ok else 'FAILED — inspect manually'}")


def main() -> None:
    if not API_KEY:
        sys.exit("N8N_API_KEY (or N8N_API_TOKEN) env var is required.")
    print(f"{'DRY RUN — ' if DRY_RUN else ''}fixing '{CODE_NODE}' esc order on {BASE_URL}")
    for spec in WORKFLOWS:
        fix_workflow(spec)
    print("done")


if __name__ == "__main__":
    main()
