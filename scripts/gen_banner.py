"""Generate the YouTube channel banner for Wordstrata.

Steps:
1. mflux: generate a 1024×576 painterly horizontal earth-strata background.
2. PIL: upscale to 2048×1152 (YouTube full-canvas spec).
3. PIL: add a soft elliptical darken in the safe-zone for text readability.
4. PIL: composite "Wordstrata" (title, large) + tagline inside the 1235×338
   safe-zone so it's visible on every device.
5. Save as banner_2048x1152.png.

Run on stl after gen_icons.py:
  eval "$(/opt/homebrew/bin/brew shellenv)"
  cd ~/etymology-shorts/api
  uv run python ~/etymology-shorts/scripts/gen_banner.py
"""

from __future__ import annotations

import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from mflux.models.common.config import ModelConfig
from mflux.models.z_image.variants.z_image import ZImage
from mflux.utils.image_util import ImageUtil

OUT_DIR = Path.home() / "etymology-shorts" / "assets" / "brand"
OUT_DIR.mkdir(parents=True, exist_ok=True)

BANNER_PATH = OUT_DIR / "banner_2048x1152.png"
BG_RAW_PATH = OUT_DIR / "banner_bg_1024x576.png"

# Generate small, upscale large (Z-Image-Turbo's comfort zone is ~1024)
W_GEN, H_GEN = 1024, 576
W_FINAL, H_FINAL = 2048, 1152

# YouTube safe zone: 1235×338 centered. Everything inside must be readable
# on every device size.
SAFE_W, SAFE_H = 1235, 338
SAFE_X = (W_FINAL - SAFE_W) // 2          # 406
SAFE_Y = (H_FINAL - SAFE_H) // 2          # 407

PROMPT = (
    "painterly illustration, oil-on-canvas texture, muted earth tones, "
    "soft natural light, sweeping horizontal painting of layered geological "
    "earth strata, warm ochre and umber sediment bands stretching across a "
    "vast underground cross-section, dramatic shadows on left and right "
    "edges with softer central tones, ancient archaeological feel, "
    "atmospheric cinematic widescreen composition, no text, no captions, "
    "no watermarks"
)

FONT_PATH = Path.home() / "etymology-shorts" / "assets" / "fonts" / "Inter-Bold.ttf"

TITLE = "Wordstrata"
TAGLINE = "Every word has buried layers."

TITLE_FONT_SIZE = 180
TAGLINE_FONT_SIZE = 60


def render_bg() -> Path:
    print("Loading ZImage…")
    t0 = time.perf_counter()
    model = ZImage(
        model_config=ModelConfig.z_image_turbo(),
        quantize=8,
    )
    print(f"  loaded in {time.perf_counter() - t0:.1f}s")

    print(f"Generating BG at {W_GEN}x{H_GEN}…")
    t0 = time.perf_counter()
    image = model.generate_image(
        seed=200,
        prompt=PROMPT,
        negative_prompt="",
        width=W_GEN,
        height=H_GEN,
        guidance=1.0,
        num_inference_steps=8,
        scheduler="flow_match_euler_discrete",
    )
    ImageUtil.save_image(image=image, path=str(BG_RAW_PATH))
    print(f"  → {BG_RAW_PATH.name} ({time.perf_counter() - t0:.1f}s)")
    return BG_RAW_PATH


def composite_banner(bg_path: Path) -> None:
    print(f"Upscaling {bg_path.name} to {W_FINAL}×{H_FINAL}…")
    bg = Image.open(bg_path).convert("RGB").resize(
        (W_FINAL, H_FINAL), Image.LANCZOS
    )

    # Soft elliptical darken in the safe zone for text readability
    overlay = Image.new("RGBA", (W_FINAL, H_FINAL), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    ell_w = SAFE_W + 200          # slightly wider than safe zone
    ell_h = SAFE_H + 220
    ex = (W_FINAL - ell_w) // 2
    ey = (H_FINAL - ell_h) // 2
    # Layered ellipses to approximate a gaussian darken
    for i in range(24):
        alpha = int(6 + i * 5)
        inset_x = i * (ell_w // 70)
        inset_y = i * (ell_h // 100)
        od.ellipse(
            (ex + inset_x, ey + inset_y,
             ex + ell_w - inset_x, ey + ell_h - inset_y),
            fill=(0, 0, 0, alpha),
        )
    bg = Image.alpha_composite(bg.convert("RGBA"), overlay).convert("RGB")
    d = ImageDraw.Draw(bg)

    # Title
    title_font = ImageFont.truetype(str(FONT_PATH), TITLE_FONT_SIZE)
    title_bb = title_font.getbbox(TITLE)
    title_w = int(title_bb[2] - title_bb[0])
    title_h = int(title_bb[3] - title_bb[1])
    title_x = (W_FINAL - title_w) // 2 - int(title_bb[0])
    # Title top of the vertically-centered text group (title + gap + tagline)
    tagline_font = ImageFont.truetype(str(FONT_PATH), TAGLINE_FONT_SIZE)
    tag_bb = tagline_font.getbbox(TAGLINE)
    tag_w = int(tag_bb[2] - tag_bb[0])
    tag_h = int(tag_bb[3] - tag_bb[1])
    gap = 24
    total_h = title_h + gap + tag_h
    group_top = SAFE_Y + (SAFE_H - total_h) // 2
    title_y = group_top - int(title_bb[1])
    tag_y = group_top + title_h + gap - int(tag_bb[1])

    print(
        f"Text layout | title @ ({title_x}, {title_y}) w={title_w} h={title_h} | "
        f"tagline @ y={tag_y} w={tag_w} h={tag_h}"
    )

    d.text(
        (title_x, title_y),
        TITLE,
        font=title_font,
        fill=(255, 245, 220),
        stroke_width=8,
        stroke_fill=(0, 0, 0),
    )
    tag_x = (W_FINAL - tag_w) // 2 - int(tag_bb[0])
    d.text(
        (tag_x, tag_y),
        TAGLINE,
        font=tagline_font,
        fill=(220, 200, 165),
        stroke_width=4,
        stroke_fill=(0, 0, 0),
    )

    bg.save(BANNER_PATH)
    print(f"  → {BANNER_PATH}")


def main() -> None:
    bg_path = render_bg()
    composite_banner(bg_path)
    print("Done.")


if __name__ == "__main__":
    main()
