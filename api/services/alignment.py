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
import threading
from dataclasses import dataclass
from pathlib import Path

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
    Raises AlignmentUnavailable on any count < 1 or sum(counts) != len(word_timings).
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


_BUNDLE = None
_MODEL = None
_TOKENIZER = None
_ALIGNER = None
_model_lock = threading.Lock()


def _ensure_model():
    """Lazily load the MMS_FA model/tokenizer/aligner once per process."""
    global _BUNDLE, _MODEL, _TOKENIZER, _ALIGNER
    if _MODEL is not None:
        return
    with _model_lock:
        if _MODEL is not None:
            return
        try:
            import torchaudio

            _BUNDLE = torchaudio.pipelines.MMS_FA
            _MODEL = _BUNDLE.get_model(with_star=False)
            _MODEL.eval()
            _TOKENIZER = _BUNDLE.get_tokenizer()
            _ALIGNER = _BUNDLE.get_aligner()
        except Exception as e:  # import error, download failure, etc.
            raise AlignmentUnavailable(f"could not load MMS_FA model: {e}") from e


def _load_16k_mono(wav_path: Path):
    """Load a WAV and resample to the bundle's 16 kHz mono requirement.

    Uses stdlib `wave` + torch for loading to avoid the torchcodec dependency
    introduced as torchaudio's default backend in 2.9+.
    """
    import wave as _wave

    import torch
    import torchaudio

    with _wave.open(str(wav_path), "rb") as wf:
        sr = wf.getframerate()
        n_ch = wf.getnchannels()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)

    # int16 PCM → float32 in [-1, 1]; copy to get a writable buffer
    data = torch.frombuffer(bytearray(raw), dtype=torch.int16).float() / 32768.0
    waveform = data.reshape(-1, n_ch).t().contiguous()

    if waveform.size(0) > 1:  # downmix to mono
        waveform = waveform.mean(dim=0, keepdim=True)
    target = _BUNDLE.sample_rate
    if sr != target:
        waveform = torchaudio.functional.resample(waveform, sr, target)
    return waveform, target


def forced_word_timings(wav_path: Path, words: list[str]) -> list[WordTiming]:
    """Align `words` (known transcript) to the audio; one WordTiming per word.

    Raises AlignmentUnavailable on any failure so the caller can fall back.
    """
    if not words:
        raise AlignmentUnavailable("empty word list")
    normalized = [normalize_mms_word(w) for w in words]
    if any(nw == "" for nw in normalized):
        bad = [w for w, nw in zip(words, normalized) if nw == ""]
        raise AlignmentUnavailable(f"tokens outside MMS charset: {bad[:5]}")

    _ensure_model()
    import torch

    try:
        waveform, sample_rate = _load_16k_mono(wav_path)
        with torch.inference_mode():
            emission, _ = _MODEL(waveform)
        token_spans = _ALIGNER(emission[0], _TOKENIZER(normalized))
    except AlignmentUnavailable:
        raise
    except Exception as e:
        raise AlignmentUnavailable(f"alignment runtime error: {e}") from e

    if len(token_spans) != len(words):
        raise AlignmentUnavailable(
            f"aligner returned {len(token_spans)} spans for {len(words)} words"
        )

    num_frames = emission.size(1)
    ratio = waveform.size(1) / num_frames / sample_rate
    out: list[WordTiming] = []
    for word, spans in zip(words, token_spans):
        start_s = spans[0].start * ratio
        end_s = spans[-1].end * ratio
        out.append(WordTiming(word=word, start_s=float(start_s), end_s=float(end_s)))
    return out
