"""Tests for the Script schema — image prompts are embedded per beat.

Each beat carries its own ordered `images` (diffusion prompts). The flat
`Script.image_prompts` sequence is derived from the beats in order, so prompts
and the images actually shown can never drift out of sync (no orphan renders,
no dangling indices). These tests pin that contract.
"""

import pytest
from pydantic import ValidationError

from models import MAX_IMAGES_TOTAL, MIN_IMAGES_TOTAL, Beat, Script, YouTubeMeta


def _youtube() -> YouTubeMeta:
    return YouTubeMeta(
        title="Dyatlov Pass Mystery Explained",
        description="Nine Soviet hikers died in the Urals in 1959 under unexplained circumstances.",
        tags=["history", "mystery", "coldcase"],
    )


def _script(images_per_beat: list[list[str]]) -> Script:
    """Build a Script whose three beats carry the given image prompts.
    Raises ValidationError if the schema rejects it."""
    labels = ("hook", "origin", "payoff")
    beats = [
        Beat(
            label=labels[i],
            narration=f"{labels[i]} narration text.",
            on_screen=labels[i].title(),
            images=images_per_beat[i],
        )
        for i in range(3)
    ]
    return Script(
        word="dyatlov pass",
        pronunciation="1959",
        title_text="DYATLOV PASS",
        tagline="A cold case.",
        beats=beats,
        youtube=_youtube(),
    )


def test_image_prompts_flattens_beats_in_order():
    script = _script([["a"], ["b", "c"], ["d"]])
    assert script.image_prompts == ["a", "b", "c", "d"]


def test_minimum_total_images_is_ok():
    script = _script([["a"], ["b"], ["c", "d"]])
    assert len(script.image_prompts) == MIN_IMAGES_TOTAL == 4


def test_maximum_total_images_is_ok():
    script = _script([["a", "b", "c"], ["d", "e"], ["f", "g"]])
    assert len(script.image_prompts) == MAX_IMAGES_TOTAL == 7


def test_fewer_than_minimum_total_is_rejected():
    with pytest.raises(ValidationError, match="at least"):
        _script([["a"], ["b"], ["c"]])  # 3 total


def test_more_than_maximum_total_is_rejected():
    with pytest.raises(ValidationError, match="at most"):
        _script([["a", "b", "c"], ["d", "e", "f"], ["g", "h"]])  # 8 total


def test_beat_with_no_images_is_rejected():
    with pytest.raises(ValidationError):
        _script([[], ["a", "b"], ["c", "d"]])


def test_beat_with_more_than_four_images_is_rejected():
    with pytest.raises(ValidationError):
        _script([["a", "b", "c", "d", "e"], ["f"], ["g"]])
