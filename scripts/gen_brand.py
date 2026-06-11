#!/usr/bin/env python3
"""Generate channel brand artifacts (icons, banners, watermarks) via Z-Image-Turbo.

Run from inside the api venv (depends on mflux + Pillow):

  uv run --project api scripts/gen_brand.py --channel <slug> --type icon
  uv run --project api scripts/gen_brand.py --channel <slug> --type banner
  uv run --project api scripts/gen_brand.py --channel <slug> --type watermark

Outputs:

  assets/brand/<channel>/icon_<name>.png      (1024×1024 square)
  assets/brand/<channel>/banner_<name>.png    (2048×1152, YouTube banner spec)
  assets/brand/<channel>/watermark_<name>.png (512×512 square)

Defaults differ per type:

  type        width   height   steps   seed-base
  icon        1024    1024     8       11
  banner      2048    1152     6       21
  watermark    512     512     8       31

Reads channels/<slug>/brand.json for the prompts list of each type. Each list
entry has {name, concept, prompt}.

Optional flags:
  --width/--height/--steps/--seed/--quantize  to override defaults
  --only <name>  to render a single prompt by name
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

# (prompts_key, width, height, steps, seed_base, filename_prefix)
TYPE_DEFAULTS = {
    "icon":      ("icon_prompts",      1024, 1024, 8, 11, "icon"),
    "banner":    ("banner_prompts",    2048, 1152, 6, 21, "banner"),
    "watermark": ("watermark_prompts",  512,  512, 8, 31, "watermark"),
}


def load_brand(channel: str) -> dict:
    path = CHANNELS_DIR / channel / "brand.json"
    if not path.exists():
        raise FileNotFoundError(f"no brand.json for channel {channel}: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--channel", required=True, help="channel slug (must match channels/<slug>/)")
    ap.add_argument("--type", choices=list(TYPE_DEFAULTS.keys()), default="icon",
                    help="artifact type to render (default: icon)")
    ap.add_argument("--width", type=int, default=None)
    ap.add_argument("--height", type=int, default=None)
    ap.add_argument("--steps", type=int, default=None, help="diffusion steps")
    ap.add_argument("--seed", type=int, default=None, help="base seed; per-prompt offset added")
    ap.add_argument("--quantize", type=int, default=8)
    ap.add_argument("--only", default=None, help="only generate the prompt with this name")
    args = ap.parse_args()

    prompts_key, def_w, def_h, def_steps, def_seed, prefix = TYPE_DEFAULTS[args.type]
    width = args.width if args.width is not None else def_w
    height = args.height if args.height is not None else def_h
    steps = args.steps if args.steps is not None else def_steps
    seed_base = args.seed if args.seed is not None else def_seed

    brand = load_brand(args.channel)
    prompts = brand.get(prompts_key, [])
    if not prompts:
        log.error("no %s in channels/%s/brand.json", prompts_key, args.channel)
        return 1
    if args.only:
        prompts = [p for p in prompts if p["name"] == args.only]
        if not prompts:
            log.error("no %s named %s in channels/%s/brand.json", prompts_key, args.only, args.channel)
            return 1

    out_dir = BRAND_OUT / args.channel
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info(
        "loading ZImage(z-image-turbo) quantize=%d for %d %s(s) at %dx%d, %d steps",
        args.quantize, len(prompts), args.type, width, height, steps,
    )
    from mflux.models.common.config import ModelConfig
    from mflux.models.z_image.variants.z_image import ZImage
    from mflux.utils.image_util import ImageUtil

    model = ZImage(
        model_config=ModelConfig.z_image_turbo(),
        quantize=args.quantize,
    )

    for i, p in enumerate(prompts):
        out_path = out_dir / f"{prefix}_{p['name']}.png"
        if out_path.exists():
            out_path.unlink()  # mflux save_image otherwise appends _1
        seed = seed_base + i
        log.info("[%d/%d] %s (seed=%d)", i + 1, len(prompts), out_path.name, seed)
        t0 = time.perf_counter()
        image = model.generate_image(
            seed=seed,
            prompt=p["prompt"],
            negative_prompt="",
            width=width,
            height=height,
            guidance=1.0,
            num_inference_steps=steps,
            scheduler="flow_match_euler_discrete",
        )
        ImageUtil.save_image(image=image, path=str(out_path))
        log.info(
            "  ↳ wrote %s (%.1f MB) in %.1fs",
            out_path.name,
            out_path.stat().st_size / 1e6,
            time.perf_counter() - t0,
        )

    log.info("done — %d %s(s) in %s", len(prompts), args.type, out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
