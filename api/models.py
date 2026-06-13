"""Pydantic schemas — request/response bodies and the LLM script contract.

The script schema is the heart of the pipeline. The Ollama call is forced to
emit JSON matching `Script` exactly. Downstream stages (image, voice, assemble)
all consume this structure.
"""

from __future__ import annotations

from typing import Literal  # noqa: F401  (used below in Literal[...])

from pydantic import BaseModel, Field, model_validator


BeatLabel = Literal["hook", "origin", "payoff"]


# Minimum number of DISTINCT images a Short must show across its beats. Hard
# floor is 4; 5-7 is the recommended range (also the cap on image_prompts).
MIN_IMAGES_USED = 4


class Beat(BaseModel):
    label: BeatLabel
    narration: str = Field(min_length=10, max_length=600)
    on_screen: str = Field(min_length=1, max_length=80)
    # Indices into Script.image_prompts. 1–4 images per beat; they play in order
    # over equal-duration sub-segments of the beat.
    image_idxs: list[int] = Field(min_length=1, max_length=4)


class YouTubeMeta(BaseModel):
    title: str = Field(min_length=10, max_length=100)
    description: str = Field(min_length=20, max_length=4000)
    tags: list[str] = Field(min_length=3, max_length=15)


class Script(BaseModel):
    word: str
    pronunciation: str
    title_text: str
    tagline: str = Field(min_length=4, max_length=80)
    beats: list[Beat] = Field(min_length=3, max_length=3)
    image_prompts: list[str] = Field(min_length=4, max_length=7)
    youtube: YouTubeMeta

    @model_validator(mode="after")
    def _validate_image_coverage(self) -> "Script":
        """Validate image references without demanding a strict permutation.

        Small local models rarely emit a perfect 1:1 mapping of beats to
        prompts, so instead of the old "exact permutation of 0..n-1" rule we
        enforce only the two invariants that actually matter downstream:

        * Every referenced index is a real slot in image_prompts. Video
          assembly looks images up by index, so an out-of-range idx would
          crash a later stage.
        * Beats collectively use at least MIN_IMAGES_USED distinct images
          (4 minimum, 5-7 recommended) so a Short never renders on too few
          visuals. image_prompts itself is capped at 4-7 entries.

        Prompts may go unused (the orphan render is wasted, not fatal) and an
        image may repeat across beats - neither is rejected here.
        """
        n = len(self.image_prompts)
        used = [idx for b in self.beats for idx in b.image_idxs]

        out_of_range = sorted({idx for idx in used if not 0 <= idx < n})
        if out_of_range:
            raise ValueError(
                f"image_idxs reference non-existent prompts {out_of_range}; "
                f"valid range is 0..{n-1}"
            )

        distinct = len(set(used))
        if distinct < MIN_IMAGES_USED:
            raise ValueError(
                f"beats use only {distinct} distinct image(s); need at least "
                f"{MIN_IMAGES_USED} (5-7 recommended), got {sorted(set(used))}"
            )
        return self

    def with_outro_cta(self, cta: str) -> "Script":
        """Return a copy with `cta` appended to the last beat's narration,
        separated by '\\n\\n' so Piper inserts a natural prosodic pause
        before the CTA. Audio and video share this transformation."""
        if not cta or not cta.strip():
            return self
        new_beats = list(self.beats)
        last = new_beats[-1]
        appended = last.narration.rstrip()
        if not appended.endswith((".", "!", "?")):
            appended += "."
        appended = appended + "\n\n" + cta.strip()
        new_beats[-1] = last.model_copy(update={"narration": appended})
        return self.model_copy(update={"beats": new_beats})


class WordRow(BaseModel):
    id: int
    word: str
    category: str
    origin_language: str
    hook: str
    status: str
    priority: int


class ScriptRequest(BaseModel):
    """If word_id is omitted, pull the next pending word automatically."""

    word_id: int | None = None


class ScriptResponse(BaseModel):
    word: WordRow
    script: Script
    script_path: str
    duration_ms: int
    model: str


class VoiceRequest(BaseModel):
    word_id: int


class VoiceResponse(BaseModel):
    word_id: int
    audio_path: str
    duration_s: float
    size_bytes: int
    voice: str
    sample_rate: int


class ImageRequest(BaseModel):
    word_id: int


class ImageGenResult(BaseModel):
    image_idx: int
    image_path: str
    prompt: str
    seed: int
    width: int
    height: int
    steps: int
    size_bytes: int


class ImageResponse(BaseModel):
    word_id: int
    model: str
    images: list[ImageGenResult]
    duration_ms: int


class AssembleRequest(BaseModel):
    word_id: int


class AssembleResponse(BaseModel):
    word_id: int
    video_path: str
    duration_s: float
    size_bytes: int
    width: int
    height: int
    fps: int
    beat_durations_s: list[float]
    sentence_count: int
    image_count: int
    segment_count: int
    story_duration_s: float
    outro_duration_s: float
    font: str
    elapsed_ms: int


class UploadRequest(BaseModel):
    word_id: int
    privacy: Literal["private", "unlisted", "public"] = "public"
    # Per-call overrides. When None, the channel-config default is used.
    category_id: str | None = None
    default_language: str | None = None
    default_audio_language: str | None = None
    contains_synthetic_media: bool | None = None
    license: Literal["youtube", "creativeCommon"] | None = None


class UploadResponse(BaseModel):
    word_id: int
    video_id: str
    url: str
    privacy: str
    elapsed_ms: int
