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

# Notification wiring (n8n instance-specific identifiers; tokens live in n8n credentials)
TELEGRAM_CHAT_ID = "3819613"
TELEGRAM_CRED = {"id": "YiSf07hq0zNuBX1W", "name": "Telegram account"}
SLACK_CHANNEL_ID = "C0BBAB1G588"
SLACK_CRED = {"id": "rMjfnad1hdCf097y", "name": "Slack account"}
# NOTE: n8n may require setting errorWorkflow via the UI per workflow — the API
# can silently drop it on import. Set it manually after importing if not present.
ERROR_WORKFLOW_ID = "1VcriNGIB4vF6A0u"  # "Pipeline Error Alert" workflow

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
        # /script timeout: gemma4 takes 90-180s normally and longer under GPU
        # contention (with mflux jobs in flight). Backend Ollama timeout is
        # 300s — give n8n 360s headroom so it never beats the backend.
        http_post("do_script",    "Generate script",   900,  f"/{slug}/script",   word_id_expr, 360000),
        http_post("do_voice",     "Generate voice",   1120, f"/{slug}/voice",     word_id_expr,  60000),
        http_post("do_image",     "Generate images",  1340, f"/{slug}/image",     word_id_expr, 7200000),
        http_post("do_assemble",  "Assemble video",   1560, f"/{slug}/assemble",  word_id_expr, 120000),
        http_post("do_upload",    "Upload to YouTube", 1780, f"/{slug}/upload",   upload_expr,  600000),
        # --- success notifications (fan-out after upload) ---
        {
            "id": "format_success",
            "name": "Format success",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [2000, 300],
            "parameters": {
                "mode": "runOnceForAllItems",
                "jsCode": (
                    "const up = $input.first().json || {};\n"
                    "const word = ($('Get next word').item && $('Get next word').item.json) || {};\n"
                    # Telegram sends with parse_mode=HTML, so HTML-escape the dynamic title.
                    # URLs are safe in HTML mode; the original 400 was an unescaped '_' in the
                    # YouTube URL under the node's legacy Markdown default (HTML treats '_'/'*'
                    # as literal, so the video URL no longer opens a phantom entity).
                    "const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');\n"
                    "const title = esc(word.word || word.title || (\"word \" + (up.word_id ?? \"\")));\n"
                    f"return [{{ json: {{ text: `✅ {display_name} — uploaded \"${{title}}\"${{up.url ? \"\\n\" + up.url : \"\"}}` }} }}];"
                ),
            },
        },
        {
            "id": "notify_telegram",
            "name": "Notify success",
            "type": "n8n-nodes-base.telegram",
            "typeVersion": 1.2,
            "position": [2220, 200],
            "parameters": {
                "resource": "message",
                "operation": "sendMessage",
                "chatId": TELEGRAM_CHAT_ID,
                "text": "={{ $json.text }}",
                # parse_mode pinned to HTML: the node's instance default was legacy Markdown,
                # under which a '_' in the YouTube video URL opened an unterminated entity → 400.
                "additionalFields": {"appendAttribution": False, "parse_mode": "HTML"},
            },
            "credentials": {"telegramApi": TELEGRAM_CRED},
        },
        {
            "id": "notify_slack",
            "name": "Notify success (Slack)",
            "type": "n8n-nodes-base.slack",
            "typeVersion": 2.4,
            "position": [2220, 400],
            "parameters": {
                "resource": "message",
                "operation": "post",
                "authentication": "accessToken",
                "select": "channel",
                "channelId": {"__rl": True, "mode": "id", "value": SLACK_CHANNEL_ID},
                "text": "={{ $json.text }}",
                "messageType": "text",
            },
            "credentials": {"slackApi": SLACK_CRED},
        },
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
    connections.update(fanout("Upload to YouTube", "Format success"))
    # Fan-out: Format success → both Telegram and Slack on output index 0
    connections["Format success"] = {
        "main": [[
            {"node": "Notify success", "type": "main", "index": 0},
            {"node": "Notify success (Slack)", "type": "main", "index": 0},
        ]]
    }

    return {
        "name": f"{display_name} — Daily Shorts",
        "active": False,
        # NOTE: n8n may require setting errorWorkflow via the UI per workflow —
        # the API can silently drop it on import. Verify in Settings after import.
        "settings": {
            "executionOrder": "v1",
            "errorWorkflow": ERROR_WORKFLOW_ID,
        },
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
