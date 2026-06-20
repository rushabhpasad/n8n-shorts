# modal_app — Z-Image-Turbo GPU image backend

The serverless GPU image backend for n8n-shorts. It runs **Z-Image-Turbo** on a
[Modal](https://modal.com) L4 GPU and exposes an authenticated HTTPS endpoint
that the FastAPI service (`api/services/image.py`) calls as its **primary image
tier** in production.

When `IMAGE_BACKEND=modal`, image generation is a per-image fallback chain:
**Modal → Space → mflux**. Modal is the fast paid GPU; the free HF Space is the
backstop; local mflux is the last resort. Any Modal failure transparently falls
through, so a run always completes.

> **Directory name:** this is `modal_app/`, **not** `modal/`. A top-level
> `modal/` package would shadow the `modal` pip SDK and break `import modal` /
> `modal deploy`. Never rename it.

## Status

- **Live in production since 2026-06-20.**
- Workspace: `rpasad23-ai` · App: `n8n-shorts-zimage`
- Endpoint: `https://rpasad23-ai--n8n-shorts-zimage-zimage-web.modal.run`
- Real-world: ~15 s/image, ~$0.06/run, ~$0.25/day across the 4-channel batch.

## How it works

`zimage_app.py` defines:

- **An image** (`modal.Image.debian_slim`) that `apt_install("git")`, pip-installs
  torch + `git+https://github.com/huggingface/diffusers` (the only place
  `ZImagePipeline` lives) + fastapi, then bakes the ~15 GB Z-Image-Turbo weights
  into the image layer via `run_function(snapshot_download)` — so cold starts
  never re-download the checkpoint.
- **A GPU class** (`@app.cls(gpu="L4", scaledown_window=60)`) that loads the
  pipeline once per container in `@modal.enter()` (`bfloat16`, `.to("cuda")`),
  scale-to-zero between batches.
- **A web endpoint** (`@modal.asgi_app()`) — a FastAPI app exposing:

```
POST /generate
Authorization: Bearer <ZIMAGE_TOKEN>
Content-Type: application/json
{ "prompt": str, "width": 768, "height": 1344, "steps": 8, "seed": 42 }

200 → image/png (raw bytes)
401 → bad/missing bearer token
```

Generation uses `guidance_scale=0.0` (required for the Turbo distilled model) and
`torch.Generator("cuda").manual_seed(seed)`. The request schema matches what
`api/services/image.py::_generate_via_modal` sends exactly.

## Deploy

Auto-deploy is wired: `.github/workflows/deploy-modal.yml` runs
`modal deploy -m modal_app.zimage_app` on every push to `main` that touches
`modal_app/**`. It needs GitHub repo secrets `MODAL_TOKEN_ID` /
`MODAL_TOKEN_SECRET` (already set).

Manual deploy (from a machine with the Modal CLI authed via `modal setup`):

```bash
modal deploy -m modal_app.zimage_app
```

First deploy takes a few minutes (CUDA image build + 15 GB weight bake);
subsequent deploys are ~4 s because the image layer is cached.

## Secret (endpoint auth)

The bearer token is a Modal Secret named `zimage-token` with key `ZIMAGE_TOKEN`.
The same value must be set as `MODAL_IMAGE_TOKEN` on the stl host (`api/.env`).

```bash
modal secret create zimage-token ZIMAGE_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

> **GOTCHA — redeploy after changing the secret.** Modal binds
> `Secret.from_name("zimage-token")` into the deployed app **at deploy time**. If
> you recreate/update the secret afterward, running containers fail every request
> with `"The secret does not exist, or you do not have access to it"` until you
> **redeploy** (`modal deploy -m modal_app.zimage_app`, ~4 s). Always redeploy
> after rotating the token.

## Wire the stl host

In `stl:~/n8n-shorts/api/.env`:

```bash
IMAGE_BACKEND=modal
MODAL_IMAGE_URL=https://rpasad23-ai--n8n-shorts-zimage-zimage-web.modal.run
MODAL_IMAGE_TOKEN=<same value as the zimage-token secret>
```

Then restart the launchd-managed service:

```bash
ssh stl 'launchctl kickstart -k gui/501/com.n8n-shorts.api'
```

To revert to the free Space backend: set `IMAGE_BACKEND=space` and restart.

## Smoke test

```bash
curl -X POST "$MODAL_IMAGE_URL/generate" \
  -H "Authorization: Bearer $MODAL_IMAGE_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"prompt":"a red fox in snow, cinematic","width":768,"height":1344,"steps":8,"seed":42}' \
  -o /tmp/modal_test.png && file /tmp/modal_test.png
# → PNG image data, 768 x 1344   (first call pays cold start ~20–30 s)
```

## Operations

```bash
modal app list                      # confirm n8n-shorts-zimage = deployed
modal app logs n8n-shorts-zimage    # tail container logs (auth/build errors show here)
```

The config knobs on the client side (`modal_attempts`, `modal_retry_sleep_s`,
`modal_timeout_s`, etc.) live in `api/config.py`; see AGENTS.md §2.15.
