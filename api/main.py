"""shorts-api — FastAPI service driving the etymology-shorts pipeline.

Phase 3a: /state, /script.
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

from config import settings
import db
from models import (
    AssembleRequest,
    AssembleResponse,
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("shorts-api")

app = FastAPI(
    title="shorts-api",
    version="0.1.0",
    description="Local pipeline for etymology Shorts.",
)


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
    )


# ─── State ──────────────────────────────────────────────────────────────────

class StateInitResponse(BaseModel):
    schema_applied: bool
    words_loaded: int
    total_words: int


@app.post("/state/init", response_model=StateInitResponse)
async def state_init() -> StateInitResponse:
    db.init_schema()
    loaded = db.load_words_if_empty()
    with db.conn() as c:
        (total,) = c.execute("SELECT COUNT(*) FROM words").fetchone()
    return StateInitResponse(
        schema_applied=True,
        words_loaded=loaded,
        total_words=total,
    )


@app.get("/state/next", response_model=WordRow | None)
async def state_next() -> WordRow | None:
    row = db.next_pending_word()
    if not row:
        return None
    # next_word view doesn't carry status; fetch full row
    full = db.get_word(row["id"])
    return WordRow.model_validate(full)


class StateSummary(BaseModel):
    total: int
    pending: int
    processing: int
    done: int
    failed: int
    skipped: int


@app.get("/state/summary", response_model=StateSummary)
async def state_summary() -> StateSummary:
    with db.conn() as c:
        rows = c.execute(
            "SELECT status, COUNT(*) AS n FROM words GROUP BY status"
        ).fetchall()
    counts = {r["status"]: r["n"] for r in rows}
    return StateSummary(
        total=sum(counts.values()),
        pending=counts.get("pending", 0),
        processing=counts.get("processing", 0),
        done=counts.get("done", 0),
        failed=counts.get("failed", 0),
        skipped=counts.get("skipped", 0),
    )


# ─── Script ─────────────────────────────────────────────────────────────────

@app.post("/script", response_model=ScriptResponse)
async def script(req: ScriptRequest) -> ScriptResponse:
    # Pick the word
    if req.word_id is not None:
        word_dict = db.get_word(req.word_id)
        if not word_dict:
            raise HTTPException(404, f"word_id {req.word_id} not found")
    else:
        word_dict = db.next_pending_word()
        if not word_dict:
            raise HTTPException(404, "no pending words in queue")
        word_dict = db.get_word(word_dict["id"])
        if not word_dict:
            raise HTTPException(500, "queue returned phantom id")

    word = WordRow.model_validate(word_dict)

    t0 = time.perf_counter()
    script: Script = await generate_script(word)
    duration_ms = int((time.perf_counter() - t0) * 1000)

    # Persist script JSON to disk
    out_dir: Path = settings.data_dir / "scripts"
    out_dir.mkdir(parents=True, exist_ok=True)
    script_path = out_dir / f"word_{word.id:04d}.json"
    script_path.write_text(
        json.dumps(script.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    log.info(
        "script for word=%s id=%d generated in %d ms (model=%s)",
        word.word,
        word.id,
        duration_ms,
        settings.ollama_model,
    )

    return ScriptResponse(
        word=word,
        script=script,
        script_path=str(script_path),
        duration_ms=duration_ms,
        model=settings.ollama_model,
    )


# ─── Voice ──────────────────────────────────────────────────────────────────

@app.post("/voice", response_model=VoiceResponse)
async def voice(req: VoiceRequest) -> VoiceResponse:
    word_dict = db.get_word(req.word_id)
    if not word_dict:
        raise HTTPException(404, f"word_id {req.word_id} not found")

    # Load the script from disk (must have run /script first)
    script_path: Path = settings.data_dir / "scripts" / f"word_{req.word_id:04d}.json"
    if not script_path.exists():
        raise HTTPException(
            409,
            f"script not generated yet for word_id={req.word_id} "
            f"(expected at {script_path})",
        )
    script = Script.model_validate(json.loads(script_path.read_text()))

    chosen_voice = voice_svc.pick_voice()
    await voice_svc.ensure_voice_downloaded(chosen_voice)

    audio_path: Path = settings.data_dir / "audio" / f"word_{req.word_id:04d}.wav"
    result = voice_svc.synthesize_to_wav(script, audio_path, chosen_voice)

    log.info(
        "voice for word_id=%d → %s (%.2fs, %d bytes)",
        req.word_id,
        result["audio_path"],
        result["duration_s"],
        result["size_bytes"],
    )

    return VoiceResponse(word_id=req.word_id, **result)


# ─── Image ──────────────────────────────────────────────────────────────────

class ImageWarmupResponse(BaseModel):
    loaded: bool
    model: str
    duration_ms: int


@app.post("/image/warmup", response_model=ImageWarmupResponse)
async def image_warmup() -> ImageWarmupResponse:
    """Force-load Flux into memory. First call downloads ~12GB and may take
    5–20 min depending on bandwidth. Subsequent calls return immediately."""
    t0 = time.perf_counter()
    result = await asyncio.to_thread(image_svc.warmup)
    duration_ms = int((time.perf_counter() - t0) * 1000)
    log.info("image/warmup done in %d ms", duration_ms)
    return ImageWarmupResponse(
        loaded=result["loaded"],
        model=result["model"],
        duration_ms=duration_ms,
    )


@app.post("/image", response_model=ImageResponse)
async def image(req: ImageRequest) -> ImageResponse:
    word_dict = db.get_word(req.word_id)
    if not word_dict:
        raise HTTPException(404, f"word_id {req.word_id} not found")

    script_path: Path = settings.data_dir / "scripts" / f"word_{req.word_id:04d}.json"
    if not script_path.exists():
        raise HTTPException(
            409,
            f"script not generated yet for word_id={req.word_id} "
            f"(expected at {script_path})",
        )
    script = Script.model_validate(json.loads(script_path.read_text()))
    out_dir: Path = settings.data_dir / "images"

    t0 = time.perf_counter()
    results = await asyncio.to_thread(
        image_svc.generate_images, script, out_dir, req.word_id
    )
    duration_ms = int((time.perf_counter() - t0) * 1000)

    log.info(
        "image gen for word_id=%d: %d images in %d ms",
        req.word_id,
        len(results),
        duration_ms,
    )

    return ImageResponse(
        word_id=req.word_id,
        model=settings.mflux_model,
        images=[ImageGenResult(**r) for r in results],
        duration_ms=duration_ms,
    )


# ─── Assemble (ffmpeg → MP4) ────────────────────────────────────────────────

@app.post("/assemble", response_model=AssembleResponse)
async def assemble(req: AssembleRequest) -> AssembleResponse:
    word_dict = db.get_word(req.word_id)
    if not word_dict:
        raise HTTPException(404, f"word_id {req.word_id} not found")

    script_path: Path = settings.data_dir / "scripts" / f"word_{req.word_id:04d}.json"
    if not script_path.exists():
        raise HTTPException(409, f"script not found for word_id={req.word_id}")
    script = Script.model_validate(json.loads(script_path.read_text()))

    audio_path = settings.data_dir / "audio" / f"word_{req.word_id:04d}.wav"
    if not audio_path.exists():
        raise HTTPException(409, f"audio not found for word_id={req.word_id}")

    n_images = len(script.image_prompts)
    image_paths = [
        settings.data_dir / "images" / f"word_{req.word_id:04d}_{i}.png"
        for i in range(n_images)
    ]
    missing = [p for p in image_paths if not p.exists()]
    if missing:
        raise HTTPException(
            409, f"missing images: {[str(p) for p in missing]}"
        )

    output_path = settings.data_dir / "videos" / f"word_{req.word_id:04d}.mp4"

    t0 = time.perf_counter()
    result = await asyncio.to_thread(
        video_svc.assemble_video, script, image_paths, audio_path, output_path
    )
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    log.info(
        "assembled video for word_id=%d → %s (%d ms)",
        req.word_id,
        result["video_path"],
        elapsed_ms,
    )

    return AssembleResponse(word_id=req.word_id, elapsed_ms=elapsed_ms, **result)


# ─── Upload ─────────────────────────────────────────────────────────────────

@app.post("/upload", response_model=UploadResponse)
async def upload(req: UploadRequest) -> UploadResponse:
    word_dict = db.get_word(req.word_id)
    if not word_dict:
        raise HTTPException(404, f"word_id {req.word_id} not found")

    script_path: Path = settings.data_dir / "scripts" / f"word_{req.word_id:04d}.json"
    if not script_path.exists():
        raise HTTPException(409, f"script not found for word_id={req.word_id}")
    script = Script.model_validate(json.loads(script_path.read_text()))

    video_path = settings.data_dir / "videos" / f"word_{req.word_id:04d}.mp4"
    if not video_path.exists():
        raise HTTPException(409, f"video not assembled for word_id={req.word_id}")

    t0 = time.perf_counter()
    result = await asyncio.to_thread(
        youtube_svc.upload_short, script, video_path, req.privacy
    )
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    # Audit trail: persist a run row capturing every input + the YouTube IDs.
    n_images = len(script.image_prompts)
    audit_image_paths = [
        str(settings.data_dir / "images" / f"word_{req.word_id:04d}_{i}.png")
        for i in range(n_images)
    ]
    audit_audio = settings.data_dir / "audio" / f"word_{req.word_id:04d}.wav"
    # Read which voice was actually used (written by /voice as a sidecar).
    voice_used = settings.piper_voices[0] if settings.piper_voices else "unknown"
    audio_meta = settings.data_dir / "audio" / f"word_{req.word_id:04d}.meta.json"
    if audio_meta.exists():
        try:
            voice_used = json.loads(audio_meta.read_text()).get("voice", voice_used)
        except (json.JSONDecodeError, OSError):
            pass

    run_id = db.record_completed_run(
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
        "uploaded word_id=%d run_id=%d → %s (%d ms, privacy=%s)",
        req.word_id, run_id,
        result["url"],
        elapsed_ms,
        result["privacy"],
    )
    db.set_word_status(req.word_id, "done")
    return UploadResponse(word_id=req.word_id, elapsed_ms=elapsed_ms, **result)


# ─── Startup ────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def _startup() -> None:
    log.info(
        "shorts-api up | ollama=%s model=%s data_dir=%s db=%s",
        settings.ollama_url,
        settings.ollama_model,
        settings.data_dir,
        settings.db_path,
    )
    # Best-effort: ensure schema + words loaded on startup
    try:
        db.init_schema()
        loaded = db.load_words_if_empty()
        if loaded:
            log.info("startup loaded %d words into state", loaded)
    except Exception as e:
        log.error("startup state init failed: %s", e)
