# AGENTS.md — operating conventions for this codebase

Read this before changing anything. The pipeline is small but every step has
landmines we've stepped on already; this doc captures them so you don't have
to relearn them.

If `AGENTS.md` and `README.md` disagree, **`AGENTS.md` wins** for
implementation conventions. The README is for setup/usage; this file is for
people (or agents) editing code.

## 1. The standard restart cycle

**Production host (`stl`) is supervised by `launchd`** (LaunchAgent
`com.n8n-shorts.api`, `RunAtLoad` + `KeepAlive` → starts on boot, auto-restarts
on crash). Restart it — never `pkill`/`nohup` on stl, that fights launchd for
port 7860:

```bash
ssh stl 'launchctl kickstart -k gui/501/com.n8n-shorts.api'
ssh stl 'curl -sS --max-time 3 http://localhost:7860/health | jq .status'
```

Local dev (not launchd-managed):

```bash
pkill -9 -f "uvicorn main:app" 2>/dev/null; sleep 2
cd api && nohup uv run uvicorn main:app --host 0.0.0.0 --port 7860 \
  > /tmp/shorts-api.log 2>&1 &
sleep 6 && curl -sS --max-time 3 http://localhost:7860/health | jq .status
```

The Homebrew PATH matters: `uv` and `ffmpeg` live in `/opt/homebrew/bin`, which
is **not** on a non-interactive ssh PATH — the launchd plist bakes it in; a
manual relaunch must `export PATH="/opt/homebrew/bin:$PATH"` or ffmpeg/`uv`
won't resolve.

**With the production `modal` backend (or the `space` backend), the
Z-Image-Turbo model is NOT loaded locally**, so restarts have no model-reload
cost. The ~30 s reload note applies only when the **mflux** fallback has fired
during the session (the model is lazy-loaded on first fallback and stays
resident until the process exits). Avoid restarting in the middle of an
in-flight image-gen run regardless of backend — the in-progress request is lost.

If you're iterating on `services/video.py` only, `/assemble` doesn't need the
model loaded; the restart cost is negligible.

## 2. Landmines (in rough order of how often they bite)

### 2.1 `mflux` `ImageUtil.save_image` auto-renames if the file exists — FIXED

If `word_0002_0.png` already exists from a previous run, `save_image` would
save the new image as `word_0002_0_1.png` instead of overwriting. `/assemble`
would then pick up the *old* `word_0002_0.png` and produce a Frankenstein video
(new audio, old images).

**Fixed:** `services/image.py` now has `_clear_stale_images(output_dir, word_id)`
which deletes all `word_<id>_*.png` files at the start of every `generate_images`
call — before any render begins. This also handles the case where a regenerated
script produces a different image count than a prior run (old renders no longer
linger on disk).

### 2.2 Homebrew `ffmpeg` lacks libfreetype → no `drawtext`

All text in videos must be rendered via Pillow → PNG, then composited with
ffmpeg's `overlay` filter. Don't write `drawtext=...` filters — they will
silently never render.

Pattern in `services/video.py`:
1. Render PIL `Image.new("RGBA", ...)` with text via `ImageDraw.text(...)`.
2. Save to tempdir.
3. Pass as an additional input to ffmpeg.
4. `[main][text]overlay=x=...:y=...:enable='between(t,s,e)'` chain.

### 2.3 `zoompan` with `-loop 1 -t` produces too many frames

If you write `-loop 1 -t {dur} -i image.png` *and* `zoompan=d={dur*fps}`, you
get `dur * 25 * dur * fps` frames out of zoompan and `-shortest` clips to
audio length — you'll see only beat 0 the whole video.

**Correct pattern (in `kb_filter()`):**
```
-loop 1 -framerate FPS -t {dur} -i image.png
... zoompan=...:d=1:s={W}x{H}:fps={FPS}
```
`d=1` = one zoompan output per input frame.

### 2.4 Piper speaks markdown and emojis literally

Piper says `*nostos*` as "asterisk nostos asterisk" and 👋 as "wave hand
emoji". `services/voice.py` runs `_normalize_for_tts(...)` over every beat
narration, which strips paired emphasis (`*…*`, `_…_`) and codepoints in the
common emoji ranges before sending to Piper. Same normalisation is applied
in `services/video.py` for captions. If you add a new text path that goes to
Piper or to the caption renderer, run it through the same normaliser.

### 2.5 Pillow `font.getbbox()` returns floats

Cast `int(...)` before passing to `Image.new(size=(w, h))` or any
position arithmetic. Pyright catches this; runtime gives a confusing error.

### 2.6 httpx 1.0.dev drops `AsyncClient` from the top level

We pin `httpx>=0.28,<1.0` in `api/pyproject.toml`. Do not relax this
constraint until upstream 1.0 stabilises *and* `from httpx import AsyncClient`
works again.

### 2.7 n8n HTTP node hides the Body UI without `sendBody: true`

When generating or editing the per-channel `n8n/workflows/<slug>.json`, every
POST node must include `"sendBody": true` *and* `"contentType": "json"` *and*
`"specifyBody": "json"` *and* `"jsonBody": "..."`. Missing `sendBody` collapses
the body section in the UI and n8n sends nothing.

### 2.8 n8n IF nodes are strict about value types in v2

`leftValue` is a number → `operator.type` must be `"number"`. Mismatched
types throw "Wrong type: '1' is a number but was expecting a string".

For null-safe checks (e.g. when `/state/next` may return null), use optional
chaining in the leftValue expression: `={{ $('Get next word').item.json?.id }}`
+ `operator: {type: "number", operation: "exists"}`.

### 2.9 Black Forest Labs models are gated on HuggingFace

`schnell`, `dev`, `flux2-klein-*`, `krea-dev` — all return 401 GatedRepoError
unless you've accepted licence terms with an authed `HF_TOKEN`. We dodged
this by switching to `z-image-turbo` (Tongyi-MAI, fully open). If you need
to switch back, set `HF_TOKEN` env var, accept the licence on the HF page in
a browser first.

`HF_TOKEN` is **also** the primary auth token for the hosted Z-Image-Turbo
Gradio Space (`image_backend = "space"`). ZeroGPU quota is **daily, per
account**: anonymous ~2 min/day, free account ~5 min/day, HF Pro 40 min/day
(`https://huggingface.co/docs/hub/spaces-zerogpu`). Actual GPU time is
~10–15 s/image, so a free account's 5 min comfortably covers a daily
4-channel run (~20–25 images); the mflux fallback absorbs the tail if the
quota runs low. Set the token in `api/.env` (gitignored).

**Quota attribution requires the official `gradio_client`, not a raw header.**
We call the Space through `gradio_client.Client(url, token=HF_TOKEN)`
(`_get_space_client()` in `services/image.py`), which performs the
`queue/join` handshake ZeroGPU uses to bill the account. A hand-rolled
`Authorization: Bearer` header on the raw `/gradio_api/call/...` REST endpoint
does **not** get the account quota — it returns an opaque `error: null` once
the shared per-IP pool is spent. Note the constructor kwarg is `token=` in
`gradio_client` 2.x (older docs say `hf_token=`).

### 2.10 Piper voice licences matter for monetised YouTube

`settings.piper_voices` is a **list** of voices the `/voice` endpoint
shuffles uniformly at random per call. All must be commercial-use-OK:

- `en_US-john-medium`   — LibriVox public domain
- `en_US-bryce-medium`  — public domain (own recording)
- `en_US-joe-medium`    — CC0 (OHF-Voice)

**Forbidden**: ryan-* (CC BY-NC-SA via RyanSpeech), hfc_*, kathleen-low,
danny-low — the NC clause kills YouTube monetisation. When adding a voice,
check `https://huggingface.co/rhasspy/piper-voices/raw/main/<path>/MODEL_CARD`
for the dataset licence. Fine-tuning lineage matters: kathleen-low and
danny-low are CC0 datasets but fine-tuned from ryan-low, so they inherit the
NC restriction in practice.

The `/voice` endpoint writes a sidecar at `output/audio/word_XXXX.meta.json`
with the voice that was picked, so `/upload` can persist it into the runs
audit table.

### 2.11 Partial voice downloads leave broken `.onnx` files

If a Piper voice download is interrupted mid-stream (HF timeout, network
blip), the resulting `.onnx` is a truncated file that's "non-zero size" but
unloadable. Our previous existence check (`if path.exists() and size > 0`)
treated this as cached and skipped re-download.

**Fixed pattern (in `ensure_voice_downloaded`):** stream to a `.part` file,
`rename` atomically on completion, `unlink` on any exception. Any future
download function in the project should use the same pattern.

### 2.12 9B+ image models are unworkable on Apple Silicon

Qwen-Image and FLUX.2-klein-9B benchmarked at ~40 s/step × 25 steps × 5
images ≈ 50 min/video on an M1 Max. Z-Image-Turbo on the **Modal** GPU backend
(production) runs ~15 s/image; the hosted **Space** backend ~10–15 s/image
(ZeroGPU, zero local RAM). The **mflux** local fallback is far slower:
~5 min/image on an M1 Max (the z-image-turbo transformer is ~23 GB; a single
768×1344 render needs ~28 GB unified memory and thrashes the macOS compressor on
a 32 GB machine) — and a sustained 7-image mflux run is what crashed the service
on 2026-06-19, which is the reason Modal exists. Never run mflux as the primary
backend in production; it is a last-resort fallback only.

### 2.13 Image backend: three tiers (Modal → Space → mflux)

`api/config.py` `image_backend` selects the per-image tier chain that
`services/image.py::_image_backend_chain` walks. **Production (`stl`) runs
`"modal"`**; the code default is `"space"` so a fresh checkout works without a
Modal account.

- **`"modal"` (production)** — chain **Modal → Space → mflux**. Fast paid GPU
  first (see §2.15), free Space as backstop, local mflux as last resort.
- **`"space"` (code default)** — chain **Space → mflux**. Calls the hosted
  Z-Image-Turbo Gradio Space (`zimage_space_url`, default
  `https://mrfakename-z-image-turbo.hf.space`) via
  `gradio_client.Client(url, token=HF_TOKEN)`. Free ZeroGPU; ~10–15 s/image.
- **`"mflux"`** — local MLX only. Offline/deterministic; slow (~5 min/image).

In every chain **mflux is the terminal tier** — its exception propagates, so a
run never fails for lack of a backend; non-terminal tiers log and fall through.
The mflux model is **lazy-loaded only when that tier is actually reached**
(`_get_model()`), so the modal/space paths never pay the ~28 GB unified-memory
cost unless they have to.

Relevant config knobs (all in `api/config.py`, env-overridable):

| Setting | Default | Notes |
|---|---|---|
| `image_backend` | `"space"` | `"modal"`, `"space"`, or `"mflux"`. stl `.env` sets `IMAGE_BACKEND=modal`. |
| `zimage_space_url` | `https://mrfakename-z-image-turbo.hf.space` | Gradio Space endpoint (passed to `gradio_client.Client`) |
| `zimage_space_timeout_s` | `180` | Timeout for the URL-fallback fetch when the Space returns a remote image dict |
| `hf_token` | `None` | Set via `HF_TOKEN` in `api/.env`; passed as `token=` to `gradio_client` so ZeroGPU usage bills the account (5 min/day free) |
| `mflux_cache_limit_bytes` | `1 GiB` | Caps MLX GPU-buffer cache; `mx.clear_cache()` called between mflux renders |

Modal-specific knobs are in §2.15.

### 2.14 Voice backend: local Piper (default) vs. Kokoro container

`api/config.py` `voice_backend` controls which path `services/voice.py` `synthesize()` takes (same Space→fallback shape as image gen):

- **`"piper"` (default)** — local Piper TTS, fully license-traceable (see §2.10),
  monetization-safe. This is the production default; don't change it lightly.
- **`"kokoro"`** — POSTs to the Kokoro-FastAPI container's OpenAI-compatible
  `/v1/audio/speech` (`kokoro_base_url`, default `http://localhost:8880`). Higher
  perceived quality, but Kokoro's training data includes "synthetic audio from
  closed TTS providers" — an **unaudited provenance risk** Piper doesn't carry, so
  it's behind a flag, not the default. On **ANY** failure (container down, HTTP,
  empty body) it falls back to local Piper for that clip, so a run always completes.

The container is defined in the repo-root `docker-compose.yml`
(`ghcr.io/remsky/kokoro-fastapi-cpu`). On macOS it is **CPU-only** — Docker has no
Metal/MLX passthrough — which is fine because image gen dominates wall-clock, not
TTS. Bring it up with `docker compose up -d kokoro-tts` on stl before flipping the
flag. The `.meta.json` sidecar records `backend` (`piper`/`kokoro`) alongside `voice`.

| Setting | Default | Notes |
|---|---|---|
| `voice_backend` | `"piper"` | `"piper"` or `"kokoro"` |
| `kokoro_base_url` | `http://localhost:8880` | Kokoro-FastAPI OpenAI-compatible base URL |
| `kokoro_voices` | `["af_heart", "af_bella"]` | Random pick per call; Kokoro's only A/A- English voices |
| `kokoro_model` | `"kokoro"` | `model` field in the speech request |
| `kokoro_speed` | `1.0` | `<1` slower; Kokoro's natural pacing needs no slowdown (Piper still uses `1.1` length_scale) |
| `kokoro_timeout_s` | `120` | HTTP timeout for the synth call |

### 2.15 Modal image backend (paid GPU, production primary)

**LIVE in production since 2026-06-20.** `image_backend="modal"` makes image gen a
3-tier chain **Modal → Space → mflux** (`api/services/image.py::_image_backend_chain`).
Real-world: ~15 s/image, ~$0.06/run, 4-channel daily batch completes in ~5 min.
Full architecture/deploy/cost details live in `modal_app/README.md`.

- **Modal app:** `modal_app/zimage_app.py` — Z-Image-Turbo on an L4 GPU, exposed as
  `POST /generate` (bearer-authed) returning PNG bytes. Scale-to-zero; weights baked
  into the image (no runtime re-download). Deploy: `modal deploy -m modal_app.zimage_app`.
  **The directory is `modal_app/`, not `modal/`, to avoid shadowing the `modal` SDK.**
- **Deployed endpoint:** `https://<workspace>--n8n-shorts-zimage-zimage-web.modal.run`
  (the `modal deploy` output prints the real URL; stable across redeploys). The
  concrete value is the production host's `MODAL_IMAGE_URL` — kept in its `.env`,
  not committed (this is a public repo).
- **Auto-deploy:** `.github/workflows/deploy-modal.yml` runs `modal deploy` on push to
  `main` touching `modal_app/**`. Needs repo secrets `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET`
  (set). Pushing the workflow file itself needs an SSH git remote — the `gh` OAuth token
  lacks `workflow` scope (origin is SSH for this repo).
- **Endpoint auth:** Modal Secret `zimage-token` (key `ZIMAGE_TOKEN`) must match stl's
  `MODAL_IMAGE_TOKEN`. stl also needs `MODAL_IMAGE_URL`.
- **Smoke test:**
  `curl -X POST "$MODAL_IMAGE_URL/generate" -H "Authorization: Bearer $MODAL_IMAGE_TOKEN" -H 'content-type: application/json' -d '{"prompt":"a red fox","width":768,"height":1344,"steps":8,"seed":42}' -o /tmp/modal_test.png`

**GOTCHA — redeploy after changing the secret.** Modal binds
`Secret.from_name("zimage-token")` into the deployed app **at deploy time**. If you
update/recreate the secret afterward, running containers throw `"The secret does not
exist, or you do not have access to it"` on every start until you **redeploy**
(`modal deploy -m modal_app.zimage_app`, ~4 s since the image is cached). Always
redeploy after rotating the token.

**GOTCHA — `apt_install("git")` is required.** The image installs diffusers from its
git URL (`git+https://github.com/huggingface/diffusers`, the only place
`ZImagePipeline` lives), so `modal.Image.debian_slim` needs git apt-installed before
the pip step or the build fails with "Cannot find command 'git'".

| Setting | Default | Notes |
|---|---|---|
| `image_backend` | `"space"` | `"modal"`, `"space"`, or `"mflux"`; `"modal"` → Modal→Space→mflux. stl `.env` sets `modal`. |
| `modal_image_url` | `None` | Deployed Modal endpoint base URL (env `MODAL_IMAGE_URL`) |
| `modal_image_token` | `None` | Shared bearer token (env `MODAL_IMAGE_TOKEN`), matches the `zimage-token` Modal Secret |
| `modal_attempts` | `2` | total tries before falling to Space |
| `modal_retry_sleep_s` | `5.0` | sleep between Modal retries |
| `modal_timeout_s` | `120` | HTTP timeout (covers cold start) |

## 3. Files where the most damage happens

These are the high-impact files. Read carefully, change with intent.

| File | What it controls | Test after editing |
|---|---|---|
| `channels/<slug>/prompts/script.md` | Output quality of the LLM for that channel. Image prompts, narration tone, sentence-level captions. Each channel has its own. | Regen one script (`curl -X POST /<slug>/script` with a `word_id`) and *read it* before any image gen. |
| `channels/<slug>/channel.json` | Per-channel metadata — slug, display name, YouTube handle, default categories, and YouTube upload defaults (`youtube_category_id`, `youtube_default_language`, `youtube_default_audio_language`, `youtube_license`, `ai_disclosure`). Loaded at runtime by `api/channels.py`. | `curl /channels` to confirm the registry sees it. |
| `api/services/analytics.py` | YouTube analytics — `channel_snapshot` (Data API v3), `period_metrics` (Analytics API v2: new/lost subs, watch time, likes, comments, shares, avg-view-% over N days), `per_video` (Data API v3 lifetime stats), `traffic_sources` (Shorts-feed share), `top_video`, `ypp_progress`, `compute_milestones`, `detect_alerts`, `channel_analytics` (orchestrates + reads/writes `analytics_snapshots` for trends), `build_daily_report`, `render_digest` (the Telegram/Slack message body). | `uv run --project api pytest api/test_analytics.py api/test_analytics_extras.py -v` (all mocked — no network). |
| `api/services/youtube.py` | YouTube upload via Data API v3. Sets `categoryId`, `defaultLanguage`, `defaultAudioLanguage`, `containsSyntheticMedia`, `license`, `embeddable`, `publicStatsViewable`, `madeForKids`. Defaults flow from `ChannelConfig`; `UploadRequest` can override per-call. | Upload one video with `privacy: "private"`, then in Studio confirm category, language, and "Altered content" disclosure are set. |
| `api/channels.py` | Channel registry (file-based). `load(slug)` resolves a `ChannelConfig`; `list_slugs()` discovers all channels at runtime. | Lookup an unknown slug — should 404 with a clear message. |
| `api/models.py` (`Script`) | Schema the LLM must satisfy. Each `Beat` carries `images: list[str]` (1–4 diffusion prompts). `Script.image_prompts` is a computed property that flattens every beat's `images` in order — the rest of the pipeline reads this property, so generated images and shown images are always 1:1 by construction. A `model_validator` enforces the total image count is 4–7 (`MIN_IMAGES_TOTAL=4`, `MAX_IMAGES_TOTAL=7`). No index pool, no permutation rule; orphan/unused prompts are structurally impossible. | Round-trip a JSON through `Script.model_validate(...)`. |
| `api/services/video.py` | ffmpeg filter graph. Easy to break the whole video silently — `-shortest` masks bugs. | Always extract frames at `t=1s`, `t=mid-beat`, `t=outro_start+0.5s` after a change. |
| `api/config.py` | All runtime knobs. Pydantic-settings; env-overridable. Helpers `channel_data_dir()`, `youtube_oauth_path(channel)`, `youtube_token_path(channel)` resolve channel-scoped paths. | Health check (`/health`) shows current values + the channels list. |
| `n8n/generate.py` | Per-channel workflow generator. Reads every `channels/<slug>/channel.json` and writes `n8n/workflows/<slug>.json`. | Run, then re-import each workflow into n8n. |
| `channels/<slug>/brand.json` | Channel icon prompts. Edit the `brand_concept` / `color_palette` / `icon_prompts` to redesign the channel's visual mark. | `uv run --project api scripts/gen_brand.py --channel <slug> --only <prompt-name>` and view the PNG. |
| `scripts/gen_brand.py` | Renders icon candidates from a channel's `brand.json`. Already calls `unlink()` before `save_image` so the §2.1 mflux landmine is dodged here. | Run with `--only` for a single candidate. |
| `scripts/merge_candidates.py` | Merges web-verified candidate JSON files (`{"candidates": [...]}`) into a channel's `words.csv`: dedupes case-insensitively on the subject column against the existing queue and within the batch, continues ids from the current max, optionally trims to `--target` (lowest `priority` first), and appends with CSV quoting. | `merge_candidates.py --channel <slug> --workdir <dir> [--target N]` for a dry run; add `--apply` to write. |

## 4. Conventions

- **Python**: 3.12+, pydantic v2, FastAPI ≥0.115. uv-managed venv in `api/.venv/`.
- **Typing**: `from __future__ import annotations` at top of every file. Strict
  typing on Pydantic models; defer for stdlib paths and one-liners.
- **No comments unless WHY is non-obvious.** Code self-documents via names.
- **Module size**: keep files under 500 lines; hard cap 1000.
- **Separation of concerns**: `services/` has all I/O + external calls. `main.py`
  is route handlers + light orchestration only. `db.py` is the only place that
  touches SQLite directly.
- **File naming for outputs**: `word_{id:04d}_{i}.png` for images, `word_{id:04d}.{wav|mp4|json}` for the rest. Don't change without updating `/assemble` and `/upload`'s path expectations. Brand icons: `assets/brand/<channel>/icon_<name>.png` where `<name>` matches `channels/<channel>/brand.json` `icon_prompts[].name`.
- **Logging**: use the existing `log = logging.getLogger("shorts-api.<service>")` pattern. Don't introduce structured loggers unless you also wire log aggregation.

## 5. How to test changes

There is a small unit-test suite covering the core schema and image-cleanup
logic: `api/test_models.py` (Script schema validation) and `api/test_image.py`
(`_clear_stale_images`). Run them via:

```bash
api/.venv/bin/python -m pytest api/test_models.py api/test_image.py
```

Integration testing remains empirical — the pipeline is too multi-process for
unit tests to be high-leverage end-to-end:

1. **`/script` change**: regen one word, paste the JSON, eyeball it.
2. **`/image` change**: regen one word's images (~1–2 min via the Modal/Space backend), open the PNGs.
3. **`/voice` change**: regen one WAV, play in QuickTime, listen for emoji/markdown bleed-through.
4. **`/assemble` change**: regen one video, extract frames at every beat boundary + outro start, inspect captions and image transitions.
5. **`/upload` change**: trigger with `privacy: "private"`, check it lands in Studio.
6. **n8n change**: Execute Workflow once end-to-end; check the audit trail in `runs` table.

## 6. Analytics endpoints

Added in `api/main.py` alongside the per-channel upload endpoints:

| Endpoint | Caller | Description |
|---|---|---|
| `GET /analytics/daily?days=30` | n8n Sheets append | All-channel structured report. `channels[]` carries: `snapshot{subscribers,total_views,video_count}`, `new_subs_1d`, `period{days,subscribers_gained,subscribers_lost,new_subscribers,estimated_minutes_watched,views,likes,comments,average_view_duration_s,shares,average_view_percentage}`, `videos_uploaded`, `avg_likes_per_video`, `avg_comments_per_video`, `videos[]`, plus the enriched signals `traffic_sources{}`, `shorts_feed_share`, `top_countries[{country,views}]`, `top_video{video_id,url,views,likes,comments}`, `ypp{subscribers,subs_target,subs_progress,shorts_views_90d,shorts_views_target,shorts_views_progress,eligible}`, `views_90d`, `queue_pending`, `uploads_24h`, `trend{compared_days,subscribers,total_views,period_views}`, `milestones[]`, `alerts[]`, and `digest_text` (the rendered Telegram/Slack body — `analytics.render_digest`, unit-tested). The workflow makes this single call: `channels[]` → Sheets, `digest_text` → chat. One failing channel is isolated into `errors`; the rest still return. |
| `GET /analytics/daily/digest?days=30` | Ad-hoc preview only | Returns just the digest body as `{text}`. Not used by the workflow. |
| `GET /{channel}/analytics?days=30` | Ad-hoc / debugging | Single-channel variant of the structured report. |
| `GET /analytics/videos` | n8n per-video branch | All-channel per-video daily stats. Returns one record per video per channel with `*_total` (cumulative) and `*_today` (day-over-day delta) columns for views, likes, comments, watch time, and shares. |
| `GET /{channel}/analytics/videos` | Ad-hoc / debugging | Single-channel per-video stats — same shape as above. |

Data sources: YouTube Data API v3 (channel snapshot + per-video lifetime stats)
and YouTube Analytics API v2 (time-ranged: new subs, watch time, period
likes/comments/shares/avg-view-%, plus `insightTrafficSourceType` for the
Shorts-feed share, `country` for top geography, and a 90-day window for YPP
progress). Uploaded video IDs
come from the `runs` table (`db.uploaded_video_ids`). Trends, milestones, and
anomaly alerts are diffed against the previous run's totals stored in the
`analytics_snapshots` table (one row per channel per day) — **no extra API
calls** — and `queue_pending` / `uploads_24h` read straight from `words`/`runs`.

### OAuth scopes (widened — re-consent required)

`api/services/youtube.py` and `scripts/yt_init.py` now request three scopes:

- `https://www.googleapis.com/auth/youtube.upload`
- `https://www.googleapis.com/auth/yt-analytics.readonly`
- `https://www.googleapis.com/auth/youtube.readonly`

**Each channel must be re-consented once** — existing `youtube_token.<slug>.json`
files lack the analytics scopes and will return 403 on Analytics API calls
until re-authorised. Re-run:

```bash
uv run scripts/yt_init.py --channel wordstrata
uv run scripts/yt_init.py --channel the-mythscape
uv run scripts/yt_init.py --channel open-verdicts
uv run scripts/yt_init.py --channel bright-beasts
```

Restart the API service after. Upload capability is retained — only the OAuth
consent dialog is re-shown to add the two new scopes.

Also ensure both **YouTube Data API v3** and **YouTube Analytics API** are
enabled in each channel's Google Cloud project.

### n8n "Daily Analytics Digest" workflow

- **Workflow ID:** `ENbQm9ctfNRcnOuT` — created **inactive**; activate after
  filling credentials and IDs (see README setup steps).
- **Schedule:** 06:00 daily — after the four upload workflows (01:00–04:00).
- **Fan-out:** a single `/analytics/daily` call feeds two branches off its
  success output — (a) `Split to rows` → 4 rows → Google Sheets `daily` tab via
  Google Service Account credential; (b) `{{ $json.digest_text }}` → Telegram +
  Slack. An HTTP-error branch sends a failure alert to both.
- **Per-video branch:** a second `GET /analytics/videos` call writes per-video
  rows to one Google Sheets tab per channel (tab name = channel slug; new
  channels auto-create their tab). This is a **live-only workflow** — it is not
  generated by `n8n/generate.py` and must be patched via the update script if
  the branch needs changes.

## 7. State and audit

`state.db` (SQLite) has two tables, both **channel-scoped**:

- **`words`** — the queue. Compound PK `(channel, id)` so each channel owns its own 1..N id space. `status IN ('pending','processing','done','failed','skipped')`. `GET /<channel>/state/next` returns the lowest-priority pending row for that channel.
- **`runs`** — one row per `/upload` event. Records: channel, file paths, model names (`script_model`, `image_model`, `tts_voice`), YouTube `video_id` + `url`, timestamps. **This is the audit trail.** When debugging "why did Tuesday's Wordstrata upload look different", you query this with `WHERE channel = 'wordstrata'`.
- **`analytics_snapshots`** — one row per channel per day. Powers the trend/milestone/alert diffs in `GET /analytics/daily` without extra API calls.
- **`video_snapshots`** — one row per video per day (cumulative totals at snapshot time). Powers the `*_today` delta columns in `GET /analytics/videos` by diffing against the prior day's snapshot. Added via `sql/schema.sql` with `CREATE TABLE IF NOT EXISTS` — no `user_version` bump required; the table is created automatically on startup.

Schema in `sql/schema.sql`. `db.record_completed_run(channel, word_id, ...)` is the canonical insert. Schema migrations run automatically on startup, gated by `PRAGMA user_version` — bump `TARGET_USER_VERSION` in `db.py` and add a migration callable when the schema changes.

### Filesystem layout

```
output/<channel>/scripts/word_{id:04d}.json
output/<channel>/audio/word_{id:04d}.wav
output/<channel>/images/word_{id:04d}_{i}.png
output/<channel>/videos/word_{id:04d}.mp4

channels/<channel>/brand.json          # icon prompts (the-mythscape, open-verdicts, bright-beasts)
assets/brand/<channel>/icon_<name>.png # 1024×1024 candidates from gen_brand.py

secrets/youtube_oauth.<channel>.json
secrets/youtube_token.<channel>.json
```

The `word_` prefix in filenames is purely historical — it stays the same across all channels (mythology, animals, etc.) so the existing assemble/upload path expectations keep working.

## 8. Notifications (Telegram + Slack)

All four pipeline workflows and the Analytics Digest send notifications to both
Telegram and Slack. Here is everything an agent needs to know to work on or
debug notifications.

### Live workflows and what they emit

| Workflow | Success signal | Error signal |
|---|---|---|
| Per-channel pipeline (×4) | "Notify success" (Telegram) + "Notify success (Slack)" nodes — fire after the YouTube upload step | "Pipeline Error Alert" workflow triggered by n8n Error Workflow setting |
| "Daily Analytics Digest" | Digest posted to Telegram + Slack | HTTP-error branch posts failure alert to Telegram + Slack |

### Shared "Pipeline Error Alert" workflow

An Error Trigger → Telegram + Slack workflow. It is the **Error Workflow** for
all four pipeline workflows. Wired in each workflow's **Settings → Error
Workflow** field. This catches any unhandled pipeline failure and posts a ❌
alert to both channels.

**Critical gotcha — import drops `errorWorkflow`:** n8n's workflow import (API
and UI) silently discards the `settings.errorWorkflow` field. After importing or
recreating any pipeline workflow you **must** manually re-set
**Settings → Error Workflow → "Pipeline Error Alert"** in the n8n UI. No import
or regeneration step can substitute for this; it must be done in the UI.

### Credential and identifier constants

| Destination | Identifier | n8n credential name |
|---|---|---|
| Telegram | chat ID `3819613` | "Telegram account" |
| Slack | channel `C0BBAB1G588` | "Slack account" |

These are constants in `n8n/generate.py` (`TELEGRAM_CHAT_ID`, `SLACK_CHANNEL`).
Neither is a secret — they are channel/chat identifiers only.

### `n8n/generate.py` emits notifications

`generate.py` emits both the Telegram and Slack success-notification nodes and
the `errorWorkflow` setting into each generated `n8n/workflows/<slug>.json`.
Regenerated workflow JSON therefore already carries the notification wiring —
the only post-import manual step is re-setting Error Workflow in the UI (see
gotcha above).

## 9. Workflow backup ("Backup Workflows to Git")

A scheduled n8n workflow commits all workflow definitions daily to a private
repo. Key facts for anyone modifying the backup workflow or its dependencies.

### Operational summary

| Property | Value |
|---|---|
| Schedule | 05:00 daily |
| Target repo | `SamyakTechLabs/stl-n8n-backups` (private) |
| Path in repo | `n8n/exports/<sanitized-name>.json` |
| Commit style | One atomic commit per run via GitHub Git Data API |
| Idempotency | Compares new tree SHA to base; **skips commit if nothing changed** |

### What is and isn't backed up

**Backed up:** workflow JSON definitions only. n8n exports reference credentials
by id/name — no tokens or secrets are present in the output.

**Not backed up:** credentials. This is intentional. Credential values must be
managed separately (n8n encrypted credential store, or a secrets manager).

### Mechanism

1. n8n API node lists all workflows.
2. A Code node normalises each definition: strips volatile keys (`versionId`,
   `updatedAt`, `createdAt`, `activeVersionId`, `triggerCount`, etc.) and sorts
   keys so diffs are stable across unrelated re-saves.
3. Commits via GitHub's **Git Data API** (create blobs → build tree → create
   commit → update ref). **Not** the simpler Contents API — that approach
   produces 409 conflicts on rapid multi-file commits.
4. If the computed tree SHA matches the current HEAD tree SHA, the commit step
   is skipped entirely.

### n8n credentials required

| n8n credential name | Type | Scope / notes |
|---|---|---|
| "GitHub account" | GitHub (PAT) | `repo` scope on the backup repo |
| "n8n account" | n8n API | Created via n8n → Settings → n8n API → Create key |

## 10. Future direction

The pipeline is feature-complete for daily Shorts on four channels (Wordstrata, The Mythscape, Open Verdicts, Bright Beasts). Open TODOs (in priority order):

1. **Affiliate footer** in YouTube description (per-channel — script LLM template change in each `channels/<slug>/prompts/script.md`)
2. **Pinned comment auto-post** in `/upload` (needs scope expansion to `youtube.force-ssl`, per-channel OAuth re-consent)
3. **Per-channel brand assets — partial**: `brand.json` + `gen_brand.py` exist for the three new channels (icon candidates only). Still pending: per-channel outro card / watermark, banner generator (2048×1152), and brand assets for `wordstrata` (currently uses a hand-designed icon).
4. **Whisper-based forced alignment** to make sentence captions tightly word-sync'd — currently captions are word-count-proportional within each beat, which trails the audio by ~200–400 ms.
5. **Long-form companion pipeline** — 10–15 min mini-docs sharing the same words queue and audio/image infra, but with different script and video assembly.

Don't add: feature flags, retry/backoff infra, k8s manifests, generic abstraction layers. This is a single-machine pipeline; over-architecting is the failure mode.
