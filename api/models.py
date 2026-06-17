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
    shares: int = 0                       # (#1) Shorts virality signal
    average_view_percentage: float = 0.0  # (#3) retention quality, 0-100


class VideoAnalytics(BaseModel):
    """Lifetime cumulative stats for one uploaded short (Data API)."""
    video_id: str
    views: int
    likes: int
    comments: int


class TopVideo(BaseModel):
    """(#5) The single best-performing uploaded short by lifetime views."""
    video_id: str
    url: str
    views: int
    likes: int
    comments: int


class CountryViews(BaseModel):
    """(#8) Views from one country over the window (top-N for the digest)."""
    country: str                    # ISO-3166 alpha-2 (e.g. "US")
    views: int


class YppProgress(BaseModel):
    """(#6) Distance to the YouTube Partner Program Shorts bar:
    1,000 subscribers AND 10M valid public Shorts views in 90 days."""
    subscribers: int
    subs_target: int = 1000
    subs_progress: float            # 0.0-1.0, capped
    shorts_views_90d: int
    shorts_views_target: int = 10_000_000
    shorts_views_progress: float    # 0.0-1.0, capped
    eligible: bool                  # both thresholds met


class TrendDelta(BaseModel):
    """(#4) Change vs an earlier daily snapshot (zero extra API calls)."""
    compared_days: int              # how many days back the baseline snapshot is
    subscribers: int                # total-subscriber change
    total_views: int                # lifetime-view change
    period_views: int               # trailing-window view change


class ChannelAnalytics(BaseModel):
    channel: str
    snapshot: ChannelSnapshot
    new_subs_1d: int                # subscribers gained-lost, yesterday
    period: PeriodMetrics           # trailing `days` window (default 30)
    videos_uploaded: int            # count of our uploaded shorts
    avg_likes_per_video: float      # lifetime cumulative across our shorts
    avg_comments_per_video: float
    videos: list[VideoAnalytics]
    # ─── enriched signals (items 1-10) ───
    traffic_sources: dict[str, int] = Field(default_factory=dict)  # (#2) source→views
    shorts_feed_share: float = 0.0  # (#2) fraction of period views from the Shorts feed
    top_countries: list[CountryViews] = Field(default_factory=list)  # (#8)
    top_video: TopVideo | None = None                              # (#5)
    ypp: YppProgress | None = None                                 # (#6)
    views_90d: int = 0              # public views over trailing 90d (drives YPP)
    queue_pending: int = 0          # (#9) pending words left in the channel queue
    uploads_24h: int = 0            # (#9) shorts uploaded in the last 24h
    trend: TrendDelta | None = None                                # (#4)
    milestones: list[str] = Field(default_factory=list)            # (#7)
    alerts: list[str] = Field(default_factory=list)                # (#10)


class DailyAnalyticsReport(BaseModel):
    date: str                       # report run date, YYYY-MM-DD
    days: int
    channels: list[ChannelAnalytics]
    errors: list[str] = Field(default_factory=list)
    # The rendered Telegram/Slack message body. Embedded here so n8n makes ONE
    # call: the Sheets path consumes `channels`, the chat path consumes this.
    digest_text: str = ""


class VideoStatRow(BaseModel):
    """One video's stats for one snapshot date — a single row in a channel tab.
    `*_total` are cumulative lifetime; `*_today` are the delta vs the prior
    snapshot (== total on the first-ever snapshot for the video)."""
    date: str                       # snapshot date, YYYY-MM-DD
    video_id: str
    url: str
    title: str
    published_at: str               # ISO 8601 from the Data API snippet
    days_live: int                  # whole days from publish to snapshot date
    views_total: int
    views_today: int
    likes_total: int
    likes_today: int
    comments_total: int
    comments_today: int
    watch_min_total: int            # Analytics API (lags ~1-2 days)
    watch_min_today: int            # Analytics API (lags ~1-2 days)
    shares_total: int               # Analytics API (lags ~1-2 days)
    shares_today: int               # Analytics API (lags ~1-2 days)


class ChannelVideoStats(BaseModel):
    """All per-video rows for one channel (one channel = one Sheets tab)."""
    channel: str
    rows: list[VideoStatRow] = Field(default_factory=list)


class VideoStatsReport(BaseModel):
    """All-channel per-video stats for one run. The n8n Code node flattens
    `channels[].rows[]`, routing each row to a tab named after its channel."""
    date: str                       # run date, YYYY-MM-DD
    channels: list[ChannelVideoStats]
    errors: list[str] = Field(default_factory=list)
