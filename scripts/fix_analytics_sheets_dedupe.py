#!/usr/bin/env python3
"""Patch the live "Daily Analytics Digest" workflow to deduplicate Google Sheets rows.

ROOT CAUSE
----------
Both Google Sheets nodes in the workflow use operation="append". When the workflow
runs more than once on the same day (manual re-trigger, retry, etc.), a second row
is appended for the same (date, channel) or (date, video_id) key — producing
duplicates like two "bright-beasts" rows for 2026-06-17.

FIX
---
Switch both nodes to operation="appendOrUpdate" with explicit matchingColumns so a
second run on the same day UPDATES the existing row instead of appending a new one:

  "Append to Analytics Sheet"  →  match on: date + channel
  "Append video stats"         →  match on: date + video_id

The underlying data model (db.py) is already idempotent via INSERT OR REPLACE on
(channel, date) and (channel, video_id, date). This patch makes the Sheets layer
equally idempotent.

WHAT THIS SCRIPT DOES NOT DO
-----------------------------
It does NOT clean up existing duplicate rows already in the Sheet — that is a
separate one-time ops step (delete the extra rows manually, keeping the latest).

IDEMPOTENCY
-----------
Running this script more than once is safe: if a node is already on
appendOrUpdate with the correct matchingColumns, it prints "already correct" and
skips the PUT.

USAGE
-----
  N8N_API_KEY=<key> python scripts/fix_analytics_sheets_dedupe.py --dry-run
  N8N_API_KEY=<key> python scripts/fix_analytics_sheets_dedupe.py
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

WORKFLOW_ID = "ENbQm9ctfNRcnOuT"

# Node names as they exist in the live workflow (verified via n8n MCP 2026-06-21).
#   "Append to Analytics Sheet" — channel-level snapshot, "Daily" tab.
#   "Append video stats"        — per-video rows, one tab per channel slug.
CHANNEL_NODE = "Append to Analytics Sheet"
VIDEO_NODE = "Append video stats"

# Match keys for appendOrUpdate — must correspond to real header columns in each tab.
CHANNEL_MATCH_KEYS = ["date", "channel"]   # Daily tab: one row per (date, channel)
VIDEO_MATCH_KEYS = ["date", "video_id"]    # Per-channel tab: one row per (date, video_id)

# The public PUT /workflows schema rejects some newer settings keys with a 400.
# Preserve only the accepted subset (same list as add_video_stats_branch.py).
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


def _patch_sheets_node(node: dict, match_keys: list[str]) -> tuple[dict, bool]:
    """Return (patched_node, was_changed).

    Switches operation from "append" to "appendOrUpdate" and sets matchingColumns
    to match_keys. The schema entries for those keys get defaultMatch=True so n8n
    knows which columns to look up the existing row by.
    """
    params = node["parameters"]
    already_op = params.get("operation") == "appendOrUpdate"
    already_keys = sorted(params.get("columns", {}).get("matchingColumns", [])) == sorted(match_keys)

    if already_op and already_keys:
        return node, False  # already correct — idempotent no-op

    # Deep-copy only the parameters dict so we don't mutate the original in-place
    # before we've confirmed we want to PUT.
    import copy
    patched = copy.deepcopy(node)
    patched["parameters"]["operation"] = "appendOrUpdate"

    cols = patched["parameters"].setdefault("columns", {})
    cols["matchingColumns"] = match_keys

    # Mark match columns as defaultMatch=True in the schema so n8n surfaces them
    # in the UI as the lookup key. Non-match columns stay False.
    for entry in cols.get("schema", []):
        entry["defaultMatch"] = entry["id"] in match_keys

    return patched, True


def main() -> None:
    if not API_KEY:
        sys.exit("N8N_API_KEY (or N8N_API_TOKEN) env var is required.")

    print(f"{'DRY RUN — ' if DRY_RUN else ''}patching Sheets dedupe on {BASE_URL}")
    print(f"  workflow: {WORKFLOW_ID}")

    wf = _req("GET", f"/workflows/{WORKFLOW_ID}")
    node_names = {n["name"] for n in wf["nodes"]}

    for required in (CHANNEL_NODE, VIDEO_NODE):
        if required not in node_names:
            sys.exit(
                f"  ABORT — expected node '{required}' not found in workflow.\n"
                f"  Found nodes: {sorted(node_names)}"
            )

    # Snapshot ALL pre-existing nodes so the safety gate can verify we only
    # touched the two target nodes and nothing else.
    target_names = {CHANNEL_NODE, VIDEO_NODE}
    before = {n["name"]: json.dumps(n, sort_keys=True) for n in wf["nodes"]}

    changes: list[str] = []
    new_nodes: list[dict] = []

    for node in wf["nodes"]:
        name = node["name"]
        if name == CHANNEL_NODE:
            old_op = node["parameters"].get("operation", "append")
            patched, changed = _patch_sheets_node(node, CHANNEL_MATCH_KEYS)
            new_nodes.append(patched)
            if changed:
                changes.append(
                    f"  {CHANNEL_NODE!r}: operation {old_op!r} → 'appendOrUpdate', "
                    f"matchingColumns={CHANNEL_MATCH_KEYS}"
                )
            else:
                print(f"  {CHANNEL_NODE!r}: already correct — skipping")
        elif name == VIDEO_NODE:
            # NEUTRALIZED 2026-06-30. Do NOT switch the per-video node to
            # appendOrUpdate. It runs inside a batchSize-1 loop appending ~44
            # rows/run; appendOrUpdate reads the tab once per item, which
            # collapsed the per-channel tabs to one row/day and blew the Sheets
            # read-quota (regression 2026-06-22, exec 137). It was reverted to
            # 'append' live on 2026-06-30 and must NEVER be switched back here.
            # Left byte-for-byte unchanged.
            new_nodes.append(node)
            print(f"  {VIDEO_NODE!r}: left as-is — must stay on 'append'")
        else:
            new_nodes.append(node)

    if not changes:
        print("  Nothing to change — both nodes are already on appendOrUpdate.")
        return

    print("\nPlan:")
    for c in changes:
        print(c)

    # Safety gate: every non-target node must be byte-for-byte unchanged.
    for node in new_nodes:
        if node["name"] in target_names:
            continue
        after = json.dumps(node, sort_keys=True)
        if after != before[node["name"]]:
            sys.exit(
                f"  ABORT — unintended change detected in non-target node '{node['name']}'"
            )

    if DRY_RUN:
        print("\n  (dry run — no PUT)")
        return

    settings = {k: v for k, v in wf.get("settings", {}).items() if k in SETTINGS_ALLOWED}
    dropped = sorted(set(wf.get("settings", {})) - set(settings))
    if dropped:
        print(f"\n  (settings keys reset to default by public API: {', '.join(dropped)})")

    _req("PUT", f"/workflows/{WORKFLOW_ID}", {
        "name": wf["name"],
        "nodes": new_nodes,
        "connections": wf["connections"],
        "settings": settings,
    })

    # Verify the PUT landed correctly.
    fresh = _req("GET", f"/workflows/{WORKFLOW_ID}")
    fresh_by_name = {n["name"]: n for n in fresh["nodes"]}

    ok = True
    for node_name, expected_keys in [(CHANNEL_NODE, CHANNEL_MATCH_KEYS)]:  # VIDEO_NODE intentionally excluded — stays on append
        n = fresh_by_name.get(node_name, {})
        actual_op = n.get("parameters", {}).get("operation")
        actual_keys = sorted(n.get("parameters", {}).get("columns", {}).get("matchingColumns", []))
        node_ok = actual_op == "appendOrUpdate" and actual_keys == sorted(expected_keys)
        status = "OK" if node_ok else "FAILED"
        print(f"  [{status}] {node_name!r}: operation={actual_op!r}, matchingColumns={actual_keys}")
        ok = ok and node_ok

    still_active = fresh.get("active", False)
    err_wf = fresh.get("settings", {}).get("errorWorkflow")
    print(f"  active: {still_active} | errorWorkflow: {err_wf}")

    if not still_active:
        _req("POST", f"/workflows/{WORKFLOW_ID}/activate", {})
        print("  -> re-activated workflow")

    if not ok:
        sys.exit("  FAILED — one or more nodes did not update as expected.")

    print("\nDone. Future workflow runs will UPDATE existing rows instead of appending duplicates.")
    print("NOTE: existing duplicate rows already in the Sheet must be cleaned up manually.")


if __name__ == "__main__":
    main()
