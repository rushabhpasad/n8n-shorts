"""Ollama call — generate a structured Script for one word.

Strategy:
- Read the long system prompt once from prompts/script.md (the section after
  the `## System prompt` heading and before `## User-message template`).
- Build the user message from the word row.
- Hit Ollama's /api/chat with `format: "json"` to force JSON-only output.
- Parse + validate against pydantic Script. One retry on parse/schema failure.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import httpx
from pydantic import ValidationError

from config import settings
from models import Script, WordRow

log = logging.getLogger("shorts-api.script")

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
SCRIPT_PROMPT_FILE = PROMPTS_DIR / "script.md"


def _load_system_prompt() -> str:
    """Extract the system-prompt section from prompts/script.md.

    Returns the text between `## System prompt` and `## User-message template`.
    """
    text = SCRIPT_PROMPT_FILE.read_text(encoding="utf-8")
    m = re.search(
        r"##\s+System prompt\s*\n(.*?)\n##\s+User-message template",
        text,
        re.DOTALL,
    )
    if not m:
        raise RuntimeError(
            f"could not find System prompt section in {SCRIPT_PROMPT_FILE}"
        )
    return m.group(1).strip()


def _user_message(word: WordRow) -> str:
    return (
        f"word: {word.word}\n"
        f"category: {word.category}\n"
        f"origin_language: {word.origin_language}\n"
        f"hook: {word.hook}"
    )


async def _call_ollama(system: str, user: str) -> str:
    """Single Ollama /api/chat call. Returns the raw assistant content string."""
    async with httpx.AsyncClient(timeout=settings.ollama_timeout_s) as client:
        resp = await client.post(
            f"{settings.ollama_url}/api/chat",
            json={
                "model": settings.ollama_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "format": "json",
                "stream": False,
                "options": {
                    "temperature": 0.75,
                    "top_p": 0.9,
                },
            },
        )
        resp.raise_for_status()
        body = resp.json()
        return body["message"]["content"]


async def generate_script(word: WordRow) -> Script:
    """Generate and validate one Script. Retries once on parse/schema failure."""
    system = _load_system_prompt()
    user = _user_message(word)

    last_err: Exception | None = None
    for attempt in (1, 2):
        try:
            raw = await _call_ollama(system, user)
            data = json.loads(raw)
            return Script.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as e:
            last_err = e
            log.warning(
                "script gen attempt %d failed (%s): %s",
                attempt,
                type(e).__name__,
                str(e)[:200],
            )

    assert last_err is not None
    raise RuntimeError(f"script generation failed after 2 attempts: {last_err}")
