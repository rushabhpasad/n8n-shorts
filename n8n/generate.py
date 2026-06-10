#!/usr/bin/env python3
"""Generate one n8n workflow.json per channel.

Reads channels/<slug>/channel.json for slug + name, then writes
n8n/workflows/<slug>.json. Each workflow is identical in shape — they differ
only by URL prefix, name, and cron schedule (staggered to avoid Ollama
contention).

Run:
  python n8n/generate.py

Import each generated file into n8n via Workflows → Import from File.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHANNELS_DIR = REPO_ROOT / "channels"
WORKFLOWS_DIR = Path(__file__).resolve().parent / "workflows"

# Stagger cron schedules so the 4 channels don't all hit Ollama/Z-Image at
# the same minute. Image gen alone takes ~25 min/video; an hour apart leaves
# headroom for retries.
CRON_BY_SLUG = {
    "wordstrata":    "0 9 * * *",
    "the-mythscape": "0 10 * * *",
    "open-verdicts": "0 11 * * *",
    "bright-beasts": "0 12 * * *",
}

DEFAULT_CRON = "0 13 * * *"  # any extra channels start at 1pm


def make_workflow(slug: str, display_name: str, cron: str) -> dict:
    base = "http://host.docker.internal:7860"
    word_id_expr = "={\"word_id\": {{ $('Get next word').item.json.id }}}"
    upload_expr = (
        "={\"word_id\": {{ $('Get next word').item.json.id }}}"
    )

    def http_post(node_id: str, name: str, x: int, path: str,
                  body: str, timeout_ms: int) -> dict:
        return {
            "id": node_id,
            "name": name,
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [x, 300],
            "parameters": {
                "method": "POST",
                "url": f"{base}{path}",
                "sendBody": True,
                "contentType": "json",
                "specifyBody": "json",
                "jsonBody": body,
                "options": {"timeout": timeout_ms},
            },
        }

    nodes = [
        {
            "id": "trigger_manual",
            "name": "Manual Trigger",
            "type": "n8n-nodes-base.manualTrigger",
            "typeVersion": 1,
            "position": [240, 300],
            "parameters": {},
        },
        {
            "id": "trigger_cron",
            "name": "Schedule Trigger",
            "type": "n8n-nodes-base.scheduleTrigger",
            "typeVersion": 1.2,
            "position": [240, 500],
            "parameters": {
                "rule": {
                    "interval": [
                        {"field": "cronExpression", "expression": cron}
                    ]
                }
            },
        },
        {
            "id": "get_next",
            "name": "Get next word",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [460, 400],
            "parameters": {
                "method": "GET",
                "url": f"{base}/{slug}/state/next",
                "options": {"timeout": 10000},
            },
        },
        {
            "id": "if_pending",
            "name": "Has pending word?",
            "type": "n8n-nodes-base.if",
            "typeVersion": 2.2,
            "position": [680, 400],
            "parameters": {
                "conditions": {
                    "options": {
                        "caseSensitive": True,
                        "leftValue": "",
                        "typeValidation": "strict",
                    },
                    "conditions": [
                        {
                            "id": "cond_id_exists",
                            "leftValue": "={{ $('Get next word').item.json?.id }}",
                            "rightValue": "",
                            "operator": {
                                "type": "number",
                                "operation": "exists",
                                "singleValue": True,
                            },
                        }
                    ],
                    "combinator": "and",
                }
            },
        },
        http_post("do_script",    "Generate script",   900,  f"/{slug}/script",   word_id_expr, 180000),
        http_post("do_voice",     "Generate voice",   1120, f"/{slug}/voice",     word_id_expr,  60000),
        http_post("do_image",     "Generate images",  1340, f"/{slug}/image",     word_id_expr, 7200000),
        http_post("do_assemble",  "Assemble video",   1560, f"/{slug}/assemble",  word_id_expr, 120000),
        http_post("do_upload",    "Upload to YouTube", 1780, f"/{slug}/upload",   upload_expr,  600000),
    ]

    def fanout(src: str, dst: str) -> dict:
        return {src: {"main": [[{"node": dst, "type": "main", "index": 0}]]}}

    connections: dict = {}
    connections.update(fanout("Manual Trigger",    "Get next word"))
    connections.update(fanout("Schedule Trigger", "Get next word"))
    connections.update(fanout("Get next word",     "Has pending word?"))
    connections["Has pending word?"] = {
        "main": [
            [{"node": "Generate script", "type": "main", "index": 0}],
            [],
        ]
    }
    connections.update(fanout("Generate script", "Generate voice"))
    connections.update(fanout("Generate voice", "Generate images"))
    connections.update(fanout("Generate images", "Assemble video"))
    connections.update(fanout("Assemble video", "Upload to YouTube"))

    return {
        "name": f"{display_name} — Daily Shorts",
        "active": False,
        "settings": {"executionOrder": "v1"},
        "nodes": nodes,
        "connections": connections,
    }


def main() -> None:
    WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)

    for ch_dir in sorted(CHANNELS_DIR.iterdir()):
        if not ch_dir.is_dir():
            continue
        cfg_path = ch_dir / "channel.json"
        if not cfg_path.exists():
            continue
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        slug = cfg["slug"]
        name = cfg["name"]
        cron = CRON_BY_SLUG.get(slug, DEFAULT_CRON)
        wf = make_workflow(slug, name, cron)
        out = WORKFLOWS_DIR / f"{slug}.json"
        out.write_text(json.dumps(wf, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {out}  (cron: {cron})")


if __name__ == "__main__":
    main()
