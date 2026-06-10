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
import re
import wave
from pathlib import Path

import httpx
from piper import PiperVoice

from config import settings
from models import Script

log = logging.getLogger("shorts-api.voice")

# rhasspy/piper-voices on Hugging Face:
# https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US/<speaker>/<quality>/
PIPER_HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

# Strip paired markdown emphasis (*bold* / _italic_) so Piper doesn't say
# "asterisk nostos asterisk" out loud.
_EMPH_RE = re.compile(r"(\*+)([^*]+?)\1|(_+)([^_]+?)\3")

# Strip emoji codepoints so Piper doesn't try to speak them.
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
_WS_RE = re.compile(r"\s+")


def _normalize_for_tts(text: str) -> str:
    text = _EMPH_RE.sub(lambda m: m.group(2) or m.group(4) or "", text)
    text = _EMOJI_RE.sub("", text)
    return _WS_RE.sub(" ", text).strip()


def _parse_voice(voice: str) -> tuple[str, str, str]:
    """'en_US-norman-medium' → ('en_US', 'norman', 'medium')."""
    parts = voice.split("-")
    if len(parts) != 3:
        raise ValueError(
            f"piper voice must be 'locale-speaker-quality', got {voice!r}"
        )
    return parts[0], parts[1], parts[2]


def pick_voice() -> str:
    """Uniformly random pick from the configured pool. Errors if pool empty."""
    pool = settings.piper_voices
    if not pool:
        raise RuntimeError("settings.piper_voices is empty")
    return random.choice(pool)


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

    # Append outro CTA to the last beat (no-op if empty).
    script = script.with_outro_cta(settings.outro_cta)

    text = "\n\n".join(_normalize_for_tts(b.narration.strip()) for b in script.beats)

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

    with wave.open(str(output_path), "rb") as r:
        frames = r.getnframes()
        rate = r.getframerate()
        duration_s = frames / rate if rate else 0.0

    # Sidecar — recorded so /upload can persist which voice was used to the
    # audit trail without having to peek inside the WAV.
    meta_path = output_path.with_suffix(".meta.json")
    meta_path.write_text(
        json.dumps({"voice": voice, "length_scale": settings.piper_length_scale}, indent=2)
    )

    return {
        "audio_path": str(output_path),
        "duration_s": round(duration_s, 2),
        "size_bytes": output_path.stat().st_size,
        "voice": voice,
        "sample_rate": rate,
    }
