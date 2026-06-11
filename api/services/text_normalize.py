"""Shared text normalization for Piper TTS and on-screen captions.

The voice (Piper) and the caption renderer (Pillow) MUST normalize the input
text identically — otherwise the captions read different words than the
narrator speaks. This module is the single source of truth for that mapping.

Operations (in order):
 1. Strip paired markdown emphasis (*foo* / _foo_).
 2. Strip emoji codepoints in common ranges.
 3. Expand numbers Piper otherwise mispronounces:
    - year ranges (1880–1920)            → "eighteen eighty to nineteen twenty"
    - decades (1880s)                     → "eighteen eighties"
    - bare years (1880)                   → "eighteen eighty"
    - ordinals (19th, 1st, 2nd, 3rd)      → "nineteenth", "first", "second", …
 4. Collapse runs of whitespace within a paragraph to a single space.

`normalize_for_tts(text)` preserves `\\n\\n` paragraph boundaries — splits
input into paragraphs first, normalizes each, then rejoins with `\\n\\n`.
This is important because Piper uses the paragraph break to insert a longer
prosodic pause and to bound its synthesis utterances cleanly. Collapsing the
break causes the abrupt transitions we saw at the payoff↔CTA boundary in
word_0005 (audible clipping at the seam).
"""

from __future__ import annotations

import re

from num2words import num2words


_EMPH_RE = re.compile(r"(\*+)([^*]+?)\1|(_+)([^_]+?)\3")

_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # SMP — emoticons / pictographs / etc.
    "\U00002600-\U000027BF"  # misc symbols + dingbats
    "♀-♂"
    "☀-⭕"
    "‍"
    "⏏⏩⌚️〰"
    "]+",
    flags=re.UNICODE,
)

# Match any run of whitespace EXCEPT a paragraph break (\n\n+) — paragraph
# breaks are preserved by splitting on them first, then collapsing remaining
# whitespace within each paragraph.
_INPARA_WS_RE = re.compile(r"\s+")

# Years and decades (channel range: 1000–2039).
_YEAR_RANGE_RE = re.compile(r"\b(1\d{3}|20[0-3]\d)\s*[–—\-]\s*(1\d{3}|20[0-3]\d)\b")
_YEAR_DECADE_RE = re.compile(r"\b(1\d{2}0|20[0-3]0)s\b")
_YEAR_RE = re.compile(r"\b(1\d{3}|20[0-3]\d)\b")

# Ordinals like 19th, 1st, 22nd, 103rd. Piper's espeak-ng sometimes reads
# "19th" as "one nine th" — expanding to "nineteenth" prevents that.
_ORDINAL_RE = re.compile(r"\b(\d{1,4})(st|nd|rd|th)\b", re.IGNORECASE)

# Paragraph splitter — one or more blank lines between blocks.
_PARA_SPLIT_RE = re.compile(r"\n\s*\n+")


def _spoken_decade(year_starting_decade: int) -> str:
    """1830 → 'eighteen thirties' (pluralize the last word of the year form)."""
    spoken = num2words(year_starting_decade, to="year")
    if spoken.endswith("y"):
        return spoken[:-1] + "ies"
    return spoken + "s"


def _spoken_ordinal(n: int) -> str:
    return num2words(n, to="ordinal")


def _normalize_numbers(text: str) -> str:
    text = _YEAR_RANGE_RE.sub(
        lambda m: f"{num2words(int(m.group(1)), to='year')} to "
                  f"{num2words(int(m.group(2)), to='year')}",
        text,
    )
    text = _YEAR_DECADE_RE.sub(
        lambda m: _spoken_decade(int(m.group(1))),
        text,
    )
    text = _YEAR_RE.sub(
        lambda m: num2words(int(m.group(1)), to="year"),
        text,
    )
    text = _ORDINAL_RE.sub(
        lambda m: _spoken_ordinal(int(m.group(1))),
        text,
    )
    return text


def normalize_for_tts(text: str) -> str:
    """Full normalization that PRESERVES paragraph boundaries.

    Input may contain `\\n\\n` to denote paragraph breaks (Piper uses them as
    utterance/silence boundaries). Output joins normalized paragraphs with the
    same `\\n\\n` so Piper retains its prosodic cue.
    """
    paragraphs = _PARA_SPLIT_RE.split(text)
    out: list[str] = []
    for p in paragraphs:
        p = _EMPH_RE.sub(lambda m: m.group(2) or m.group(4) or "", p)
        p = _EMOJI_RE.sub("", p)
        p = _normalize_numbers(p)
        p = _INPARA_WS_RE.sub(" ", p).strip()
        if p:
            out.append(p)
    return "\n\n".join(out)


def normalize_inline(text: str) -> str:
    """Variant for caption rendering — same number/emphasis/emoji rules, but
    flattens to a single line (collapses any `\\n\\n` too). Caption sentences
    are already one-line, so the paragraph distinction doesn't matter here."""
    return _INPARA_WS_RE.sub(" ", normalize_for_tts(text)).strip()
