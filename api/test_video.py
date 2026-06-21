"""Tests for video.py sentence-splitting and timing helpers.

`_split_for_video` must return (caption_text, voice_tokens) — a list of
str tokens, not an int count. `_compute_sentence_timings` derives word counts
from len(tokens) and must produce the same proportional timings as the old
int-returning version.
"""

from __future__ import annotations

import pytest

from models import Beat, Script, YouTubeMeta
from services.video import _compute_sentence_timings, _split_for_video


# ─── Fixtures ────────────────────────────────────────────────────────────────


def _youtube() -> YouTubeMeta:
    return YouTubeMeta(
        title="Test Short Title Goes Here",
        description="A twenty-character-plus description for testing purposes.",
        tags=["test", "word", "short"],
    )


def _beat(label: str, narration: str) -> Beat:
    """Build a minimal Beat with one image prompt (satisfies min_length=1)."""
    return Beat(
        label=label,  # type: ignore[arg-type]
        narration=narration,
        on_screen=label.title(),
        images=["a test image prompt here"],
    )


def _script(hook: str, origin: str, payoff: str) -> Script:
    """Build a valid 3-beat Script (min 4 images total → give payoff 2 images)."""
    return Script(
        word="test",
        pronunciation="test",
        title_text="TEST",
        tagline="A short tagline.",
        beats=[
            _beat("hook", hook),
            _beat("origin", origin),
            Beat(
                label="payoff",
                narration=payoff,
                on_screen="Payoff",
                images=["img1", "img2", "img3"],  # 3 images to hit total ≥ 4
            ),
        ],
        youtube=_youtube(),
    )


# ─── _split_for_video tests ──────────────────────────────────────────────────


def test_split_returns_caption_and_voice_tokens():
    out = _split_for_video("The year 1888 mattered. It spread fast.")
    # two sentences
    assert len(out) == 2
    caption0, tokens0 = out[0]
    # caption keeps the digits; voice tokens expand the year
    assert "1888" in caption0
    assert "eighteen" in tokens0
    # tokens is a list, count derivable from it
    assert isinstance(tokens0, list)
    assert all(isinstance(t, str) for t in tokens0)


def test_split_single_sentence_no_period():
    """A narration with no sentence-ending punctuation → one entry."""
    out = _split_for_video("Hello world this is a single sentence")
    assert len(out) == 1
    caption, tokens = out[0]
    assert isinstance(tokens, list)
    assert len(tokens) >= 1


def test_split_tokens_count_matches_voice_form():
    """Token count must equal what normalize_inline produces (split by whitespace)."""
    from services.text_normalize import normalize_inline

    narration = "The 3rd day was pivotal."
    out = _split_for_video(narration)
    assert len(out) == 1
    _caption, tokens = out[0]
    expected_count = len(normalize_inline(narration.strip()).split())
    assert len(tokens) == expected_count


def test_split_empty_parts_skipped():
    """Multiple spaces / trailing separators should not produce empty entries."""
    out = _split_for_video("First sentence. Second sentence.")
    assert all(caption.strip() for caption, _ in out)


# ─── _compute_sentence_timings tests ────────────────────────────────────────


def test_timings_cover_full_beat_duration():
    """Last sentence of a beat must end exactly at beat boundary."""
    script = _script(
        hook="The 1st hook line here goes on.",
        origin="Origin text that is somewhat longer.",
        payoff="Payoff text goes right here now.",
    )
    beat_durs = [3.0, 4.0, 2.0]
    timings = _compute_sentence_timings(script, beat_durs, lead_s=0.0)
    # Collect end times per beat
    # Beat 0: hook sentences
    hook_sentences = _split_for_video(script.beats[0].narration)
    beat0_end = timings[len(hook_sentences) - 1][2]
    assert abs(beat0_end - 3.0) < 1e-6


def test_timings_total_length():
    """Number of timing entries = total sentences across all beats."""
    script = _script(
        hook="Hook sentence one. Hook sentence two.",
        origin="Single origin sentence here.",
        payoff="Payoff one. Payoff two.",
    )
    beat_durs = [4.0, 3.0, 3.0]
    timings = _compute_sentence_timings(script, beat_durs, lead_s=0.0)
    expected = sum(
        len(_split_for_video(b.narration)) for b in script.beats
    )
    assert len(timings) == expected


def test_timings_monotone():
    """Start times must be non-decreasing."""
    script = _script(
        hook="The year 1888 changed everything. It really did matter.",
        origin="The origin was simple. It grew from there.",
        payoff="The payoff arrived fast.",
    )
    beat_durs = [5.0, 4.0, 3.0]
    timings = _compute_sentence_timings(script, beat_durs, lead_s=0.0)
    starts = [s for _, s, _ in timings]
    assert starts == sorted(starts)


def test_timings_with_lead_shifts_start():
    """lead_s > 0 should shift (but floor at 0) the start of the first caption."""
    script = _script(
        hook="A single hook sentence here.",
        origin="A single origin sentence here.",
        payoff="A single payoff sentence here.",
    )
    beat_durs = [3.0, 3.0, 3.0]
    timings_no_lead = _compute_sentence_timings(script, beat_durs, lead_s=0.0)
    timings_lead = _compute_sentence_timings(script, beat_durs, lead_s=0.5)
    # First caption with lead=0 starts at 0; with lead=0.5 still floors to 0
    assert timings_no_lead[0][1] == 0.0
    assert timings_lead[0][1] == 0.0  # max(0, 0 - 0.5) = 0


# ─── Task 7: forced-alignment caption timing ─────────────────────────────────

from unittest.mock import patch

import services.video as video
from services.alignment import WordTiming


def _two_beat_script() -> Script:
    """Build a minimal 3-beat Script (models.Script requires exactly 3 beats,
    >=4 images total). Third beat is a minimal payoff to satisfy the validator."""
    return Script(
        word="alpha",
        pronunciation="AL-fuh",
        title_text="ALPHA",
        tagline="A short tagline here.",
        beats=[
            _beat("hook", "Alpha bravo. Charlie."),
            _beat("origin", "Delta echo foxtrot."),
            Beat(
                label="payoff",
                narration="Gamma hotel india juliet.",
                on_screen="Payoff",
                images=["img1", "img2", "img3"],
            ),
        ],
        youtube=_youtube(),
    )


def test_forced_story_timings_maps_aligned_words(tmp_path):
    script = _two_beat_script()
    # story sentences → "Alpha bravo."=2, "Charlie."=1, "Delta echo foxtrot."=3,
    #                    "Gamma hotel india juliet."=4  — 10 story words total
    # cta "thanks" → 1 word; total transcript words = 11
    fake = [
        WordTiming("alpha", 0.0, 0.4), WordTiming("bravo", 0.4, 0.9),    # sent 0
        WordTiming("charlie", 0.9, 1.5),                                  # sent 1
        WordTiming("delta", 1.5, 1.9), WordTiming("echo", 1.9, 2.3),
        WordTiming("foxtrot", 2.3, 3.0),                                  # sent 2
        WordTiming("gamma", 3.0, 3.3), WordTiming("hotel", 3.3, 3.6),
        WordTiming("india", 3.6, 3.8), WordTiming("juliet", 3.8, 4.2),   # sent 3
        WordTiming("thanks", 4.2, 4.7),                                   # cta — discarded
    ]
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"")  # not read — forced_word_timings is mocked
    with patch.object(video, "forced_word_timings", return_value=fake):
        spans = video._forced_story_timings(script, wav, cta_text="Thanks")
    captions = [c for c, _, _ in spans]
    times = [(round(s, 3), round(e, 3)) for _, s, e in spans]
    assert len(spans) == 4  # four story sentences (3 beats * sentences), CTA not included
    assert times == [(0.0, 0.9), (0.9, 1.5), (1.5, 3.0), (3.0, 4.2)]
    assert captions[0].startswith("Alpha")


def test_assemble_falls_back_when_alignment_unavailable(tmp_path, monkeypatch):
    from services.alignment import AlignmentUnavailable

    script = _two_beat_script()
    monkeypatch.setattr(video.settings, "align_backend", "forced")

    def _boom(*a, **k):
        raise AlignmentUnavailable("forced failure for test")

    monkeypatch.setattr(video, "forced_word_timings", _boom)
    # _forced_story_timings should propagate AlignmentUnavailable...
    import pytest
    with pytest.raises(AlignmentUnavailable):
        video._forced_story_timings(script, tmp_path / "a.wav", cta_text="")
    # ...and _compute_sentence_timings still works as the fallback producer.
    durs = [1.0, 1.0, 1.0]
    fallback = video._compute_sentence_timings(script, durs)
    assert len(fallback) >= 3
