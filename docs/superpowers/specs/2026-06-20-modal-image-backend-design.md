# Modal Image Backend (Z-Image-Turbo on GPU) — Design Spec

**Date:** 2026-06-20
**Status:** Draft (design); pending user review → implementation plan
**Branch:** `feat/modal-image-backend`

## Problem

Image generation is the pipeline's only real bottleneck. The hosted Z-Image-Turbo
Gradio Space (free HuggingFace ZeroGPU) is the intended fast path, but its free
quota (~5 GPU-min/day ≈ 25 images) does not cover the ~28 images/day we generate
(7 images × 4 channels), and it is flaky. When the Space fails, every image falls
back to **local mflux** on stl, which takes **~8–10 min per image** — a 7-image
video can take over an hour, and the sustained MLX load is what crashed the
FastAPI service on 2026-06-19 (took out 4 consecutive scheduled runs).

We want a **fast, predictable, paid GPU path** that becomes the primary image
backend, while keeping the free Space and local mflux as fallbacks so a run always
completes.

## Goals

- A new image backend that runs **Z-Image-Turbo on a Modal GPU**, fast
  (~few sec/image after warmup) and reliable.
- Slot it into the existing per-image fallback chain as **Modal → Space → mflux**.
- Keep the **same model, resolution, steps, and seed** across all three tiers so
  output style is consistent regardless of which tier serves a given image.
- **Scale-to-zero** cost posture: pay only for actual generation, no idle GPU.
- **Auto-deploy** the Modal app via GitHub Actions on merge to `main`.
- A run **always completes** — any Modal failure falls through to Space, then mflux.

## Non-goals

- Moving TTS, scripts, or video assembly to Modal (image gen only — Kokoro TTS and
  local Ollama are already fast enough).
- Replacing or removing the Space or mflux backends (they remain as fallbacks).
- Keeping a Modal container warm between runs (scale-to-zero accepted; one cold
  start per batch, amortized over 7 images, is negligible).
- A new image model or aesthetic (Z-Image-Turbo stays).

## Decisions (from brainstorming)

1. **Scope:** image gen only.
2. **Chain placement:** `image_backend="modal"` → ordered chain **Modal → Space → mflux**.
   Modal is the fast primary; the free Space is the secondary backstop; local mflux
   is the last resort.
3. **Model:** Z-Image-Turbo (same as Space + mflux). The existing
   `mrfakename-z-image-turbo` Space proves a torch/CUDA serving path exists; we
   mirror that inference rather than invent one.
4. **Interface:** an **HTTPS web endpoint** hosted by Modal (`@modal.asgi_app` /
   FastAPI), called from stl with `httpx`, mirroring the existing `_generate_via_space`
   shape (returns PNG bytes). Loose coupling — stl needs no `modal` client at runtime.
5. **Cost:** scale-to-zero. Weights cached in a `modal.Volume` so cold starts skip
   the ~15GB re-download. Default GPU **L4** (24GB; Z-Image-Turbo ~6B fits in fp16;
   cheapest CUDA tier), exposed as a constant so it is swappable.
6. **Deployment:** the Modal app lives in this repo under `modal/` and is
   **auto-deployed by GitHub Actions** on push to `main` touching `modal/**`.

## Architecture

### Two deployables

**A. Modal app — `modal/zimage_app.py` (in this repo)**

- A `modal.App` that builds a CUDA image with torch + Z-Image-Turbo deps.
- A class with `@modal.enter()` loading the model **once** per container onto the
  GPU; weights cached in a `modal.Volume` (skip re-download on cold start).
- An `@modal.asgi_app()` FastAPI exposing `POST /generate`.
- `gpu="L4"` (constant), scale-to-zero (no `min_containers`).
- Auth via a `modal.Secret` holding a shared bearer token; the endpoint returns
  401 on missing/bad token.
- Deployed with `modal deploy -m modal.zimage_app`. Endpoint URL is **stable**
  across redeploys (per app/function), so stl's config never needs updating after
  the first deploy.

**B. stl client — `_generate_via_modal()` in `api/services/image.py`**

- An `httpx` POST to the Modal endpoint with a bearer header; returns PNG bytes.
- Mirrors `_generate_via_space` structure, with a small retry wrapper
  (`_generate_via_modal_with_retries`) analogous to the Space retry helper.

### Request contract

```
POST {MODAL_IMAGE_URL}/generate
Authorization: Bearer <MODAL_IMAGE_TOKEN>
Content-Type: application/json
Body: {"prompt": str, "width": 768, "height": 1344, "steps": 8, "seed": 42}

200 → image/png (raw bytes)
401 → bad/missing token   (stl raises → falls to Space)
5xx / timeout → stl raises → falls to Space
```

### Backend chain refactor (`generate_images`)

Today `generate_images()` hardcodes "try space, else mflux" per image. Refactor to
build an **ordered list of tier callables** from `image_backend`, then per image try
each tier in order until one succeeds:

- `image_backend="modal"` → `[modal, space, mflux]`
- `image_backend="space"` → `[space, mflux]`  (unchanged behavior)
- `image_backend="mflux"` → `[mflux]`         (unchanged behavior)

Each non-final tier's failure is logged and falls through to the next; mflux is
always the terminal tier (guaranteed completion). The existing Space retry/quota
logic and mflux lazy-load are preserved unchanged. `backends_used` accounting and
the per-image result dicts are unchanged except they may now record `"modal"`.

## Config additions (`api/config.py`)

- `image_backend`: `Literal["space", "mflux"]` → `Literal["modal", "space", "mflux"]`
  (default stays `"space"` until we cut over in deployment).
- `modal_image_url: str | None = None` (from env `MODAL_IMAGE_URL`)
- `modal_image_token: str | None = None` (from env `MODAL_IMAGE_TOKEN`)
- `modal_attempts: int = 2`
- `modal_retry_sleep_s: float = 5.0`
- `modal_timeout_s: float = 120.0` (covers cold start + generation)

Secrets (`MODAL_IMAGE_URL`, `MODAL_IMAGE_TOKEN`) live in stl's `api/.env` only,
never committed. The same bearer token is stored as a `modal.Secret` on the Modal
side so both ends agree.

## Auto-deploy (`.github/workflows/deploy-modal.yml`)

```yaml
name: Deploy Modal app
on:
  push:
    branches: [main]
    paths: ['modal/**']            # only redeploy when the Modal app changes
jobs:
  deploy:
    runs-on: ubuntu-latest
    env:
      MODAL_TOKEN_ID: ${{ secrets.MODAL_TOKEN_ID }}
      MODAL_TOKEN_SECRET: ${{ secrets.MODAL_TOKEN_SECRET }}
    steps:
      - uses: actions/checkout@v6
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install modal
      - run: modal deploy -m modal.zimage_app
```

GitHub repo secrets `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` are **already set**
(2026-06-20). The shared bearer token is created once as a `modal.Secret` (manually
or via CLI) — it is independent of the Modal account token and is not in the repo.

## Error handling

Every Modal failure mode — cold-start failure, timeout, 5xx, 401, network — is
caught, logged at WARNING, and falls through to the Space chain exactly as Space
failures fall to mflux today. No new path can stall or crash the pipeline. The
heavy local mflux fallback (the original crash trigger) is now reached far less
often, reducing the sustained-load risk on stl.

## Testing

- **Unit (`api/test_image.py`)**, with `httpx` monkeypatched (no real GPU/network):
  - `_generate_via_modal` posts the correct URL, bearer header, and JSON payload,
    and returns response bytes.
  - `_generate_via_modal_with_retries` retries then raises on persistent failure.
  - The tier chain falls **Modal → Space → mflux** correctly given injected
    failures, and records the right `backends_used`.
  - `image_backend="space"` and `"mflux"` behavior is unchanged (regression guard).
- **Modal app:** a thin import/smoke check of the generation function signature;
  full GPU generation is validated manually via `curl` against the deployed endpoint
  (documented in AGENTS.md), not in CI (no GPU in CI).
- Full suite stays green (currently 68 tests).

## Deployment / cutover plan

1. Merge code (default `image_backend` still `"space"`) → GitHub Actions deploys the
   Modal app; capture the stable endpoint URL.
2. Create the `modal.Secret` bearer token; add `MODAL_IMAGE_URL` + `MODAL_IMAGE_TOKEN`
   to stl `api/.env`.
3. Smoke-test the endpoint with `curl`; confirm a PNG comes back.
4. Flip stl `image_backend=modal`; restart the launchd-managed uvicorn service
   (`launchctl kickstart -k gui/501/com.n8n-shorts.api`).
5. Trigger one pipeline run; confirm logs show `modal` as the serving tier and the
   per-image latency drop.

## Documentation updates

- **AGENTS.md:** new subsection documenting the Modal backend, the 3-tier chain, the
  config knobs, the secret/token setup, the auto-deploy workflow, and the `curl`
  smoke test.
- **README.md:** image-gen row updated to mention the Modal backend.

## Prerequisites (status)

- Modal account + CLI authed in the working session — **done**.
- GitHub repo secrets `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` — **done**.
- `modal.Secret` for the endpoint bearer token — **to do at implementation/deploy time**.
