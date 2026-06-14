"""shorts-api — FastAPI service driving the multi-channel shorts pipeline.

All endpoints are scoped by channel, e.g. POST /wordstrata/script,
POST /the-mythscape/upload. The channel comes from the URL path; request
bodies carry only the word_id.
"""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import shutil
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import channels as channel_registry
import db
from config import settings
from models import (
    AssembleRequest,
    AssembleResponse,
    ChannelAnalytics,
    DailyAnalyticsReport,
    ImageGenResult,
    ImageRequest,
    ImageResponse,
    Script,
    ScriptRequest,
    ScriptResponse,
    UploadRequest,
    UploadResponse,
    VoiceRequest,
    VoiceResponse,
    WordRow,
)
from services.script import generate_script
from services import image as image_svc
from services import video as video_svc
from services import voice as voice_svc
from services import youtube as youtube_svc
from services import analytics as analytics_svc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("shorts-api")

app = FastAPI(
    title="shorts-api",
    version="0.2.0",
    description="Local multi-channel pipeline for YouTube Shorts.",
)


# ─── helpers ────────────────────────────────────────────────────────────────

def _resolve_channel(channel: str) -> channel_registry.ChannelConfig:
    try:
        return channel_registry.load(channel)
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(404, f"unknown channel '{channel}': {e}")


def _channel_dir(channel: str, sub: str) -> Path:
    return settings.channel_data_dir(channel) / sub


def _script_path(channel: str, word_id: int) -> Path:
    return _channel_dir(channel, "scripts") / f"word_{word_id:04d}.json"


def _audio_path(channel: str, word_id: int) -> Path:
    return _channel_dir(channel, "audio") / f"word_{word_id:04d}.wav"


def _image_path(channel: str, word_id: int, idx: int) -> Path:
    return _channel_dir(channel, "images") / f"word_{word_id:04d}_{idx}.png"


def _video_path(channel: str, word_id: int) -> Path:
    return _channel_dir(channel, "videos") / f"word_{word_id:04d}.mp4"


# ─── Health ─────────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    python: str
    platform: str
    ffmpeg: str | None
    ollama_url: str
    ollama_model: str
    data_dir: str
    db_path: str
    channels: list[str]


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        python=platform.python_version(),
        platform=platform.platform(),
        ffmpeg=shutil.which("ffmpeg"),
        ollama_url=settings.ollama_url,
        ollama_model=settings.ollama_model,
        data_dir=str(settings.data_dir),
        db_path=str(settings.db_path),
        channels=channel_registry.list_slugs(),
    )


# ─── Channels (registry) ────────────────────────────────────────────────────

class ChannelInfo(BaseModel):
    slug: str
    name: str
    handle: str | None
    tagline: str | None
    topic: str


@app.get("/channels", response_model=list[ChannelInfo])
async def list_channels() -> list[ChannelInfo]:
    return [
        ChannelInfo(slug=c.slug, name=c.name, handle=c.handle,
                    tagline=c.tagline, topic=c.topic)
        for c in channel_registry.all_configs()
    ]


# ─── State (per-channel) ────────────────────────────────────────────────────

class StateInitResponse(BaseModel):
    channel: str
    schema_applied: bool
    words_loaded: int
    total_words: int


@app.post("/{channel}/state/init", response_model=StateInitResponse)
async def state_init(channel: str) -> StateInitResponse:
    _resolve_channel(channel)
    db.init_schema()
    loaded = db.load_words_if_empty(channel)
    with db.conn() as c:
        (total,) = c.execute(
            "SELECT COUNT(*) FROM words WHERE channel = ?", (channel,)
        ).fetchone()
    return StateInitResponse(
        channel=channel,
        schema_applied=True,
        words_loaded=loaded,
        total_words=total,
    )


@app.get("/{channel}/state/next", response_model=WordRow | None)
async def state_next(channel: str) -> WordRow | None:
    _resolve_channel(channel)
    row = db.next_pending_word(channel)
    if not row:
        return None
    return WordRow.model_validate(row)


class StateSummary(BaseModel):
    channel: str
    total: int
    pending: int
    processing: int
    done: int
    failed: int
    skipped: int


@app.get("/{channel}/state/summary", response_model=StateSummary)
async def state_summary(channel: str) -> StateSummary:
    _resolve_channel(channel)
    with db.conn() as c:
        rows = c.execute(
            "SELECT status, COUNT(*) AS n FROM words WHERE channel = ? GROUP BY status",
            (channel,),
        ).fetchall()
    counts = {r["status"]: r["n"] for r in rows}
    return StateSummary(
        channel=channel,
        total=sum(counts.values()),
        pending=counts.get("pending", 0),
        processing=counts.get("processing", 0),
        done=counts.get("done", 0),
        failed=counts.get("failed", 0),
        skipped=counts.get("skipped", 0),
    )


# ─── Script ─────────────────────────────────────────────────────────────────

@app.post("/{channel}/script", response_model=ScriptResponse)
async def script(channel: str, req: ScriptRequest) -> ScriptResponse:
    _resolve_channel(channel)
    if req.word_id is not None:
        word_dict = db.get_word(channel, req.word_id)
        if not word_dict:
            raise HTTPException(404, f"word_id {req.word_id} not found in {channel}")
    else:
        word_dict = db.next_pending_word(channel)
        if not word_dict:
            raise HTTPException(404, f"no pending words in queue for {channel}")

    word = WordRow.model_validate(word_dict)

    t0 = time.perf_counter()
    script: Script = await generate_script(channel, word)
    duration_ms = int((time.perf_counter() - t0) * 1000)

    out_path = _script_path(channel, word.id)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(script.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    log.info(
        "script channel=%s word=%s id=%d generated in %d ms (model=%s)",
        channel, word.word, word.id, duration_ms, settings.ollama_model,
    )

    return ScriptResponse(
        word=word, script=script, script_path=str(out_path),
        duration_ms=duration_ms, model=settings.ollama_model,
    )


# ─── Voice ──────────────────────────────────────────────────────────────────

@app.post("/{channel}/voice", response_model=VoiceResponse)
async def voice(channel: str, req: VoiceRequest) -> VoiceResponse:
    _resolve_channel(channel)
    if not db.get_word(channel, req.word_id):
        raise HTTPException(404, f"word_id {req.word_id} not found in {channel}")

    script_path = _script_path(channel, req.word_id)
    if not script_path.exists():
        raise HTTPException(
            409,
            f"script not generated yet for word_id={req.word_id} "
            f"in channel={channel} (expected at {script_path})",
        )
    script = Script.model_validate(json.loads(script_path.read_text()))

    chosen_voice = voice_svc.pick_voice()
    await voice_svc.ensure_voice_downloaded(chosen_voice)

    audio_path = _audio_path(channel, req.word_id)
    result = voice_svc.synthesize_to_wav(script, audio_path, chosen_voice)

    log.info(
        "voice channel=%s word_id=%d → %s (%.2fs, %d bytes)",
        channel, req.word_id, result["audio_path"],
        result["duration_s"], result["size_bytes"],
    )

    return VoiceResponse(word_id=req.word_id, **result)


# ─── Image ──────────────────────────────────────────────────────────────────

class ImageWarmupResponse(BaseModel):
    loaded: bool
    model: str
    duration_ms: int


@app.post("/image/warmup", response_model=ImageWarmupResponse)
async def image_warmup() -> ImageWarmupResponse:
    """Force-load Flux/Z-Image into memory. Channel-agnostic — the model is
    shared across channels."""
    t0 = time.perf_counter()
    result = await asyncio.to_thread(image_svc.warmup)
    duration_ms = int((time.perf_counter() - t0) * 1000)
    log.info("image/warmup done in %d ms", duration_ms)
    return ImageWarmupResponse(
        loaded=result["loaded"],
        model=result["model"],
        duration_ms=duration_ms,
    )


@app.post("/{channel}/image", response_model=ImageResponse)
async def image(channel: str, req: ImageRequest) -> ImageResponse:
    _resolve_channel(channel)
    if not db.get_word(channel, req.word_id):
        raise HTTPException(404, f"word_id {req.word_id} not found in {channel}")

    script_path = _script_path(channel, req.word_id)
    if not script_path.exists():
        raise HTTPException(
            409,
            f"script not generated yet for word_id={req.word_id} in channel={channel}",
        )
    script = Script.model_validate(json.loads(script_path.read_text()))
    out_dir = _channel_dir(channel, "images")

    t0 = time.perf_counter()
    results = await asyncio.to_thread(
        image_svc.generate_images, script, out_dir, req.word_id
    )
    duration_ms = int((time.perf_counter() - t0) * 1000)

    log.info(
        "image gen channel=%s word_id=%d: %d images in %d ms",
        channel, req.word_id, len(results), duration_ms,
    )

    return ImageResponse(
        word_id=req.word_id,
        model=settings.mflux_model,
        images=[ImageGenResult(**r) for r in results],
        duration_ms=duration_ms,
    )


# ─── Assemble ───────────────────────────────────────────────────────────────

@app.post("/{channel}/assemble", response_model=AssembleResponse)
async def assemble(channel: str, req: AssembleRequest) -> AssembleResponse:
    _resolve_channel(channel)
    if not db.get_word(channel, req.word_id):
        raise HTTPException(404, f"word_id {req.word_id} not found in {channel}")

    script_path = _script_path(channel, req.word_id)
    if not script_path.exists():
        raise HTTPException(409, f"script not found for word_id={req.word_id} in {channel}")
    script = Script.model_validate(json.loads(script_path.read_text()))

    audio_path = _audio_path(channel, req.word_id)
    if not audio_path.exists():
        raise HTTPException(409, f"audio not found for word_id={req.word_id} in {channel}")

    n_images = len(script.image_prompts)
    image_paths = [_image_path(channel, req.word_id, i) for i in range(n_images)]
    missing = [p for p in image_paths if not p.exists()]
    if missing:
        raise HTTPException(409, f"missing images: {[str(p) for p in missing]}")

    out_path = _video_path(channel, req.word_id)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    result = await asyncio.to_thread(
        video_svc.assemble_video, script, image_paths, audio_path, out_path
    )
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    log.info(
        "assembled video channel=%s word_id=%d → %s (%d ms)",
        channel, req.word_id, result["video_path"], elapsed_ms,
    )

    return AssembleResponse(word_id=req.word_id, elapsed_ms=elapsed_ms, **result)


# ─── Upload ─────────────────────────────────────────────────────────────────

@app.post("/{channel}/upload", response_model=UploadResponse)
async def upload(channel: str, req: UploadRequest) -> UploadResponse:
    cfg = _resolve_channel(channel)
    if not db.get_word(channel, req.word_id):
        raise HTTPException(404, f"word_id {req.word_id} not found in {channel}")

    script_path = _script_path(channel, req.word_id)
    if not script_path.exists():
        raise HTTPException(409, f"script not found for word_id={req.word_id} in {channel}")
    script = Script.model_validate(json.loads(script_path.read_text()))

    video_path = _video_path(channel, req.word_id)
    if not video_path.exists():
        raise HTTPException(409, f"video not assembled for word_id={req.word_id} in {channel}")

    t0 = time.perf_counter()
    result = await asyncio.to_thread(
        youtube_svc.upload_short,
        channel,
        script,
        video_path,
        req.privacy,
        category_id=req.category_id or cfg.youtube_category_id,
        default_language=req.default_language or cfg.youtube_default_language,
        default_audio_language=(
            req.default_audio_language or cfg.youtube_default_audio_language
        ),
        contains_synthetic_media=(
            cfg.ai_disclosure
            if req.contains_synthetic_media is None
            else req.contains_synthetic_media
        ),
        license_=req.license or cfg.youtube_license,
    )
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    # Audit trail
    n_images = len(script.image_prompts)
    audit_image_paths = [str(_image_path(channel, req.word_id, i)) for i in range(n_images)]
    audit_audio = _audio_path(channel, req.word_id)
    voice_used = settings.piper_voices[0] if settings.piper_voices else "unknown"
    audio_meta = audit_audio.with_suffix(".meta.json")
    if audio_meta.exists():
        try:
            voice_used = json.loads(audio_meta.read_text()).get("voice", voice_used)
        except (json.JSONDecodeError, OSError):
            pass

    run_id = db.record_completed_run(
        channel,
        req.word_id,
        script_path=str(script_path),
        image_paths=audit_image_paths,
        audio_path=str(audit_audio) if audit_audio.exists() else None,
        video_path=str(video_path),
        youtube_video_id=result["video_id"],
        youtube_url=result["url"],
        script_model=settings.ollama_model,
        image_model=settings.mflux_model,
        tts_voice=voice_used,
    )

    log.info(
        "uploaded channel=%s word_id=%d run_id=%d → %s (%d ms, privacy=%s)",
        channel, req.word_id, run_id, result["url"], elapsed_ms, result["privacy"],
    )
    db.set_word_status(channel, req.word_id, "done")
    _ = cfg  # quiet linter
    return UploadResponse(word_id=req.word_id, elapsed_ms=elapsed_ms, **result)


# ─── Analytics ────────────────────────────────────────────────────────────────

@app.get("/analytics/daily", response_model=DailyAnalyticsReport)
async def analytics_daily(days: int = 30) -> DailyAnalyticsReport:
    """All-channel daily digest (structured JSON, for the Google Sheets append).
    Called by the n8n analytics workflow."""
    channels = channel_registry.list_slugs()
    return await asyncio.to_thread(analytics_svc.build_daily_report, channels, days)


class DigestResponse(BaseModel):
    text: str


# Declared BEFORE /{channel} so the wildcard doesn't capture "analytics".
@app.get("/analytics/daily/digest", response_model=DigestResponse)
async def analytics_daily_digest(days: int = 30) -> DigestResponse:
    """The all-channel digest pre-rendered as the Telegram/Slack message body.
    The n8n Code node forwards this string verbatim — formatting lives here so
    it stays unit-tested."""
    channels = channel_registry.list_slugs()
    report = await asyncio.to_thread(analytics_svc.build_daily_report, channels, days)
    return DigestResponse(text=analytics_svc.render_digest(report))


# NOTE: must stay declared AFTER /analytics/daily — otherwise the {channel}
# wildcard would capture "analytics" as a slug.
@app.get("/{channel}/analytics", response_model=ChannelAnalytics)
async def analytics_channel(channel: str, days: int = 30) -> ChannelAnalytics:
    _resolve_channel(channel)
    return await asyncio.to_thread(analytics_svc.channel_analytics, channel, days)


# ─── Startup ────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def _startup() -> None:
    log.info(
        "shorts-api up | ollama=%s model=%s data_dir=%s db=%s channels=%s",
        settings.ollama_url, settings.ollama_model,
        settings.data_dir, settings.db_path,
        channel_registry.list_slugs(),
    )
    try:
        db.init_schema()
        for slug in channel_registry.list_slugs():
            loaded = db.load_words_if_empty(slug)
            if loaded:
                log.info("startup loaded %d words for channel=%s", loaded, slug)
    except Exception as e:
        log.error("startup state init failed: %s", e)
