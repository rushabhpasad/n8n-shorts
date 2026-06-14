"""Pydantic schemas — request/response bodies and the LLM script contract.

The script schema is the heart of the pipeline. The Ollama call is forced to
emit JSON matching `Script` exactly. Downstream stages (image, voice, assemble)
all consume this structure.
"""

from __future__ import annotations

from typing import Literal  # noqa: F401  (used below in Literal[...])

from pydantic import BaseModel, Field, model_validator


BeatLabel = Literal["hook", "origin", "payoff"]


# Total images a Short shows across all beats. Hard floor 4, ceiling 7
# (5-6 recommended). Each image is one diffusion render, so this is also the
# per-Short image-generation budget.
MIN_IMAGES_TOTAL = 4
MAX_IMAGES_TOTAL = 7


class Beat(BaseModel):
    label: BeatLabel
    narration: str = Field(min_length=10, max_length=600)
    on_screen: str = Field(min_length=1, max_length=80)
    # Diffusion prompts for this beat's images, played in order over
    # equal-duration sub-segments of the beat. 1–4 per beat. Prompts live here
    # (not a shared indexed pool) so the images generated always match the
    # images shown — no orphan renders, no dangling indices.
    images: list[str] = Field(min_length=1, max_length=4)


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
    youtube: YouTubeMeta

    @property
    def image_prompts(self) -> list[str]:
        """Flat, ordered list of every beat's image prompts.

        This is the canonical image sequence the whole pipeline consumes
        (image generation renders one PNG per entry; assembly maps each entry
        to a video sub-segment). Because it is derived from the beats, the
        prompts generated and the images shown are always 1:1 by construction.
        """
        return [prompt for beat in self.beats for prompt in beat.images]

    @model_validator(mode="after")
    def _validate_image_count(self) -> "Script":
        """Bound the total image count across all beats.

        Per-beat counts (1–4) are enforced on Beat.images; here we only guard
        the Short-wide total so a video never renders on too few visuals or
        blows the per-Short image-generation budget.
        """
        total = sum(len(beat.images) for beat in self.beats)
        if total < MIN_IMAGES_TOTAL:
            raise ValueError(
                f"beats carry only {total} image(s); need at least "
                f"{MIN_IMAGES_TOTAL}"
            )
        if total > MAX_IMAGES_TOTAL:
            raise ValueError(
                f"beats carry {total} images; at most {MAX_IMAGES_TOTAL} allowed"
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


# ─── Analytics ────────────────────────────────────────────────────────────────

class ChannelSnapshot(BaseModel):
    """Lifetime channel totals (YouTube Data API, point-in-time)."""
    subscribers: int
    total_views: int
    video_count: int


class PeriodMetrics(BaseModel):
    """Time-ranged channel metrics (YouTube Analytics API)."""
    days: int
    subscribers_gained: int
    subscribers_lost: int
    new_subscribers: int            # gained - lost
    estimated_minutes_watched: int
    views: int
    likes: int
    comments: int
    average_view_duration_s: int


class VideoAnalytics(BaseModel):
    """Lifetime cumulative stats for one uploaded short (Data API)."""
    video_id: str
    views: int
    likes: int
    comments: int


class ChannelAnalytics(BaseModel):
    channel: str
    snapshot: ChannelSnapshot
    new_subs_1d: int                # subscribers gained-lost, yesterday
    period: PeriodMetrics           # trailing `days` window (default 30)
    videos_uploaded: int            # count of our uploaded shorts
    avg_likes_per_video: float      # lifetime cumulative across our shorts
    avg_comments_per_video: float
    videos: list[VideoAnalytics]


class DailyAnalyticsReport(BaseModel):
    date: str                       # report run date, YYYY-MM-DD
    days: int
    channels: list[ChannelAnalytics]
    errors: list[str] = Field(default_factory=list)
