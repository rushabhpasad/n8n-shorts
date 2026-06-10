#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Generate square channel-icon candidates from channels/<slug>/brand.json.

Reuses the Z-Image-Turbo model that shorts-api keeps resident via /image/warmup,
so this script runs *inside* the api venv via uv. Output:

  assets/brand/<channel>/icon_<name>.png   (1024×1024 square)

Run on the host where mflux + Z-Image-Turbo are installed (i.e. stl):

  uv run --project api scripts/gen_brand.py --channel the-mythscape
  uv run --project api scripts/gen_brand.py --channel open-verdicts
  uv run --project api scripts/gen_brand.py --channel bright-beasts

Optional: --steps 8 (default), --width 1024 --height 1024 (square),
--only v1-altar (single prompt by name).

This shares the Z-Image-Turbo download with shorts-api — first call may take
a minute to load the model into MLX memory if shorts-api isn't running.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gen_brand")

REPO_ROOT = Path(__file__).resolve().parent.parent
CHANNELS_DIR = REPO_ROOT / "channels"
BRAND_OUT = REPO_ROOT / "assets" / "brand"


def load_brand(channel: str) -> dict:
    path = CHANNELS_DIR / channel / "brand.json"
    if not path.exists():
        raise FileNotFoundError(f"no brand.json for channel {channel}: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--channel", required=True, help="channel slug (must match channels/<slug>/)")
    ap.add_argument("--width", type=int, default=1024)
    ap.add_argument("--height", type=int, default=1024)
    ap.add_argument("--steps", type=int, default=8, help="diffusion steps (turbo: 8)")
    ap.add_argument("--seed", type=int, default=11, help="base seed; per-prompt offset added")
    ap.add_argument("--quantize", type=int, default=8)
    ap.add_argument("--only", default=None, help="only generate the prompt with this name")
    args = ap.parse_args()

    brand = load_brand(args.channel)
    icons = brand["icon_prompts"]
    if args.only:
        icons = [p for p in icons if p["name"] == args.only]
        if not icons:
            log.error("no icon prompt named %s in channels/%s/brand.json", args.only, args.channel)
            return 1

    out_dir = BRAND_OUT / args.channel
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info(
        "loading ZImage(z-image-turbo) quantize=%d for %d icon(s) at %dx%d, %d steps",
        args.quantize, len(icons), args.width, args.height, args.steps,
    )
    from mflux.models.common.config import ModelConfig
    from mflux.models.z_image.variants.z_image import ZImage
    from mflux.utils.image_util import ImageUtil

    model = ZImage(
        model_config=ModelConfig.z_image_turbo(),
        quantize=args.quantize,
    )

    for i, icon in enumerate(icons):
        out_path = out_dir / f"icon_{icon['name']}.png"
        if out_path.exists():
            out_path.unlink()  # mflux save_image otherwise appends _1
        seed = args.seed + i
        log.info("[%d/%d] %s (seed=%d)", i + 1, len(icons), out_path.name, seed)
        t0 = time.perf_counter()
        image = model.generate_image(
            seed=seed,
            prompt=icon["prompt"],
            negative_prompt="",
            width=args.width,
            height=args.height,
            guidance=1.0,
            num_inference_steps=args.steps,
            scheduler="flow_match_euler_discrete",
        )
        ImageUtil.save_image(image=image, path=str(out_path))
        log.info(
            "  ↳ wrote %s (%.1f MB) in %.1fs",
            out_path.name,
            out_path.stat().st_size / 1e6,
            time.perf_counter() - t0,
        )

    log.info("done — %d icon(s) in %s", len(icons), out_dir)
    log.info("pick your favorite, upload as the channel profile picture in YouTube Studio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
