#!/usr/bin/env python3
"""Generate channel brand artifacts (icons, banners, watermarks).

Run from inside the api venv (depends on mflux + Pillow):

  uv run --project api scripts/gen_brand.py --channel <slug> --type icon
  uv run --project api scripts/gen_brand.py --channel <slug> --type banner
  uv run --project api scripts/gen_brand.py --channel <slug> --type watermark

Outputs:

  assets/brand/<channel>/icon_<name>.png      (1024×1024 square, diffusion)
  assets/brand/<channel>/banner_<name>.png    (2048×1152, diffusion)
  assets/brand/<channel>/watermark.png        (512×512 square, PIL resize of approved icon)

The watermark is intentionally NOT diffusion-generated — YouTube's watermark
is a tiny corner overlay (~150×150 on playback), and brand consistency wants
the same visual mark the channel icon uses. We just resize the approved icon.

Defaults for diffusion types:

  type        width   height   steps   seed-base
  icon        1024    1024     8       11
  banner      2048    1152     6       21

Reads channels/<slug>/brand.json. For icon/banner: reads {icon,banner}_prompts.
For watermark: reads `approved_icon` and loads the corresponding icon PNG.

Optional flags (diffusion types only):
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

# Diffusion-generated artifacts only.
# (prompts_key, width, height, steps, seed_base, filename_prefix)
TYPE_DEFAULTS = {
    "icon":      ("icon_prompts",      1024, 1024, 8, 11, "icon"),
    "banner":    ("banner_prompts",    2048, 1152, 6, 21, "banner"),
}

WATERMARK_SIZE = 512  # output size for the resized-from-icon watermark


def load_brand(channel: str) -> dict:
    path = CHANNELS_DIR / channel / "brand.json"
    if not path.exists():
        raise FileNotFoundError(f"no brand.json for channel {channel}: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def make_watermark(channel: str) -> int:
    """Resize the channel's approved icon into a watermark.png."""
    from PIL import Image

    brand = load_brand(channel)
    approved = brand.get("approved_icon")
    if not approved:
        log.error("channels/%s/brand.json has no approved_icon", channel)
        return 1
    out_dir = BRAND_OUT / channel
    src = out_dir / f"icon_{approved}.png"
    dst = out_dir / "watermark.png"
    if not src.exists():
        log.error("approved icon not found: %s", src)
        return 1
    img = Image.open(src).convert("RGBA")
    img.thumbnail((WATERMARK_SIZE, WATERMARK_SIZE), Image.Resampling.LANCZOS)
    img.save(dst, "PNG", optimize=True)
    log.info("%s: %s -> %s (%dx%d, %.0f KB)",
             channel, src.name, dst.name,
             img.size[0], img.size[1], dst.stat().st_size / 1024)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--channel", required=True, help="channel slug (must match channels/<slug>/)")
    ap.add_argument("--type", choices=list(TYPE_DEFAULTS.keys()) + ["watermark"], default="icon",
                    help="artifact type (default: icon)")
    ap.add_argument("--width", type=int, default=None)
    ap.add_argument("--height", type=int, default=None)
    ap.add_argument("--steps", type=int, default=None, help="diffusion steps")
    ap.add_argument("--seed", type=int, default=None, help="base seed; per-prompt offset added")
    ap.add_argument("--quantize", type=int, default=8)
    ap.add_argument("--only", default=None, help="only generate the prompt with this name")
    args = ap.parse_args()

    if args.type == "watermark":
        return make_watermark(args.channel)

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
