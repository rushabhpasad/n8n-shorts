"""Image gen — hosted Z-Image-Turbo Space first, local mflux as fallback.

Backends (settings.image_backend):
  - "space": call the hosted Z-Image-Turbo Gradio Space (free ZeroGPU,
    ~12s/image, zero local RAM). On ANY failure for an image (GPU quota,
    network, server error) fall back to local mflux for that image, so a run
    always completes.
  - "mflux": local MLX generation only (Apple Silicon).

The local mflux model is lazy-loaded ONLY when a fallback is actually needed,
so the "space" path never pays the ~28GB unified-memory cost unless it has to.

mflux notes: model is z-image-turbo (Tongyi/Alibaba). First local use downloads
~15GB from Tongyi-MAI/Z-Image-Turbo. We invoke from FastAPI via
asyncio.to_thread so the event loop stays free.
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

        import mlx.core as mx
        from mflux.models.common.config import ModelConfig
        from mflux.models.z_image.variants.z_image import ZImage

        _model = ZImage(
            model_config=ModelConfig.z_image_turbo(),
            quantize=settings.mflux_quantize,
        )
        # MLX keeps an unbounded buffer cache by default; cap it (we also clear
        # between renders in _render_mflux) so a multi-image run stays near the
        # resident model size instead of ballooning the compressor.
        mx.set_cache_limit(settings.mflux_cache_limit_bytes)
        mx.reset_peak_memory()
        log.info("ZImage loaded and resident")

    return _model


def warmup() -> dict:
    _get_model()
    return {
        "loaded": True,
        "model": settings.mflux_model,
        "quantize": settings.mflux_quantize,
    }


def _clear_stale_images(output_dir: Path, word_id: int) -> int:
    """Remove pre-existing word_<id>_*.png before a re-run.

    A regenerated script can produce a different image count than a prior run
    (the per-beat schema makes this common), so old renders would otherwise
    linger on disk — confusing to debug and wasted space. Returns the count
    removed. Only touches this word's PNGs; other files are left alone.
    """
    removed = 0
    for p in output_dir.glob(f"word_{word_id:04d}_*.png"):
        p.unlink()
        removed += 1
    return removed


_space_client = None
_space_client_lock = threading.Lock()


def _get_space_client():
    """Lazily build and cache a gradio_client.Client for the Z-Image Space.

    Passing the HF token via `token=` routes ZeroGPU usage to the account's
    daily quota (free 5 min, PRO 40 min) instead of the anonymous per-IP pool
    (2 min). The client performs the queue/join handshake that the raw
    `/gradio_api/call` REST protocol does not — that handshake is what the
    quota attribution relies on, so a hand-rolled Bearer header on the REST
    endpoint never gets the account quota. Imported lazily so the module loads
    without gradio_client present (e.g. in unit tests).
    """
    global _space_client
    if _space_client is not None:
        return _space_client

    with _space_client_lock:
        if _space_client is None:
            from gradio_client import Client

            log.info("connecting gradio_client to %s (authenticated=%s)",
                     settings.zimage_space_url, bool(settings.hf_token))
            _space_client = Client(
                settings.zimage_space_url,
                token=settings.hf_token or None,
                verbose=False,
            )
    return _space_client


def _image_bytes_from_result(result) -> bytes:
    """Extract PNG bytes from a /generate_image predict result.

    The Space returns `(generated_image, seed)`. gradio_client downloads file
    outputs to a temp dir and usually hands back a local path string; depending
    on version it may instead return the raw FileData dict (path/url).
    """
    image = result[0] if isinstance(result, (list, tuple)) else result
    if isinstance(image, dict):
        local = image.get("path")
        if local and Path(local).exists():
            return Path(local).read_bytes()
        url = image.get("url")
        if url:
            import httpx

            resp = httpx.get(url, timeout=settings.zimage_space_timeout_s)
            resp.raise_for_status()
            return resp.content
        raise RuntimeError(f"space image dict has no path/url: {image!r}")
    if not image:
        raise RuntimeError(f"space returned no image: {result!r}")
    return Path(image).read_bytes()


def _generate_via_space(prompt: str, width: int, height: int, steps: int, seed: int) -> bytes:
    """Generate one image via the hosted Z-Image-Turbo Space using gradio_client.

    Returns PNG bytes. Raises on quota/network/server error so the caller can
    fall back to local mflux.
    """
    client = _get_space_client()
    # /generate_image arg order: prompt, height, width, num_inference_steps,
    # seed, randomize_seed.
    result = client.predict(
        prompt, height, width, steps, seed, False, api_name="/generate_image"
    )
    return _image_bytes_from_result(result)


def _render_mflux(model, prompt: str, seed: int, out_path: Path) -> None:
    """Render one image locally via mflux and save it, releasing GPU buffers."""
    import gc

    import mlx.core as mx
    from mflux.utils.image_util import ImageUtil

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
    del image
    gc.collect()
    mx.clear_cache()


def generate_images(script: Script, output_dir: Path, word_id: int) -> list[dict]:
    """Generate len(script.image_prompts) PNGs (4–7) into output_dir.

    Per-image: try the configured backend; if "space" fails, fall back to mflux.
    """
    n = len(script.image_prompts)
    if n < 1:
        raise ValueError("script has no image_prompts")
    output_dir.mkdir(parents=True, exist_ok=True)

    stale = _clear_stale_images(output_dir, word_id)
    if stale:
        log.info("cleared %d stale image(s) for word_id=%d", stale, word_id)

    backend = settings.image_backend
    width, height, steps = settings.mflux_width, settings.mflux_height, settings.mflux_steps
    mflux_model = None  # lazy — only loaded if a fallback is actually needed

    results: list[dict] = []
    backends_used: list[str] = []
    for i, prompt in enumerate(script.image_prompts):
        out_path = output_dir / f"word_{word_id:04d}_{i}.png"
        seed = settings.mflux_seed_base + i
        used: str | None = None

        if backend == "space":
            try:
                log.info(
                    "[%d/%d] space generate seed=%d %dx%d steps=%d → %s",
                    i + 1, n, seed, width, height, steps, out_path.name,
                )
                out_path.write_bytes(_generate_via_space(prompt, width, height, steps, seed))
                used = "space"
            except Exception as e:
                log.warning(
                    "[%d/%d] space backend failed (%s) — falling back to mflux",
                    i + 1, n, str(e)[:200],
                )

        if used is None:
            if mflux_model is None:
                mflux_model = _get_model()
            log.info(
                "[%d/%d] mflux generate seed=%d %dx%d steps=%d → %s",
                i + 1, n, seed, width, height, steps, out_path.name,
            )
            _render_mflux(mflux_model, prompt, seed, out_path)
            used = "mflux"

        backends_used.append(used)
        results.append({
            "image_idx": i,
            "image_path": str(out_path),
            "prompt": prompt,
            "seed": seed,
            "width": width,
            "height": height,
            "steps": steps,
            "size_bytes": out_path.stat().st_size,
        })

    log.info(
        "image gen done: %d images (space=%d mflux=%d)",
        n, backends_used.count("space"), backends_used.count("mflux"),
    )
    return results
