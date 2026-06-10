"""Runtime config — loaded from environment, optional .env file."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # API server
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=7860)

    # Ollama (script LLM)
    ollama_url: str = Field(default="http://localhost:11434")
    ollama_model: str = Field(default="gemma4:latest")
    ollama_timeout_s: float = Field(default=120.0)

    # mflux (image gen)
    # Picked z-image-turbo (Tongyi/Alibaba, open) after:
    #   - schnell, dev, flux2-klein: all gated by Black Forest Labs
    #   - qwen-image: open but ~40s/step on Apple Silicon → 50 min per 3-image video, not workable
    # z-image-turbo is distilled (4-8 steps), much smaller, and designed for speed.
    mflux_model: str = Field(default="z-image-turbo")
    mflux_quantize: int = Field(default=8)
    mflux_steps: int = Field(default=8)            # turbo's sweet spot
    mflux_guidance: float = Field(default=1.0)     # distilled → guidance=1
    mflux_scheduler: str = Field(default="flow_match_euler_discrete")
    mflux_width: int = Field(default=768)
    mflux_height: int = Field(default=1344)
    mflux_seed_base: int = Field(default=42)

    # Piper TTS — pool of voices; /voice picks one uniformly at random per call.
    # All listed voices must be commercial-use-OK (LibriVox public-domain, CC0,
    # or equivalent). Do NOT add ryan-*, hfc_*, kathleen-*, or danny-* — those
    # trace back to NC-licensed datasets that block YouTube monetization.
    piper_voices: list[str] = Field(default=[
        "en_US-norman-medium",  # LibriVox public domain  (15.5h training)
        "en_US-john-medium",    # LibriVox public domain  (12.5h training)
        "en_US-bryce-medium",   # public domain (own recording, conversational)
        "en_US-joe-medium",     # CC0 (OHF-Voice)
    ])
    piper_voices_dir: Path = Field(default=Path.home() / "etymology-shorts" / "assets" / "piper")
    # length_scale: 1.0 = normal, >1 = slower. 1.1 → 10% slower per user request.
    piper_length_scale: float = Field(default=1.1)

    # Caption-vs-audio sync: shift each live caption's start window earlier by
    # this much. 0.0 means use the proportional-timing math as-is. Bump it up
    # if captions visibly trail the voice. Proper fix is whisper-based forced
    # alignment.
    caption_lead_s: float = Field(default=0.0)

    # CTA spoken at the end of every video. The same text appears as a
    # final live-caption sentence on top of the dedicated outro card
    # (rendered by video.py — a branded SUBSCRIBE-button frame shown for
    # exactly the CTA's audio duration). Empty string disables both
    # the spoken CTA and the outro card.
    outro_cta: str = Field(
        default=(
            "Hey! If you enjoyed the content, please like, share with a friend, "
            "and subscribe for more! Don't forget to hit the bell icon so you "
            "never miss an upload. Thanks for the support!"
        )
    )

    # Pause inserted before the CTA narration starts. The Piper "\n\n" hint
    # in the script gives a natural prosodic break, and we shift the outro
    # card's appearance later by the same amount so the last narration image
    # lingers for a moment before the SUBSCRIBE card appears.
    outro_pause_s: float = Field(default=0.25)

    # Storage. Per-channel output lives at `<data_dir>/<channel>/{scripts,audio,images,videos}`.
    data_dir: Path = Field(default=Path.home() / "etymology-shorts" / "output")
    db_path: Path = Field(default=Path.home() / "etymology-shorts" / "state.db")

    # YouTube — secrets are per-channel:
    #   secrets/youtube_oauth.<channel>.json
    #   secrets/youtube_token.<channel>.json
    secrets_dir: Path = Field(
        default=Path.home() / "etymology-shorts" / "secrets",
    )

    def channel_data_dir(self, channel: str) -> Path:
        return self.data_dir / channel

    def youtube_oauth_path(self, channel: str) -> Path:
        return self.secrets_dir / f"youtube_oauth.{channel}.json"

    def youtube_token_path(self, channel: str) -> Path:
        return self.secrets_dir / f"youtube_token.{channel}.json"


settings = Settings()
