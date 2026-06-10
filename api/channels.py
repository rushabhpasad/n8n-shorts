"""Channel registry — loads channels/<slug>/channel.json into a typed config.

Channels are discovered at runtime by listing the directories under
`channels_dir`. Each must contain a `channel.json` and a `prompts/` directory.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from pydantic import BaseModel, Field

log = logging.getLogger("shorts-api.channels")


CHANNELS_DIR = Path(__file__).resolve().parent.parent / "channels"


class ChannelConfig(BaseModel):
    slug: str
    name: str
    handle: str | None = None
    tagline: str | None = None
    topic: str
    description_template: str
    default_categories: list[str] = Field(default_factory=list)
    ai_disclosure: bool = True
    shorts_max_duration_s: int = 180
    content_safety: str | None = None

    @property
    def dir(self) -> Path:
        return CHANNELS_DIR / self.slug

    @property
    def prompts_dir(self) -> Path:
        return self.dir / "prompts"

    @property
    def script_prompt_path(self) -> Path:
        return self.prompts_dir / "script.md"

    @property
    def image_prompt_path(self) -> Path:
        return self.prompts_dir / "image.md"

    @property
    def words_csv_path(self) -> Path:
        return self.dir / "words.csv"


@lru_cache(maxsize=None)
def load(slug: str) -> ChannelConfig:
    cfg_path = CHANNELS_DIR / slug / "channel.json"
    if not cfg_path.exists():
        raise FileNotFoundError(f"no channel config at {cfg_path}")
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg = ChannelConfig.model_validate(data)
    if cfg.slug != slug:
        raise ValueError(
            f"channel.json slug mismatch: dir={slug} but slug={cfg.slug}"
        )
    return cfg


def list_slugs() -> list[str]:
    if not CHANNELS_DIR.exists():
        return []
    return sorted(
        p.name
        for p in CHANNELS_DIR.iterdir()
        if p.is_dir() and (p / "channel.json").exists()
    )


def all_configs() -> Iterable[ChannelConfig]:
    for slug in list_slugs():
        yield load(slug)
