#!/usr/bin/env python3
"""Surgically patch the live n8n daily-shorts workflows for the Telegram 400 bug.

Root cause: the "Notify success" Telegram node had no parse_mode, so the running
n8n instance applied its legacy *Markdown* default. A '_' in the YouTube Shorts
URL (video IDs use [A-Za-z0-9_-]) then opened an unterminated Markdown entity →
`400 Bad Request: can't parse entities`. Data-dependent, so it fails only on the
days the video ID happens to contain an odd number of '_'.

Fix (two fields per workflow, nothing else touched):
  1. Notify success (telegram): additionalFields.parse_mode = "HTML"
  2. Format success (code):      HTML-escape the dynamic title (&, <, >)

This patches via the n8n public REST API (GET → edit → PUT) so existing node
expressions are preserved byte-for-byte — we deliberately avoid the n8n-mcp
SDK-reconstruction path, which has corrupted expressions before. The script is
idempotent and refuses to PUT if anything other than the two intended fields
would change.

Run:
  N8N_API_KEY=<key> python scripts/patch_telegram_parsemode.py
  N8N_API_KEY=<key> python scripts/patch_telegram_parsemode.py --dry-run
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
import urllib.error

BASE_URL = os.environ.get("N8N_BASE_URL", "http://100.66.55.82:5678").rstrip("/")
API_KEY = os.environ.get("N8N_API_KEY", "")
DRY_RUN = "--dry-run" in sys.argv

# Live workflows to patch: the four daily-shorts pipelines plus the shared error
# alert. Each names its Telegram node and its upstream Code node, since the daily
# pipelines ("Notify success"/"Format success") and the error alert
# ("Send alert"/"Format error") use different node names.
WORKFLOWS = [
    {"slug": "bright-beasts", "id": "V0YZwaEfQFuM0K9j", "telegram": "Notify success", "code": "Format success"},
    {"slug": "open-verdicts", "id": "dVoWIr8mQE90qWH3", "telegram": "Notify success", "code": "Format success"},
    {"slug": "the-mythscape", "id": "7dnSJbYza4lxB3dX", "telegram": "Notify success", "code": "Format success"},
    {"slug": "wordstrata",    "id": "4eg67BVX75KuXsoN", "telegram": "Notify success", "code": "Format success"},
    {"slug": "error-alert",   "id": "1VcriNGIB4vF6A0u", "telegram": "Send alert",     "code": "Format error"},
]

# The public PUT /workflows schema is stricter than what GET returns: it rejects
# newer settings keys (binaryMode, availableInMCP, timeSavedMode, callerPolicy).
# Send only the keys it accepts so errorWorkflow/executionOrder are preserved.
SETTINGS_ALLOWED = {
    "saveExecutionProgress", "saveManualExecutions", "saveDataErrorExecution",
    "saveDataSuccessExecution", "executionTimeout", "errorWorkflow",
    "timezone", "executionOrder",
}

ESC_DECL = (
    "const esc = (s) => String(s).replace(/&/g, '&amp;')"
    ".replace(/</g, '&lt;').replace(/>/g, '&gt;');\n"
)


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
        sys.exit(f"  HTTP {e.code} on {method} {path}: {e.read().decode()[:300]}")


def patch_code_node(js: str) -> str:
    """HTML-escape the dynamic text a Format node interpolates. Idempotent.

    Handles both shapes in use:
      - assignment style:   const title = word.word || ...;   (daily pipelines)
      - template-literal:   `... ${wf} ... ${err} ...`        (error alert)
    """
    if "esc(" in js:
        return js
    # Template-literal: wrap bare `${ident}` interpolations -> `${esc(ident)}`.
    js2 = re.sub(r"\$\{([A-Za-z_]\w*)\}", r"${esc(\1)}", js)
    # Assignment style: wrap the title RHS once.
    js2 = re.sub(r"(const title = )(.+);", r"\1esc(\2);", js2, count=1)
    if js2 == js:
        return js
    # Insert the esc helper immediately before its FIRST use so it is never
    # referenced in its temporal dead zone. Daily pipelines use it above the
    # return (`const title = esc(...)`); the error alert uses it only inside the
    # return template. Placing it before `return` unconditionally would put the
    # `const esc` decl below `const title = esc(...)` → ReferenceError.
    if "const title = esc(" in js2:
        return js2.replace("const title = esc(", ESC_DECL + "const title = esc(", 1)
    return js2.replace("return [", ESC_DECL + "return [", 1)


def patch_workflow(spec: dict) -> None:
    slug, wf_id = spec["slug"], spec["id"]
    tg_name, code_name = spec["telegram"], spec["code"]
    wf = _req("GET", f"/workflows/{wf_id}")
    before = json.dumps(wf["nodes"], sort_keys=True)
    changed: list[str] = []

    for node in wf["nodes"]:
        if node.get("name") == tg_name and node.get("type") == "n8n-nodes-base.telegram":
            af = node["parameters"].setdefault("additionalFields", {})
            if af.get("parse_mode") != "HTML":
                af["parse_mode"] = "HTML"
                changed.append(f"{tg_name}.parse_mode=HTML")
        elif node.get("name") == code_name and node.get("type") == "n8n-nodes-base.code":
            new_js = patch_code_node(node["parameters"]["jsCode"])
            if new_js != node["parameters"]["jsCode"]:
                node["parameters"]["jsCode"] = new_js
                changed.append(f"{code_name}.jsCode (HTML-escape)")

    if not changed:
        print(f"  {slug}: already patched — no change")
        return

    # Safety gate: confirm only the two intended nodes differ.
    after = json.loads(json.dumps(wf["nodes"]))
    b_nodes = {n["name"]: n for n in json.loads(before)}
    for n in after:
        if n["name"] in (tg_name, code_name):
            continue
        if json.dumps(n, sort_keys=True) != json.dumps(b_nodes[n["name"]], sort_keys=True):
            sys.exit(f"  {slug}: ABORT — unintended change in node '{n['name']}'")

    print(f"  {slug}: {', '.join(changed)}")
    if DRY_RUN:
        return

    settings = {k: v for k, v in wf.get("settings", {}).items() if k in SETTINGS_ALLOWED}
    dropped = sorted(set(wf.get("settings", {})) - set(settings))
    if dropped:
        print(f"    (settings keys not accepted by public API, revert to default: {', '.join(dropped)})")
    payload = {
        "name": wf["name"],
        "nodes": wf["nodes"],
        "connections": wf["connections"],
        "settings": settings,
    }
    _req("PUT", f"/workflows/{wf_id}", payload)

    # Verify the live state post-PUT.
    fresh = _req("GET", f"/workflows/{wf_id}")
    tg = next(n for n in fresh["nodes"] if n["name"] == tg_name)
    fmt = next(n for n in fresh["nodes"] if n["name"] == code_name)
    ok = tg["parameters"]["additionalFields"].get("parse_mode") == "HTML" and "esc(" in fmt["parameters"]["jsCode"]
    print(f"    -> verified: {'OK' if ok else 'FAILED — inspect manually'}")


def main() -> None:
    if not API_KEY:
        sys.exit("N8N_API_KEY env var is required (Settings -> n8n API in n8n).")
    print(f"{'DRY RUN — ' if DRY_RUN else ''}patching {len(WORKFLOWS)} workflows on {BASE_URL}")
    for spec in WORKFLOWS:
        patch_workflow(spec)
    print("done")


if __name__ == "__main__":
    main()
