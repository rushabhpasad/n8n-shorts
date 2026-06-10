"""Image gen — Z-Image-Turbo via mflux (MLX, Apple Silicon).

Generates one image per entry in script.image_prompts. Count is variable
(5–7 per the script schema). Sequential — model can't run two diffusions
concurrently on a single GPU.

Lazy-loaded module-global model. First call downloads ~15GB from
Tongyi-MAI/Z-Image-Turbo. Subsequent calls just generate.

We invoke from FastAPI via asyncio.to_thread so the event loop stays free.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from config import settings
from models import Script

log = logging.getLogger("shorts-api.image")

_model = None
_model_lock = threading.Lock()


def _get_model():
    global _model
    if _model is not None:
        return _model

    with _model_lock:
        if _model is not None:
            return _model

        log.info(
            "loading ZImage(z-image-turbo) quantize=%d (first call downloads ~15GB)",
            settings.mflux_quantize,
        )

        from mflux.models.common.config import ModelConfig
        from mflux.models.z_image.variants.z_image import ZImage

        _model = ZImage(
            model_config=ModelConfig.z_image_turbo(),
            quantize=settings.mflux_quantize,
        )
        log.info("ZImage loaded and resident")

    return _model


def warmup() -> dict:
    _get_model()
    return {
        "loaded": True,
        "model": settings.mflux_model,
        "quantize": settings.mflux_quantize,
    }


def generate_images(script: Script, output_dir: Path, word_id: int) -> list[dict]:
    """Generate len(script.image_prompts) PNGs (5–7) into output_dir."""
    n = len(script.image_prompts)
    if n < 1:
        raise ValueError("script has no image_prompts")
    output_dir.mkdir(parents=True, exist_ok=True)

    model = _get_model()
    from mflux.utils.image_util import ImageUtil

    results: list[dict] = []
    for i, prompt in enumerate(script.image_prompts):
        out_path = output_dir / f"word_{word_id:04d}_{i}.png"
        seed = settings.mflux_seed_base + i
        log.info(
            "[%d/%d] generate seed=%d %dx%d steps=%d guidance=%.1f → %s",
            i + 1, n, seed,
            settings.mflux_width, settings.mflux_height,
            settings.mflux_steps, settings.mflux_guidance,
            out_path.name,
        )
        image = model.generate_image(
            seed=seed,
            prompt=prompt,
            negative_prompt="",
            width=settings.mflux_width,
            height=settings.mflux_height,
            guidance=settings.mflux_guidance,
            num_inference_steps=settings.mflux_steps,
            scheduler=settings.mflux_scheduler,
        )
        ImageUtil.save_image(image=image, path=str(out_path))

        results.append({
            "image_idx": i,
            "image_path": str(out_path),
            "prompt": prompt,
            "seed": seed,
            "width": settings.mflux_width,
            "height": settings.mflux_height,
            "steps": settings.mflux_steps,
            "size_bytes": out_path.stat().st_size,
        })

    return results
