"""Forced alignment of a KNOWN transcript to rendered TTS audio.

We already know exactly what was spoken (the voice-form narration), so this is
forced alignment, not ASR: torchaudio's MMS_FA (wav2vec2 CTC) pipeline places
each known word in time. No transcription, so no hallucinated/misheard words.

Design contract: the caller passes the flat list of voice tokens it ALSO used to
count words per sentence. The aligner returns exactly one span per input word,
so sum(sentence_word_counts) == len(word_timings) by construction. Any deviation
(empty-normalizing token, count mismatch, model failure) raises
AlignmentUnavailable and the caller falls back to proportional timing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_MMS_CHARSET_RE = re.compile(r"[^a-z']")


class AlignmentUnavailable(Exception):
    """Raised when forced alignment cannot run or its output is untrustworthy.

    The caller catches this and falls back to proportional caption timing.
    """


def normalize_mms_word(token: str) -> str:
    """Map a voice-form token to the MMS_FA charset (lowercase a–z plus ').

    Returns "" when nothing survives (e.g. a bare digit normalize_inline did
    not expand) — the caller treats an empty result as a fallback trigger.
    """
    return _MMS_CHARSET_RE.sub("", token.lower())


@dataclass(frozen=True)
class WordTiming:
    word: str
    start_s: float
    end_s: float


def map_words_to_sentences(
    word_timings: list[WordTiming], counts: list[int]
) -> list[tuple[float, float]]:
    """Group consecutive word timings into per-sentence (start_s, end_s) spans.

    `counts[i]` is sentence i's voice-word count; spans are assigned in order.
    Raises AlignmentUnavailable on any count < 1 or sum(counts) != len(words).
    """
    if any(c < 1 for c in counts):
        raise AlignmentUnavailable(f"non-positive sentence word count in {counts}")
    if sum(counts) != len(word_timings):
        raise AlignmentUnavailable(
            f"word/sentence mismatch: {len(word_timings)} words vs sum {sum(counts)}"
        )
    spans: list[tuple[float, float]] = []
    cursor = 0
    for c in counts:
        start = word_timings[cursor].start_s
        end = word_timings[cursor + c - 1].end_s
        spans.append((start, end))
        cursor += c
    return spans
