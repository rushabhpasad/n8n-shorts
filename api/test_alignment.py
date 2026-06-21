import pytest

from services.alignment import (
    AlignmentUnavailable,
    WordTiming,
    map_words_to_sentences,
    normalize_mms_word,
)


def test_lowercases_and_strips_punctuation():
    assert normalize_mms_word("Word,") == "word"
    assert normalize_mms_word("(origin)") == "origin"


def test_keeps_apostrophe():
    assert normalize_mms_word("don't") == "don't"


def test_strips_hyphen_to_single_token():
    # num2words year form for 1888 is "eighty-eight"; one aligner word.
    assert normalize_mms_word("eighty-eight") == "eightyeight"


def test_bare_digit_normalizes_to_empty():
    # Not expanded by normalize_inline; signals the caller to fall back.
    assert normalize_mms_word("5") == ""
    assert normalize_mms_word("—") == ""


def _wt(seq):
    # seq: list of (start, end) → WordTiming list with dummy words
    return [WordTiming(word=f"w{i}", start_s=s, end_s=e) for i, (s, e) in enumerate(seq)]


def test_maps_counts_to_first_and_last_word_spans():
    words = _wt([(0.0, 0.5), (0.5, 1.0), (1.0, 1.4), (1.4, 2.0)])
    # two sentences: 1 word, then 3 words
    spans = map_words_to_sentences(words, [1, 3])
    assert spans == [(0.0, 0.5), (0.5, 2.0)]


def test_single_sentence_spans_all_words():
    words = _wt([(0.2, 0.6), (0.6, 1.1)])
    assert map_words_to_sentences(words, [2]) == [(0.2, 1.1)]


def test_count_mismatch_raises():
    words = _wt([(0.0, 0.5), (0.5, 1.0)])
    with pytest.raises(AlignmentUnavailable):
        map_words_to_sentences(words, [1, 1, 1])  # sum 3 != 2


def test_zero_count_raises():
    words = _wt([(0.0, 0.5)])
    with pytest.raises(AlignmentUnavailable):
        map_words_to_sentences(words, [0, 1])


import os
import wave
import struct
import math
from pathlib import Path

_RUN_MODEL = os.environ.get("RUN_ALIGNMENT_MODEL") == "1"


def _write_sine_wav(path: Path, seconds: float, rate: int = 24000):
    n = int(seconds * rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", int(3000 * math.sin(2 * math.pi * 220 * t / rate)))
            for t in range(n)
        )
        w.writeframes(frames)


@pytest.mark.skipif(not _RUN_MODEL, reason="set RUN_ALIGNMENT_MODEL=1 to run the real model")
def test_forced_word_timings_returns_one_span_per_word(tmp_path):
    from services.alignment import forced_word_timings

    wav = tmp_path / "x.wav"
    _write_sine_wav(wav, seconds=2.0)
    words = ["hello", "world", "again"]
    timings = forced_word_timings(wav, words)
    assert len(timings) == len(words)
    assert all(t.end_s >= t.start_s for t in timings)
    assert all(0.0 <= t.start_s <= 2.01 for t in timings)


def test_empty_normalizing_word_raises(tmp_path):
    from services.alignment import forced_word_timings

    wav = tmp_path / "x.wav"
    _write_sine_wav(wav, seconds=0.5)
    with pytest.raises(AlignmentUnavailable):
        forced_word_timings(wav, ["the", "5", "forms"])  # "5" → "" → abort
