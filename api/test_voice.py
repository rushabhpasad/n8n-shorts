"""Tests for the voice service: shared text build + backend dispatch/fallback.

These avoid the real Piper/Kokoro stacks — Piper synthesis and the Kokoro HTTP
call are monkeypatched. Async paths are driven via asyncio.run so no pytest
async plugin is required.
"""

import asyncio
import io
import wave

import pytest

import services.voice as voice
from models import Beat, Script, YouTubeMeta


def _make_script() -> Script:
    return Script(
        word="amelia earhart disappearance",
        pronunciation="uh-MEE-lee-uh AIR-hart",
        title_text="The Vanishing",
        tagline="A flight that never landed.",
        beats=[
            Beat(label="hook", narration="She took off and was never seen again.",
                 on_screen="1937", images=["a vintage plane", "an ocean horizon"]),
            Beat(label="origin", narration="The search covered thousands of miles.",
                 on_screen="Pacific", images=["a nautical map"]),
            Beat(label="payoff", narration="No wreckage was ever confirmed.",
                 on_screen="Mystery", images=["a fading photograph"]),
        ],
        youtube=YouTubeMeta(
            title="The Amelia Earhart Mystery",
            description="What happened on that final flight over the Pacific?",
            tags=["history", "mystery", "aviation"],
        ),
    )


def _tiny_wav_bytes(rate: int = 24000, frames: int = 12000) -> bytes:
    """A valid mono 16-bit WAV so _wav_duration can read a real header."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * frames)
    return buf.getvalue()


def test_build_narration_text_joins_beats_and_appends_cta(monkeypatch):
    monkeypatch.setattr(voice.settings, "outro_cta", "Follow for more.")
    text = voice._build_narration_text(_make_script())

    # All three beats present, joined with blank-line separators, CTA last.
    # (with_outro_cta adds its own \n\n inside the final beat, so chunk count > 3.)
    assert "\n\n" in text
    assert "never seen again" in text          # hook beat
    assert "search covered" in text            # origin beat
    assert text.strip().endswith("Follow for more.")  # CTA on the last beat


def test_pick_kokoro_voice_empty_pool_raises(monkeypatch):
    monkeypatch.setattr(voice.settings, "kokoro_voices", [])
    with pytest.raises(RuntimeError, match="kokoro_voices is empty"):
        voice.pick_kokoro_voice()


def test_synthesize_piper_backend_skips_kokoro(monkeypatch):
    monkeypatch.setattr(voice.settings, "voice_backend", "piper")

    async def _no_download(_v):
        return None

    called = {}
    monkeypatch.setattr(voice, "pick_voice", lambda: "en_US-john-medium")
    monkeypatch.setattr(voice, "ensure_voice_downloaded", _no_download)
    monkeypatch.setattr(
        voice, "synthesize_to_wav",
        lambda s, p, v: {"voice": v, "backend": "piper"},
    )

    async def _kokoro(*a, **k):
        called["kokoro"] = True
    monkeypatch.setattr(voice, "_synthesize_via_kokoro", _kokoro)

    result = asyncio.run(voice.synthesize(_make_script(), voice.Path("/tmp/x.wav")))

    assert result == {"voice": "en_US-john-medium", "backend": "piper"}
    assert "kokoro" not in called   # piper backend must not touch kokoro


def test_synthesize_kokoro_success_skips_piper(monkeypatch):
    monkeypatch.setattr(voice.settings, "voice_backend", "kokoro")
    monkeypatch.setattr(voice.settings, "kokoro_voices", ["af_heart"])

    async def _kokoro(s, p, v):
        return {"voice": v, "backend": "kokoro"}
    monkeypatch.setattr(voice, "_synthesize_via_kokoro", _kokoro)

    def _boom():
        raise AssertionError("piper must not be reached on kokoro success")
    monkeypatch.setattr(voice, "pick_voice", _boom)

    result = asyncio.run(voice.synthesize(_make_script(), voice.Path("/tmp/x.wav")))
    assert result == {"voice": "af_heart", "backend": "kokoro"}


def test_synthesize_kokoro_failure_falls_back_to_piper(monkeypatch):
    monkeypatch.setattr(voice.settings, "voice_backend", "kokoro")
    monkeypatch.setattr(voice.settings, "kokoro_voices", ["af_heart"])

    async def _kokoro(s, p, v):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(voice, "_synthesize_via_kokoro", _kokoro)

    async def _no_download(_v):
        return None
    monkeypatch.setattr(voice, "pick_voice", lambda: "en_US-joe-medium")
    monkeypatch.setattr(voice, "ensure_voice_downloaded", _no_download)
    monkeypatch.setattr(
        voice, "synthesize_to_wav",
        lambda s, p, v: {"voice": v, "backend": "piper"},
    )

    result = asyncio.run(voice.synthesize(_make_script(), voice.Path("/tmp/x.wav")))
    assert result == {"voice": "en_US-joe-medium", "backend": "piper"}  # fell back


def test_normalize_wav_header_fixes_intmax_framecount(tmp_path):
    # Kokoro-FastAPI streams WAV with a 0xFFFFFFFF data-chunk size → INT_MAX frames.
    # Forge that: a valid canonical-header WAV with the size fields overwritten.
    out = tmp_path / "streamed.wav"
    out.write_bytes(_tiny_wav_bytes(rate=24000, frames=24000))  # 1.0s, 44-byte header
    raw = bytearray(out.read_bytes())
    raw[4:8] = (0xFFFFFFFF).to_bytes(4, "little")    # RIFF chunk size
    raw[40:44] = (0xFFFFFFFF).to_bytes(4, "little")  # data chunk size
    out.write_bytes(raw)

    # Sanity: the forged header reports the bogus count before normalization.
    assert wave.open(str(out), "rb").getnframes() == (0xFFFFFFFF // 2)

    voice._normalize_wav_header(out)

    dur, rate = voice._wav_duration(out)
    assert rate == 24000
    assert dur == 1.0   # true length recovered, not 0xFFFFFFFF/2 frames


def test_synthesize_via_kokoro_posts_payload_and_writes_wav(tmp_path, monkeypatch):
    monkeypatch.setattr(voice.settings, "outro_cta", "")
    monkeypatch.setattr(voice.settings, "kokoro_base_url", "http://localhost:8880/")
    monkeypatch.setattr(voice.settings, "kokoro_model", "kokoro")
    monkeypatch.setattr(voice.settings, "kokoro_speed", 0.9)
    captured = {}
    wav = _tiny_wav_bytes(rate=24000, frames=24000)  # 1.0s of audio

    class FakeResp:
        content = wav

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            captured["url"] = url
            captured["json"] = json
            return FakeResp()

    monkeypatch.setattr(voice.httpx, "AsyncClient", FakeClient)

    out = tmp_path / "word_0009.wav"
    result = asyncio.run(voice._synthesize_via_kokoro(_make_script(), out, "af_bella"))

    # request shape
    assert captured["url"] == "http://localhost:8880/v1/audio/speech"  # rstrip slash
    assert captured["json"]["voice"] == "af_bella"
    assert captured["json"]["response_format"] == "wav"
    assert captured["json"]["speed"] == 0.9
    assert "never seen again" in captured["json"]["input"]

    # written artifacts
    assert out.read_bytes() == wav
    assert result["sample_rate"] == 24000
    assert result["duration_s"] == 1.0
    meta = __import__("json").loads(out.with_suffix(".meta.json").read_text())
    assert meta["backend"] == "kokoro" and meta["voice"] == "af_bella"
