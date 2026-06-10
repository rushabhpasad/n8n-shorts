"""Generate channel icon candidates via Z-Image-Turbo.

Standalone. Loads its own ZImage model instance (independent of the running
shorts-api). Outputs 4 1024×1024 candidates into ~/etymology-shorts/assets/brand/.

Usage on stl:
  eval "$(/opt/homebrew/bin/brew shellenv)"
  cd ~/etymology-shorts/api
  uv run python ~/etymology-shorts/scripts/gen_icons.py
"""

from __future__ import annotations

import time
from pathlib import Path

from mflux.models.common.config import ModelConfig
from mflux.models.z_image.variants.z_image import ZImage
from mflux.utils.image_util import ImageUtil

OUT_DIR = Path.home() / "etymology-shorts" / "assets" / "brand"
OUT_DIR.mkdir(parents=True, exist_ok=True)

W = H = 1024
STEPS = 8
GUIDANCE = 1.0
QUANTIZE = 8

STYLE = (
    "painterly illustration, oil-on-canvas texture, muted earth tones, "
    "soft natural light, "
)
CLOSING = (
    " centered square composition, dark earthy ochre background, "
    "no text, no captions, no watermarks, no words, atmospheric"
)

PROMPTS: list[tuple[str, int, str]] = [
    (
        "icon_strata_w", 100,
        STYLE
        + "a single massive ornate letter W carved into horizontal layers of "
        "warm sandstone strata, deep shadows, archaeological excavation feel,"
        + CLOSING,
    ),
    (
        "icon_petrified_book", 101,
        STYLE
        + "an ancient open leather-bound book viewed from above, its pages "
        "petrified into thin visible layers of warm cream-colored rock strata,"
        + CLOSING,
    ),
    (
        "icon_tablet_w", 102,
        STYLE
        + "a weathered limestone tablet bearing a single boldly carved letter "
        "W rising from layered sediment, soft candlelight from upper left, "
        "archaeological discovery feel,"
        + CLOSING,
    ),
    (
        "icon_fossil_w", 103,
        STYLE
        + "a vertical cross-section of warm geological earth strata, a single "
        "glowing illuminated letterform W partially embedded in the middle "
        "layer like a buried fossil, soft warm rim-light,"
        + CLOSING,
    ),
]


def main() -> None:
    t0 = time.perf_counter()
    print("Loading ZImage (z-image-turbo)…")
    model = ZImage(
        model_config=ModelConfig.z_image_turbo(),
        quantize=QUANTIZE,
    )
    print(f"  loaded in {time.perf_counter() - t0:.1f}s")

    for i, (name, seed, prompt) in enumerate(PROMPTS, start=1):
        out_path = OUT_DIR / f"{name}.png"
        print(f"[{i}/{len(PROMPTS)}] {name}  seed={seed}")
        t1 = time.perf_counter()
        image = model.generate_image(
            seed=seed,
            prompt=prompt,
            negative_prompt="",
            width=W,
            height=H,
            guidance=GUIDANCE,
            num_inference_steps=STEPS,
            scheduler="flow_match_euler_discrete",
        )
        ImageUtil.save_image(image=image, path=str(out_path))
        print(f"  → {out_path.name}  ({time.perf_counter() - t1:.1f}s)")

    print("Done. Pull back with:")
    print(f"  rsync -avh stl:{OUT_DIR}/ ./_samples/brand/")


if __name__ == "__main__":
    main()
