# Forced-Alignment Captions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace proportional (word-count-estimated) caption timing with acoustic forced alignment so each sentence caption's on-screen window matches when the narrator actually speaks it, eliminating the current ~200–400 ms trail.

**Architecture:** We already know the spoken transcript (the voice-form narration), so this is *forced alignment*, not transcription — no Whisper ASR. We use `torchaudio`'s `MMS_FA` (wav2vec2 CTC) pipeline to align the known transcript to the rendered WAV and read true per-word `(start, end)` spans. A new `api/services/alignment.py` owns model loading and alignment; `video.py` builds a flat voice-token list from the *same* per-sentence tokenization it already uses for word counts (so the word↔sentence index mapping is exact by construction), maps aligned word spans back to sentences, and uses those timings in the existing ffmpeg `overlay ... enable='between(t,start,end)'` chain. A config flag (`align_backend`) gates the feature; any alignment failure falls back to the existing proportional path, mirroring the Kokoro→Piper fallback pattern.

**Tech Stack:** Python 3.13, `torch` + `torchaudio` (CPU), `torchaudio.pipelines.MMS_FA`, FastAPI, Pillow, ffmpeg, pytest.

## Global Constraints

- **No behavior change on merge.** `align_backend` defaults to `"proportional"`. Forced alignment is enabled only by flipping the flag on the deployment host after `torch`/`torchaudio` are installed and a clip is validated by ear.
- **Graceful degradation is mandatory.** Any failure in the forced path (import error, model download failure, charset mismatch, word-count mismatch, runtime exception) logs a warning and falls back to the existing proportional timings for that clip. A clip must never fail to assemble because of alignment.
- **The word↔sentence mapping must be exact by construction**, never fuzzy string matching: the flat token list fed to the aligner is the concatenation of the *same* per-sentence `normalize_inline(...).split()` tokens used for sentence word counts. `sum(sentence_word_counts) == len(aligned_words)` is an invariant, asserted at runtime.
- **CTA caption stays pinned** to `[story_dur, total_s]` on the outro card — it is intentionally not word-synced. The full transcript (story + CTA) is aligned so the transcript matches the full audio, but only story-sentence spans are consumed; CTA token spans are discarded.
- **Image segment timing is unchanged** — images remain proportionally timed per beat. Alignment only affects caption windows.
- `torchaudio` `MMS_FA` requires **16 kHz mono** waveform; TTS output is 22.05/24 kHz. Resample for alignment only; the final mux still uses the original WAV.
- Target macOS arm64 (local + `stl`); CPU-only inference. No GPU.
- Follow `~/.claude/rules/general-rules.md`: small focused files (<600 lines), strict separation (alignment logic in a service, not in routes), named exports, tests in the same PR, conventional commits.

---

## File Structure

- **Create `api/services/alignment.py`** — owns: lazy model/tokenizer/aligner singleton; `forced_word_timings(wav_path, words)`; pure `map_words_to_sentences(word_timings, counts)`; `normalize_mms_word(token)`; `WordTiming` dataclass; `AlignmentUnavailable` exception. One responsibility: turn (wav, known words) into validated per-word/per-sentence timings.
- **Create `api/test_alignment.py`** — unit tests for the pure functions (`normalize_mms_word`, `map_words_to_sentences`) and a default-skipped integration test that runs the real model on a generated WAV.
- **Modify `api/config.py`** — add `align_backend: Literal["proportional", "forced"]` flag (default `"proportional"`); update the `caption_lead_s` comment.
- **Modify `api/services/video.py`** — refactor `_split_for_video` to return voice *tokens* (not just a count); add `_forced_story_timings(...)`; branch in `assemble_video` to prefer forced timings with proportional fallback.
- **Modify `api/test_video.py`** — does not exist yet; create it for the new `video.py` helpers (token split + forced-timing assembly logic with a mocked aligner).
- **Modify `api/pyproject.toml`** — add `torch` + `torchaudio` deps.
- **Modify `AGENTS.md` / `README.md`** — document the flag, the new dependency, and the `stl` enablement step.

---

### Task 1: Add `torch` / `torchaudio` dependencies

**Files:**
- Modify: `api/pyproject.toml` (dependencies array)

**Interfaces:**
- Consumes: nothing.
- Produces: `import torch`, `import torchaudio` available in the `api/.venv`.

- [ ] **Step 1: Add the dependencies**

In `api/pyproject.toml`, inside the `dependencies = [ ... ]` array, after the `piper-tts` entry, add:

```toml
    # Forced alignment of the known transcript to rendered audio (caption sync).
    # CPU-only inference via torchaudio's MMS_FA wav2vec2 CTC pipeline.
    "torch>=2.2",
    "torchaudio>=2.2",
```

- [ ] **Step 2: Install and verify the imports resolve**

Run:
```bash
cd api && uv sync && uv run python -c "import torch, torchaudio; print(torch.__version__, torchaudio.__version__)"
```
Expected: prints two version strings (e.g. `2.x.x 2.x.x`), no `ModuleNotFoundError`.

- [ ] **Step 3: Verify the MMS_FA bundle is reachable (one-time model download)**

Run:
```bash
cd api && uv run python -c "import torchaudio; b=torchaudio.pipelines.MMS_FA; print(b.sample_rate); b.get_model(with_star=False); print('model ok')"
```
Expected: prints `16000` then `model ok` (downloads the model to the torchaudio cache on first run; needs network).

- [ ] **Step 4: Commit**

```bash
git add api/pyproject.toml api/uv.lock
git commit -m "build(api): add torch/torchaudio for forced-alignment captions"
```

---

### Task 2: Add the `align_backend` config flag

**Files:**
- Modify: `api/config.py:107-111` (the `caption_lead_s` block)

**Interfaces:**
- Consumes: nothing.
- Produces: `settings.align_backend: Literal["proportional", "forced"]`.

- [ ] **Step 1: Add the flag and update the lead comment**

In `api/config.py`, replace the existing `caption_lead_s` block (the comment plus the field) with:

```python
    # Caption-vs-audio sync strategy.
    #   "proportional" — split each beat's audio duration across sentences by
    #                    voice-form word count (estimate; trails ~200–400 ms).
    #   "forced"       — torchaudio MMS_FA forced alignment of the known
    #                    transcript to the rendered WAV (true per-word timing).
    #                    Falls back to "proportional" on any alignment failure.
    align_backend: Literal["proportional", "forced"] = Field(default="proportional")

    # Only used by the "proportional" backend: shift each caption's start window
    # earlier by this much if captions visibly trail the voice. The "forced"
    # backend makes this unnecessary (and ignores it).
    caption_lead_s: float = Field(default=0.0)
```

Confirm `Literal` is already imported in `config.py` (it is — `voice_backend` uses it). If not, add `from typing import Literal`.

- [ ] **Step 2: Verify config loads**

Run:
```bash
cd api && uv run python -c "from config import settings; print(settings.align_backend)"
```
Expected: prints `proportional`.

- [ ] **Step 3: Commit**

```bash
git add api/config.py
git commit -m "feat(api): add align_backend config flag (default proportional)"
```

---

### Task 3: `normalize_mms_word` — map a voice token to the MMS_FA charset

**Files:**
- Create: `api/services/alignment.py`
- Test: `api/test_alignment.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `normalize_mms_word(token: str) -> str` (lowercase, keeps only `[a-z']`, returns `""` when nothing survives). `AlignmentUnavailable(Exception)`.

The MMS_FA dictionary is lowercase `a–z` plus apostrophe. Our voice tokens come from `normalize_inline(...).split()`, so years/ordinals are already spelled out (e.g. `"eighteen"`, `"eighty-eight"`). We strip to the model charset. A token that normalizes to empty (e.g. a bare digit `"5"` that `normalize_inline` did not expand) would break the exact 1:1 mapping, so we surface it as empty and let the caller abort to the proportional fallback rather than silently dropping a word.

- [ ] **Step 1: Write the failing test**

Create `api/test_alignment.py`:

```python
from services.alignment import normalize_mms_word


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && uv run pytest test_alignment.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.alignment'`.

- [ ] **Step 3: Write minimal implementation**

Create `api/services/alignment.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd api && uv run pytest test_alignment.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add api/services/alignment.py api/test_alignment.py
git commit -m "feat(api): add MMS_FA token normalizer for forced alignment"
```

---

### Task 4: `map_words_to_sentences` — pure word-span → sentence-span mapping

**Files:**
- Modify: `api/services/alignment.py`
- Test: `api/test_alignment.py`

**Interfaces:**
- Consumes: `WordTiming` (defined here).
- Produces:
  - `@dataclass(frozen=True) class WordTiming: word: str; start_s: float; end_s: float`
  - `map_words_to_sentences(word_timings: list[WordTiming], counts: list[int]) -> list[tuple[float, float]]` — sentence *i* spans word indices `[a, b)` where `b-a == counts[i]`; its span is `(word_timings[a].start_s, word_timings[b-1].end_s)`. Raises `AlignmentUnavailable` if `sum(counts) != len(word_timings)` or any count `< 1`.

This is the load-bearing function — a silent off-by-one here desyncs every caption — so it is pure and exhaustively tested.

- [ ] **Step 1: Write the failing test**

Append to `api/test_alignment.py`:

```python
import pytest

from services.alignment import (
    AlignmentUnavailable,
    WordTiming,
    map_words_to_sentences,
)


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && uv run pytest test_alignment.py -v`
Expected: FAIL with `ImportError: cannot import name 'WordTiming'`.

- [ ] **Step 3: Write minimal implementation**

In `api/services/alignment.py`, add the import and definitions:

```python
from dataclasses import dataclass


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
```

Add `from dataclasses import dataclass` near the top imports.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd api && uv run pytest test_alignment.py -v`
Expected: PASS (8 passed total).

- [ ] **Step 5: Commit**

```bash
git add api/services/alignment.py api/test_alignment.py
git commit -m "feat(api): add pure word-span to sentence-span mapper"
```

---

### Task 5: `forced_word_timings` — run MMS_FA and return validated per-word timings

**Files:**
- Modify: `api/services/alignment.py`
- Test: `api/test_alignment.py` (default-skipped integration test)

**Interfaces:**
- Consumes: `normalize_mms_word`, `WordTiming`, `AlignmentUnavailable`.
- Produces: `forced_word_timings(wav_path: Path, words: list[str]) -> list[WordTiming]` — returns exactly `len(words)` timings in order, or raises `AlignmentUnavailable`. Loads the model once (process-lifetime singleton).

Timing math (per the torchaudio MMS_FA tutorial): the model emits `T` frames over the whole waveform; seconds-per-frame `= waveform_samples / num_frames / sample_rate`. A word's span is `aligner` token-spans `[0].start` → `[-1].end` scaled by that ratio.

- [ ] **Step 1: Write the failing (skipped-by-default) integration test**

Append to `api/test_alignment.py`:

```python
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
```

(The second test runs without the model — the empty-word guard fires before any model call.)

- [ ] **Step 2: Run tests to verify the new one fails**

Run: `cd api && uv run pytest test_alignment.py::test_empty_normalizing_word_raises -v`
Expected: FAIL with `ImportError: cannot import name 'forced_word_timings'`.

- [ ] **Step 3: Write the implementation**

In `api/services/alignment.py`, add:

```python
import logging
from pathlib import Path

log = logging.getLogger("shorts-api.alignment")

_BUNDLE = None
_MODEL = None
_TOKENIZER = None
_ALIGNER = None


def _ensure_model():
    """Lazily load the MMS_FA model/tokenizer/aligner once per process."""
    global _BUNDLE, _MODEL, _TOKENIZER, _ALIGNER
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
    """Load a WAV and resample to the bundle's 16 kHz mono requirement."""
    import torch
    import torchaudio

    waveform, sr = torchaudio.load(str(wav_path))
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
```

Move the `import logging` / `from pathlib import Path` to the existing import block at the top of the file (keep imports grouped, not mid-file).

- [ ] **Step 4: Run the non-model test to verify it passes**

Run: `cd api && uv run pytest test_alignment.py::test_empty_normalizing_word_raises -v`
Expected: PASS.

- [ ] **Step 5: Run the real-model integration test once, manually**

Run: `cd api && RUN_ALIGNMENT_MODEL=1 uv run pytest test_alignment.py::test_forced_word_timings_returns_one_span_per_word -v`
Expected: PASS (downloads model on first run; takes a few seconds).

- [ ] **Step 6: Commit**

```bash
git add api/services/alignment.py api/test_alignment.py
git commit -m "feat(api): MMS_FA forced word-timing with graceful fallback contract"
```

---

### Task 6: Refactor `_split_for_video` to return voice tokens

**Files:**
- Modify: `api/services/video.py:81-121` (`_split_for_video` and its caller `_compute_sentence_timings`)
- Test: `api/test_video.py` (create)

**Interfaces:**
- Consumes: `normalize_for_caption`, `normalize_inline`.
- Produces: `_split_for_video(narration: str) -> list[tuple[str, list[str]]]` — now returns `(caption_text, voice_tokens)` per sentence (was `(caption_text, voice_word_count)`). Word count is derived as `len(voice_tokens)`. This lets Task 7 build the aligner's flat token list from the exact same tokenization, guaranteeing the count invariant.

- [ ] **Step 1: Write the failing test**

Create `api/test_video.py`:

```python
from services.video import _split_for_video, _compute_sentence_timings
from models import Script, Beat  # adjust import if Beat lives elsewhere


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && uv run pytest test_video.py -v`
Expected: FAIL — current `_split_for_video` returns `(caption, int)`, so `"eighteen" in tokens0` fails (`in` on an int raises `TypeError`).

- [ ] **Step 3: Implement the refactor**

In `api/services/video.py`, replace `_split_for_video` (lines ~81–95) with:

```python
def _split_for_video(narration: str) -> list[tuple[str, list[str]]]:
    """Split narration into sentences. Returns (caption_text, voice_tokens) per
    sentence. Caption preserves digits/ordinals as written; voice_tokens are the
    fully-expanded spoken form (what Piper actually says) — their count gates
    proportional sentence durations, and the tokens themselves feed forced
    alignment so the word↔sentence mapping is exact by construction."""
    parts = _SENT_SPLIT_RE.split(narration.strip())
    out: list[tuple[str, list[str]]] = []
    for p in parts:
        if not p.strip():
            continue
        caption = normalize_for_caption(p.strip())
        tokens = normalize_inline(p.strip()).split() or [p.strip()]
        out.append((caption, tokens))
    return out
```

Then update `_compute_sentence_timings` (lines ~98–121) to derive counts from token lists. Replace its body with:

```python
def _compute_sentence_timings(
    script: Script,
    beat_durations: list[float],
    lead_s: float = 0.0,
) -> list[tuple[str, float, float]]:
    out: list[tuple[str, float, float]] = []
    beat_start = 0.0
    for i, beat in enumerate(script.beats):
        sentences = _split_for_video(beat.narration)
        if not sentences:
            sentences = [(normalize_for_caption(beat.on_screen), beat.on_screen.split() or ["x"])]
        counts = [max(1, len(toks)) for _, toks in sentences]
        total_words = sum(counts)
        beat_dur = beat_durations[i]
        cursor = beat_start
        for j, ((caption, _toks), wc) in enumerate(zip(sentences, counts)):
            if j == len(sentences) - 1:
                end = beat_start + beat_dur
            else:
                end = cursor + beat_dur * (wc / total_words)
            shifted_start = max(0.0, cursor - lead_s)
            out.append((caption, round(shifted_start, 3), round(end, 3)))
            cursor = end
        beat_start += beat_dur
    return out
```

- [ ] **Step 4: Run the new test plus the existing suite to confirm no regression**

Run: `cd api && uv run pytest test_video.py -v && uv run pytest -q`
Expected: `test_video.py` PASS; full suite shows no new failures vs. baseline.

- [ ] **Step 5: Commit**

```bash
git add api/services/video.py api/test_video.py
git commit -m "refactor(api): _split_for_video returns voice tokens for alignment reuse"
```

---

### Task 7: Wire forced timings into `assemble_video` with proportional fallback

**Files:**
- Modify: `api/services/video.py` — add `_forced_story_timings(...)`; branch in `assemble_video` (lines ~420–427).
- Test: `api/test_video.py`

**Interfaces:**
- Consumes: `alignment.forced_word_timings`, `alignment.map_words_to_sentences`, `alignment.AlignmentUnavailable`, refactored `_split_for_video`.
- Produces: `_forced_story_timings(script: Script, audio_path: Path, cta_text: str) -> list[tuple[str, float, float]]` — story-sentence `(caption, start_s, end_s)` from forced alignment; raises `AlignmentUnavailable` on any failure. `assemble_video` prefers it when `settings.align_backend == "forced"`, else/at failure uses `_compute_sentence_timings`.

The full transcript (story + CTA) is aligned so it matches the full audio; only the leading story spans are mapped to sentences. The CTA caption is appended afterward, pinned to `[story_dur, total_s]` exactly as today.

- [ ] **Step 1: Write the failing test (aligner mocked — no model needed)**

Append to `api/test_video.py`:

```python
from unittest.mock import patch
from pathlib import Path

import services.video as video
from services.alignment import WordTiming


def _two_beat_script():
    # Build a Script with beats whose narration yields known sentence counts.
    # Adjust constructor to match models.Script/Beat signature.
    beats = [
        Beat(narration="Alpha bravo. Charlie.", on_screen="x", images=["i"], image_prompts=["p"]),
        Beat(narration="Delta echo foxtrot.", on_screen="y", images=["j"], image_prompts=["q"]),
    ]
    return Script(title_text="WORD", beats=beats)


def test_forced_story_timings_maps_aligned_words(tmp_path):
    script = _two_beat_script()
    # story sentences → counts: ["Alpha bravo."]=2, ["Charlie."]=1, ["Delta echo foxtrot."]=3
    # cta "thanks" → 1 word; total transcript words = 7
    fake = [
        WordTiming("alpha", 0.0, 0.4), WordTiming("bravo", 0.4, 0.9),   # sentence 0
        WordTiming("charlie", 0.9, 1.5),                                 # sentence 1
        WordTiming("delta", 1.5, 1.9), WordTiming("echo", 1.9, 2.3),
        WordTiming("foxtrot", 2.3, 3.0),                                 # sentence 2
        WordTiming("thanks", 3.0, 3.5),                                  # cta — discarded
    ]
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"")  # not read — forced_word_timings is mocked
    with patch.object(video, "forced_word_timings", return_value=fake):
        spans = video._forced_story_timings(script, wav, cta_text="Thanks")
    captions = [c for c, _, _ in spans]
    times = [(round(s, 3), round(e, 3)) for _, s, e in spans]
    assert len(spans) == 3  # three story sentences, CTA not included
    assert times == [(0.0, 0.9), (0.9, 1.5), (1.5, 3.0)]
    assert captions[0].startswith("Alpha")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && uv run pytest test_video.py::test_forced_story_timings_maps_aligned_words -v`
Expected: FAIL with `AttributeError: module 'services.video' has no attribute '_forced_story_timings'`.

- [ ] **Step 3: Implement `_forced_story_timings` and the branch**

In `api/services/video.py`, add the import near the other service imports:

```python
from services.alignment import (
    AlignmentUnavailable,
    forced_word_timings,
    map_words_to_sentences,
)
```

Add the helper (place it after `_compute_sentence_timings`):

```python
def _forced_story_timings(
    script: Script, audio_path: Path, cta_text: str
) -> list[tuple[str, float, float]]:
    """Story-sentence (caption, start_s, end_s) from forced alignment.

    Aligns the FULL spoken transcript (story + CTA) so it matches the full
    audio, then maps only the leading story-word spans onto story sentences.
    The CTA caption is pinned to the outro window by the caller, so its spans
    are aligned-but-discarded here. Raises AlignmentUnavailable on any failure.
    """
    story: list[tuple[str, list[str]]] = []
    for beat in script.beats:
        story.extend(_split_for_video(beat.narration))
    if not story:
        raise AlignmentUnavailable("no story sentences")

    story_words = [tok for _cap, toks in story for tok in toks]
    cta_words = normalize_inline(cta_text).split() if cta_text.strip() else []
    all_words = story_words + cta_words

    word_timings = forced_word_timings(audio_path, all_words)
    story_timings = word_timings[: len(story_words)]  # discard CTA spans
    counts = [len(toks) for _cap, toks in story]
    spans = map_words_to_sentences(story_timings, counts)

    return [
        (cap, round(start, 3), round(end, 3))
        for (cap, _toks), (start, end) in zip(story, spans)
    ]
```

Then in `assemble_video`, replace the single line that computes `sent_timings` (currently lines ~421–423) with the branch:

```python
        sent_timings = None
        if settings.align_backend == "forced":
            try:
                sent_timings = _forced_story_timings(script, audio_path, cta_text)
                log.info("captions: forced alignment ok (%d sentences)", len(sent_timings))
            except AlignmentUnavailable as e:
                log.warning("captions: forced alignment unavailable (%s); using proportional", e)
                sent_timings = None
        if sent_timings is None:
            sent_timings = _compute_sentence_timings(
                script, beat_durs, lead_s=settings.caption_lead_s
            )
```

Leave the subsequent CTA append (`if cta_text: sent_timings.append((normalize_for_caption(cta_text), story_dur, total_s))`) unchanged — it already pins the CTA caption to the outro window for both backends.

Note: `beat_durs` is still computed above this block and is still used by `_compute_image_segments`, so image timing is untouched.

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd api && uv run pytest test_video.py::test_forced_story_timings_maps_aligned_words -v`
Expected: PASS.

- [ ] **Step 5: Add a fallback test (aligner raises → proportional path used)**

Append to `api/test_video.py`:

```python
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
    durs = [1.0, 1.0]
    fallback = video._compute_sentence_timings(script, durs)
    assert len(fallback) >= 2
```

- [ ] **Step 6: Run the full suite**

Run: `cd api && uv run pytest -q`
Expected: no new failures vs. baseline; the two new `test_video.py` tests pass.

- [ ] **Step 7: Commit**

```bash
git add api/services/video.py api/test_video.py
git commit -m "feat(api): forced-alignment caption timing with proportional fallback"
```

---

### Task 8: End-to-end validation on a real clip (manual gate)

**Files:** none (verification task).

**Interfaces:** Consumes the full pipeline.

- [ ] **Step 1: Render a known word with forced alignment enabled**

Run (against a local dev instance, picking an existing word_id with audio + images already produced):
```bash
cd api && ALIGN_BACKEND=forced uv run uvicorn main:app --port 8099 &
# then trigger /voice → /image → /assemble for one word_id via the usual route,
# OR call assemble_video directly in a REPL with align_backend="forced".
```
Expected in logs: `captions: forced alignment ok (N sentences)`.

- [ ] **Step 2: Verify by ear / frame-step**

Open the produced MP4 and confirm each caption appears as the narrator begins that sentence (no perceptible trail). Compare against a proportional render of the same word.

- [ ] **Step 3: Confirm fallback path on a tricky clip**

Render a word whose narration contains a bare digit (forces `normalize_mms_word` → `""`). Confirm the log shows `forced alignment unavailable (...); using proportional` and the clip still assembles successfully.

- [ ] **Step 4: No commit** (validation only). Record findings in the PR description.

---

### Task 9: Documentation + rollout notes

**Files:**
- Modify: `AGENTS.md` (config flag, new dependency, `stl` enablement step, the alignment service in the architecture section).
- Modify: `README.md` (mention forced-alignment captions + how to enable).

**Interfaces:** none.

- [ ] **Step 1: Document the flag and dependency in `AGENTS.md`**

Add `align_backend` to the settings/config reference table, note the `torch`/`torchaudio` dependency and the one-time MMS_FA model download, add `api/services/alignment.py` to the services list, and add a rollout note:

```markdown
### Enabling forced-alignment captions on stl

1. `cd ~/n8n-shorts && git pull && cd api && uv sync`  (installs torch/torchaudio)
2. Pre-warm the model once: `uv run python -c "import torchaudio; torchaudio.pipelines.MMS_FA.get_model(with_star=False)"`
3. Set `ALIGN_BACKEND=forced` in the service environment (or `align_backend` in config).
4. Restart uvicorn (remember the Homebrew PATH requirement for uv/ffmpeg on stl).
5. Render one word and verify the log line `captions: forced alignment ok`.
Any alignment failure auto-falls back to proportional timing — safe to leave on.
```

- [ ] **Step 2: Document in `README.md`**

Add a short consumer-facing note under the captions/feature section that captions are acoustically aligned to the narration when `align_backend=forced`, defaulting to proportional.

- [ ] **Step 3: Verify docs reference real symbols**

Run: `cd api && uv run python -c "from config import settings; assert hasattr(settings, 'align_backend'); from services import alignment; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 4: Commit**

```bash
git add AGENTS.md README.md
git commit -m "docs: document forced-alignment captions + stl rollout"
```

---

## Self-Review

**Spec coverage:**
- Tool choice (torchaudio MMS_FA, not Whisper ASR) → Tasks 1, 5. ✓
- 16 kHz resample → Task 5 `_load_16k_mono`. ✓
- Align known transcript → Task 5. ✓
- Exact word↔sentence mapping via shared tokenization → Tasks 4, 6, 7. ✓
- CTA pinned, full transcript aligned but CTA spans discarded → Task 7 `_forced_story_timings` + unchanged CTA append. ✓
- Config flag, default no-op → Task 2. ✓
- Graceful fallback on every failure mode → Tasks 5 (raises), 7 (catches), 8 step 3 (verified). ✓
- Image timing unchanged → Task 7 note (`beat_durs` still drives `_compute_image_segments`). ✓
- `caption_lead_s` retired for forced path → Task 2 comment + forced path never applies lead. ✓
- Docs sync → Task 9. ✓
- Deployment/model-prewarm on stl → Task 9 rollout note. ✓

**Placeholder scan:** No TBDs; every code step shows complete code; every command shows expected output. Tests that need the heavy model are explicitly env-gated (`RUN_ALIGNMENT_MODEL=1`) so CI/local default runs stay fast and offline.

**Type consistency:** `WordTiming(word, start_s, end_s)` is defined in Task 4 and consumed identically in Tasks 5 and 7. `forced_word_timings(wav_path, words) -> list[WordTiming]` (Task 5) is called with exactly those args in Task 7. `map_words_to_sentences(word_timings, counts) -> list[tuple[float,float]]` (Task 4) is called with `(story_timings, counts)` in Task 7. `_split_for_video -> list[tuple[str, list[str]]]` (Task 6) is consumed as `(cap, toks)` in Task 7. `AlignmentUnavailable` raised in Tasks 4/5, caught in Task 7. Consistent.

**Open assumptions to confirm during execution (not blockers):**
- `models.Script` / `Beat` constructor signatures in the Task 6/7 test helpers — adjust to the real constructors (the codebase has `Script.with_outro_cta`, `script.beats`, `beat.narration`, `beat.on_screen`, `beat.images`, `script.image_prompts`).
- MMS_FA `aligner(emission[0], tokenizer(words))` returns one token-span list per word; if the installed torchaudio version differs, the `len(token_spans) != len(words)` guard in Task 5 converts the mismatch into a safe fallback rather than a crash.
