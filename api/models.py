"""Pydantic schemas — request/response bodies and the LLM script contract.

The script schema is the heart of the pipeline. The Ollama call is forced to
emit JSON matching `Script` exactly. Downstream stages (image, voice, assemble)
all consume this structure.
"""

from __future__ import annotations

from typing import Literal  # noqa: F401  (used below in Literal[...])

from pydantic import BaseModel, Field, model_validator


BeatLabel = Literal["hook", "origin", "payoff"]


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
        """Every index in image_prompts must be referenced by exactly one beat."""
        used = [idx for b in self.beats for idx in b.image_idxs]
        n = len(self.image_prompts)
        if sorted(used) != list(range(n)):
            raise ValueError(
                f"image_idxs across beats must be a permutation of 0..{n-1}, "
                f"got {sorted(used)}"
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
