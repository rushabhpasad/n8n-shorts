"""Piper TTS — narration → WAV with per-video voice shuffle.

- Voice files (.onnx + .onnx.json) live under settings.piper_voices_dir.
- Auto-download from rhasspy/piper-voices on first use of each voice.
- For every /voice call we pick one voice uniformly at random from
  settings.piper_voices (4 by default — all commercial-use-OK).
- We write a small sidecar JSON next to the WAV recording which voice was
  used; /upload reads this when persisting the audit trail.
"""

from __future__ import annotations

import json
import logging
import random
import wave
from pathlib import Path

import httpx
from piper import PiperVoice

from config import settings
from models import Script
from services.text_normalize import normalize_for_tts

log = logging.getLogger("shorts-api.voice")

# rhasspy/piper-voices on Hugging Face:
# https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US/<speaker>/<quality>/
PIPER_HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


def _parse_voice(voice: str) -> tuple[str, str, str]:
    """'en_US-norman-medium' → ('en_US', 'norman', 'medium')."""
    parts = voice.split("-")
    if len(parts) != 3:
        raise ValueError(
            f"piper voice must be 'locale-speaker-quality', got {voice!r}"
        )
    return parts[0], parts[1], parts[2]


def pick_voice() -> str:
    """Uniformly random pick from the configured Piper pool. Errors if empty."""
    pool = settings.piper_voices
    if not pool:
        raise RuntimeError("settings.piper_voices is empty")
    return random.choice(pool)


def pick_kokoro_voice() -> str:
    """Uniformly random pick from the configured Kokoro pool. Errors if empty."""
    pool = settings.kokoro_voices
    if not pool:
        raise RuntimeError("settings.kokoro_voices is empty")
    return random.choice(pool)


def _build_narration_text(script: Script) -> str:
    """Assemble the spoken text shared by every backend.

    normalize_for_tts preserves any \\n\\n paragraph breaks inside each beat's
    narration — important for the payoff↔CTA boundary, which a TTS engine would
    otherwise read as a single utterance (causing the t=40s clip we saw on
    word_0005). Beats are joined with \\n\\n too so the engine inserts inter-beat
    silence cleanly. The outro CTA is appended to the last beat (no-op if empty).
    """
    script = script.with_outro_cta(settings.outro_cta)
    return "\n\n".join(normalize_for_tts(b.narration.strip()) for b in script.beats)


def _wav_duration(path: Path) -> tuple[float, int]:
    """Return (duration_s, sample_rate) by reading the WAV header."""
    with wave.open(str(path), "rb") as r:
        frames = r.getnframes()
        rate = r.getframerate()
    return (frames / rate if rate else 0.0), rate


def _normalize_wav_header(path: Path) -> None:
    """Rewrite the WAV header in place with the true frame count.

    Kokoro-FastAPI streams the WAV and leaves a 0xFFFFFFFF placeholder in the
    data-chunk size, so getnframes() reports INT_MAX. Anything that trusts the
    header — our _wav_duration AND video.assemble_video's _wav_duration_s, which
    drives every beat/segment duration — would otherwise compute a nonsense
    length. Re-read the real PCM bytes (readframes reads to EOF despite the bogus
    count) and rewrite a correct header. No-op-safe on already-correct WAVs.
    """
    with wave.open(str(path), "rb") as r:
        params = r.getparams()
        data = r.readframes(2**31 - 1)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(params.nchannels)
        w.setsampwidth(params.sampwidth)
        w.setframerate(params.framerate)
        w.writeframes(data)
    tmp.replace(path)


def _write_voice_meta(output_path: Path, voice: str, *, backend: str, **extra) -> None:
    """Sidecar JSON so /upload can persist the voice used without opening the WAV.

    `voice` key is read by /upload — keep it. `backend` + `extra` are additive.
    """
    meta = {"voice": voice, "backend": backend, **extra}
    output_path.with_suffix(".meta.json").write_text(json.dumps(meta, indent=2))


def _voice_result(output_path: Path, voice: str, duration_s: float, rate: int) -> dict:
    return {
        "audio_path": str(output_path),
        "duration_s": round(duration_s, 2),
        "size_bytes": output_path.stat().st_size,
        "voice": voice,
        "sample_rate": rate,
    }


async def ensure_voice_downloaded(voice: str) -> Path:
    """Download .onnx + .onnx.json if missing for `voice`. Returns the .onnx path."""
    locale, speaker, quality = _parse_voice(voice)

    settings.piper_voices_dir.mkdir(parents=True, exist_ok=True)
    onnx_path = settings.piper_voices_dir / f"{voice}.onnx"
    cfg_path = settings.piper_voices_dir / f"{voice}.onnx.json"

    lang_root = locale.split("_")[0]  # en_US → en
    base = f"{PIPER_HF_BASE}/{lang_root}/{locale}/{speaker}/{quality}/{voice}"

    targets = [
        (onnx_path, f"{base}.onnx"),
        (cfg_path, f"{base}.onnx.json"),
    ]

    # Atomic download: stream into a .part file, rename when complete.
    # Avoids the failure mode where a previous interrupted download leaves a
    # truncated .onnx that passes a naive "file exists" check.
    async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as client:
        for path, url in targets:
            if path.exists() and path.stat().st_size > 0:
                continue
            tmp = path.with_suffix(path.suffix + ".part")
            log.info("downloading %s", url)
            try:
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    with tmp.open("wb") as f:
                        async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                            f.write(chunk)
                tmp.rename(path)
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
            log.info(
                "downloaded → %s (%.1f MB)",
                path.name,
                path.stat().st_size / 1e6,
            )

    return onnx_path


def synthesize_to_wav(
    script: Script,
    output_path: Path,
    voice: str,
) -> dict:
    """Render the 3-beat narration + CTA to a single WAV using `voice`.
    Also writes a sidecar JSON next to the WAV recording the voice choice."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    onnx_path = settings.piper_voices_dir / f"{voice}.onnx"
    if not onnx_path.exists():
        raise RuntimeError(
            f"voice file missing: {onnx_path}. Call ensure_voice_downloaded() first."
        )

    piper = PiperVoice.load(str(onnx_path))

    text = _build_narration_text(script)

    # length_scale > 1 slows speech. API moved between Piper versions; try the
    # SynthesisConfig path (current piper-tts ≥1.3), fall back to plain kwarg.
    try:
        from piper.config import SynthesisConfig  # type: ignore
        syn = SynthesisConfig(length_scale=settings.piper_length_scale)
        with wave.open(str(output_path), "wb") as wav_file:
            piper.synthesize_wav(text, wav_file, syn_config=syn)
    except (ImportError, TypeError):
        with wave.open(str(output_path), "wb") as wav_file:
            piper.synthesize_wav(text, wav_file)

    duration_s, rate = _wav_duration(output_path)
    _write_voice_meta(
        output_path, voice, backend="piper", length_scale=settings.piper_length_scale
    )
    return _voice_result(output_path, voice, duration_s, rate)


async def _synthesize_via_kokoro(script: Script, output_path: Path, voice: str) -> dict:
    """Render narration via the Kokoro-FastAPI OpenAI-compatible endpoint.

    POSTs to /v1/audio/speech and writes the returned WAV. Raises on any HTTP /
    network / format error so the caller can fall back to Piper.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    text = _build_narration_text(script)
    payload = {
        "model": settings.kokoro_model,
        "input": text,
        "voice": voice,
        "response_format": "wav",
        "speed": settings.kokoro_speed,
    }
    url = f"{settings.kokoro_base_url.rstrip('/')}/v1/audio/speech"
    async with httpx.AsyncClient(timeout=settings.kokoro_timeout_s) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        audio = resp.content
    if not audio:
        raise RuntimeError("kokoro returned empty audio body")
    output_path.write_bytes(audio)
    _normalize_wav_header(output_path)  # fix streamed-WAV INT_MAX frame count

    duration_s, rate = _wav_duration(output_path)
    _write_voice_meta(
        output_path, voice, backend="kokoro", speed=settings.kokoro_speed
    )
    return _voice_result(output_path, voice, duration_s, rate)


async def synthesize(script: Script, output_path: Path) -> dict:
    """Render narration with the configured backend; always falls back to Piper.

    Mirrors the image Space→mflux pattern: try the (higher-quality but
    provenance-uncertain) Kokoro container, and on ANY failure fall back to the
    license-clean local Piper so a run always completes.
    """
    if settings.voice_backend == "kokoro":
        voice = pick_kokoro_voice()
        try:
            log.info("kokoro synth voice=%s → %s", voice, output_path.name)
            return await _synthesize_via_kokoro(script, output_path, voice)
        except Exception as e:
            log.warning(
                "kokoro backend failed (%s) — falling back to piper", str(e)[:200]
            )

    voice = pick_voice()
    await ensure_voice_downloaded(voice)
    log.info("piper synth voice=%s → %s", voice, output_path.name)
    return synthesize_to_wav(script, output_path, voice)
