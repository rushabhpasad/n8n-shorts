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


# --- YouTubeMeta.tags back-fill --------------------------------------------
# The local Ollama model sometimes drops the required `tags` array and instead
# writes hashtags inline in the description (this exact miss 500'd a live run).
# A `mode="before"` validator repairs that so a stray formatting choice never
# fails the whole pipeline. These tests pin the repair contract.


def test_valid_tags_are_left_untouched():
    meta = YouTubeMeta(
        title="Dyatlov Pass Mystery Explained",
        description="Nine hikers died in the Urals in 1959.",
        tags=["history", "mystery", "coldcase"],
    )
    assert meta.tags == ["history", "mystery", "coldcase"]


def test_missing_tags_backfilled_from_description_hashtags():
    meta = YouTubeMeta.model_validate(
        {
            "title": "The Disappearance of the Princes",
            "description": "Two boys vanished from the Tower in 1483.\n"
            "#history #mystery #medieval",
        }
    )
    assert meta.tags == ["history", "mystery", "medieval"]


def test_backfill_dedupes_and_strips_hash_prefixes():
    meta = YouTubeMeta.model_validate(
        {
            "title": "A Ten Char Title",
            "description": "Summary text here.\n#History #history #mystery #cold #cold",
            "tags": ["#History"],  # single hashy tag, below the min
        }
    )
    # existing "History" kept (# stripped); description adds mystery + cold;
    # case-insensitive dedupe drops the repeats.
    assert meta.tags == ["History", "mystery", "cold"]


def test_backfill_from_context_when_no_hashtags():
    # No hashtags in the description and no tags -> derive from the request word.
    meta = YouTubeMeta.model_validate(
        {
            "title": "The Princes In The Tower",
            "description": "A plain factual summary with no hashtags at all.",
        },
        context={"word": "princes in the tower", "category": "disappearance"},
    )
    assert len(meta.tags) >= 3
    assert "princes" in meta.tags and "disappearance" in meta.tags


def test_backfill_caps_at_fifteen():
    hashtags = " ".join(f"#tag{i}" for i in range(20))
    meta = YouTubeMeta.model_validate(
        {"title": "Twenty Hashtags Here", "description": f"Summary.\n{hashtags}"}
    )
    assert len(meta.tags) == 15


def test_unrepairable_tags_still_raise():
    # No tags, no hashtags, no context -> genuine validation failure surfaces.
    with pytest.raises(ValidationError):
        YouTubeMeta.model_validate(
            {
                "title": "No Tags Anywhere",
                "description": "A summary with absolutely no hashtags to harvest.",
            }
        )
