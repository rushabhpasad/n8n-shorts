"""Tests for the Script schema — focused on image-index coverage rules."""

import pytest
from pydantic import ValidationError

from models import Beat, Script, YouTubeMeta


def _youtube() -> YouTubeMeta:
    return YouTubeMeta(
        title="Dyatlov Pass Mystery Explained",
        description="Nine Soviet hikers died in the Urals in 1959 under unexplained circumstances.",
        tags=["history", "mystery", "coldcase"],
    )


def _script(idxs: list[list[int]], n_prompts: int) -> Script:
    """Build a Script with the three beats carrying the given image_idxs and
    `n_prompts` image prompts. Raises ValidationError if the schema rejects it."""
    labels = ("hook", "origin", "payoff")
    beats = [
        Beat(label=labels[i], narration=f"{labels[i]} narration text.",
             on_screen=labels[i].title(), image_idxs=idxs[i])
        for i in range(3)
    ]
    return Script(
        word="dyatlov pass",
        pronunciation="1959",
        title_text="DYATLOV PASS",
        tagline="A cold case.",
        beats=beats,
        image_prompts=[f"prompt {i}" for i in range(n_prompts)],
        youtube=_youtube(),
    )


def test_orphan_prompt_is_allowed():
    # 6 prompts generated, only indices 0..4 referenced (index 5 orphaned).
    # The old strict-permutation rule rejected this; the relaxed rule accepts it.
    script = _script([[0, 1], [2, 3], [4]], n_prompts=6)
    assert len(script.image_prompts) == 6


def test_repeated_index_is_allowed():
    # An image may appear in more than one beat.
    script = _script([[0, 1], [2, 3], [0]], n_prompts=4)
    assert script is not None


def test_exactly_four_distinct_is_ok():
    script = _script([[0, 1], [2], [3]], n_prompts=4)
    assert len({i for b in script.beats for i in b.image_idxs}) == 4


def test_fewer_than_four_distinct_is_rejected():
    with pytest.raises(ValidationError, match="distinct"):
        _script([[0], [1], [2]], n_prompts=4)


def test_out_of_range_index_is_rejected():
    with pytest.raises(ValidationError, match="non-existent"):
        _script([[0, 1], [2, 3], [9]], n_prompts=4)
