"""Tests for the script-generation service resilience.

The local Ollama model is non-deterministic and occasionally emits a script
that fails schema validation (most often by dropping `youtube.tags`). The
service must survive that: it retries a few times and passes request context
so the `YouTubeMeta` tag back-fill can repair a droppable field. These tests
pin that behaviour without hitting a real Ollama.
"""

import asyncio
import json

import pytest

import services.script as script_svc
from models import Script, WordRow

WORD = WordRow(
    id=34,
    word="princes in the tower",
    category="disappearance",
    origin_language="1483",
    hook="Two boys vanished from the Tower of London in 1483.",
    status="pending",
    priority=5,
)


def _payload(*, with_tags: bool) -> dict:
    """A valid script dict; `youtube.tags` present or absent per flag."""
    youtube: dict = {
        "title": "The Disappearance of the Princes #shorts",
        "description": "Two boys vanished from the Tower in 1483.\n"
        "#history #mystery #medieval",
    }
    if with_tags:
        youtube["tags"] = ["history", "mystery", "unsolved"]
    return {
        "word": "princes in the tower",
        "pronunciation": "1483",
        "title_text": "PRINCES IN THE TOWER",
        "tagline": "A royal cold case.",
        "beats": [
            {
                "label": "hook",
                "narration": "Two boys entered the Tower and vanished.",
                "on_screen": "Vanished",
                "images": ["a dim medieval tower at dusk"],
            },
            {
                "label": "origin",
                "narration": "Edward the Fifth and his brother were lodged there in 1483.",
                "on_screen": "1483",
                "images": ["two young princes", "a shadowed stone stair"],
            },
            {
                "label": "payoff",
                "narration": "Bones found in 1674 have never been identified.",
                "on_screen": "Unsolved",
                "images": ["a sealed wooden chest"],
            },
        ],
        "youtube": youtube,
    }


@pytest.fixture(autouse=True)
def _stub_prompt(monkeypatch):
    """Avoid reading a real channel prompt file in unit tests."""
    monkeypatch.setattr(script_svc, "_load_system_prompt", lambda channel: "system")


def test_missing_tags_are_repaired_not_fatal(monkeypatch):
    """A response with no `youtube.tags` succeeds on the first attempt via
    the description-hashtag back-fill — no retry, no 500."""
    calls = {"n": 0}

    async def fake_call(system, user):
        calls["n"] += 1
        return json.dumps(_payload(with_tags=False))

    monkeypatch.setattr(script_svc, "_call_ollama", fake_call)

    script = asyncio.run(script_svc.generate_script("open-verdicts", WORD))
    assert isinstance(script, Script)
    assert script.youtube.tags == ["history", "mystery", "medieval"]
    assert calls["n"] == 1


def test_retries_up_to_three_times_before_failing(monkeypatch):
    """Unparseable output is retried three times, then raises."""
    calls = {"n": 0}

    async def fake_call(system, user):
        calls["n"] += 1
        return "this is not json"

    monkeypatch.setattr(script_svc, "_call_ollama", fake_call)

    with pytest.raises(RuntimeError, match="after 3 attempts"):
        asyncio.run(script_svc.generate_script("open-verdicts", WORD))
    assert calls["n"] == 3


def test_recovers_on_a_later_attempt(monkeypatch):
    """A transient bad response followed by a good one still succeeds."""
    responses = ["not json", json.dumps(_payload(with_tags=True))]
    calls = {"n": 0}

    async def fake_call(system, user):
        i = calls["n"]
        calls["n"] += 1
        return responses[i]

    monkeypatch.setattr(script_svc, "_call_ollama", fake_call)

    script = asyncio.run(script_svc.generate_script("open-verdicts", WORD))
    assert script.youtube.tags == ["history", "mystery", "unsolved"]
    assert calls["n"] == 2
