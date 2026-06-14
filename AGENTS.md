# AGENTS.md — operating conventions for this codebase

Read this before changing anything. The pipeline is small but every step has
landmines we've stepped on already; this doc captures them so you don't have
to relearn them.

If `AGENTS.md` and `README.md` disagree, **`AGENTS.md` wins** for
implementation conventions. The README is for setup/usage; this file is for
people (or agents) editing code.

## 1. The standard restart cycle

```bash
# Restart the service after a code change:
pkill -9 -f "uvicorn main:app" 2>/dev/null; sleep 2
cd api && nohup uv run uvicorn main:app --host 0.0.0.0 --port 7860 \
  > /tmp/shorts-api.log 2>&1 &

# Wait ~6s for it to come up, then verify
sleep 6 && curl -sS --max-time 3 http://localhost:7860/health | jq .status
```

**With the default `space` backend, the Z-Image-Turbo model is NOT loaded
locally**, so restarts have no model-reload cost. The ~30 s reload note applies
only when the mflux fallback has fired during the session (the model is
lazy-loaded on first fallback and stays resident until the process exits).
Avoid restarting in the middle of an in-flight image-gen run regardless of
backend — the in-progress request is lost.

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
Gradio Space (`image_backend = "space"`). Anonymous Space calls get ~2 min
ZeroGPU GPU/day; a free HF account with `HF_TOKEN` set gets ~3.5–5 min/day
(enough for 2–3 of the 4 channels); an HF Pro account gets ~8× that, covering
all four. Set it in `api/.env` (gitignored). The mflux fallback absorbs any
quota overflow automatically.

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
images ≈ 50 min/video on an M1 Max. Z-Image-Turbo via the hosted Space backend
takes ~12 s/image (ZeroGPU, zero local RAM), giving roughly 1–2 min of image
generation per video in normal operation. The mflux local fallback path is
significantly slower: ~5 min/image on an M1 Max (the z-image-turbo transformer
is ~23 GB; a single 768×1344 render requires ~28 GB unified memory and thrashes
the macOS compressor on a 32 GB machine). Stick with the `space` backend unless
you're debugging or have no network.

### 2.13 Image backend: hosted Space (default) vs. local mflux

`api/config.py` `image_backend` controls which path `services/image.py` takes:

- **`"space"` (default)** — calls the hosted Z-Image-Turbo Gradio Space on
  Hugging Face (`zimage_space_url`, default `https://mrfakename-z-image-turbo.hf.space`).
  Free ZeroGPU; ~12 s/image; zero local RAM. Per-image automatic fallback to
  local mflux on ANY failure (GPU quota exceeded, network error, server error),
  so a run always completes even when the Space is unavailable.
- **`"mflux"`** — local MLX generation only. Use when you want deterministic
  local runs or are offline.

The local mflux model is **lazy-loaded only when a fallback actually fires**
(`_get_model()` in `image.py`). The `space` path never pays the ~28 GB
unified-memory cost unless it has to.

Relevant config knobs (all in `api/config.py`, env-overridable):

| Setting | Default | Notes |
|---|---|---|
| `image_backend` | `"space"` | `"space"` or `"mflux"` |
| `zimage_space_url` | `https://mrfakename-z-image-turbo.hf.space` | Gradio Space endpoint |
| `zimage_space_timeout_s` | `180` | Per-image Space call timeout |
| `hf_token` | `None` | Set via `HF_TOKEN` in `api/.env`; raises ZeroGPU quota |
| `mflux_cache_limit_bytes` | `1 GiB` | Caps MLX GPU-buffer cache; `mx.clear_cache()` called between mflux renders |

## 3. Files where the most damage happens

These are the high-impact files. Read carefully, change with intent.

| File | What it controls | Test after editing |
|---|---|---|
| `channels/<slug>/prompts/script.md` | Output quality of the LLM for that channel. Image prompts, narration tone, sentence-level captions. Each channel has its own. | Regen one script (`curl -X POST /<slug>/script` with a `word_id`) and *read it* before any image gen. |
| `channels/<slug>/channel.json` | Per-channel metadata — slug, display name, YouTube handle, default categories, and YouTube upload defaults (`youtube_category_id`, `youtube_default_language`, `youtube_default_audio_language`, `youtube_license`, `ai_disclosure`). Loaded at runtime by `api/channels.py`. | `curl /channels` to confirm the registry sees it. |
| `api/services/youtube.py` | YouTube upload via Data API v3. Sets `categoryId`, `defaultLanguage`, `defaultAudioLanguage`, `containsSyntheticMedia`, `license`, `embeddable`, `publicStatsViewable`, `madeForKids`. Defaults flow from `ChannelConfig`; `UploadRequest` can override per-call. | Upload one video with `privacy: "private"`, then in Studio confirm category, language, and "Altered content" disclosure are set. |
| `api/channels.py` | Channel registry (file-based). `load(slug)` resolves a `ChannelConfig`; `list_slugs()` discovers all channels at runtime. | Lookup an unknown slug — should 404 with a clear message. |
| `api/models.py` (`Script`) | Schema the LLM must satisfy. Each `Beat` carries `images: list[str]` (1–4 diffusion prompts). `Script.image_prompts` is a computed property that flattens every beat's `images` in order — the rest of the pipeline reads this property, so generated images and shown images are always 1:1 by construction. A `model_validator` enforces the total image count is 4–7 (`MIN_IMAGES_TOTAL=4`, `MAX_IMAGES_TOTAL=7`). No index pool, no permutation rule; orphan/unused prompts are structurally impossible. | Round-trip a JSON through `Script.model_validate(...)`. |
| `api/services/video.py` | ffmpeg filter graph. Easy to break the whole video silently — `-shortest` masks bugs. | Always extract frames at `t=1s`, `t=mid-beat`, `t=outro_start+0.5s` after a change. |
| `api/config.py` | All runtime knobs. Pydantic-settings; env-overridable. Helpers `channel_data_dir()`, `youtube_oauth_path(channel)`, `youtube_token_path(channel)` resolve channel-scoped paths. | Health check (`/health`) shows current values + the channels list. |
| `n8n/generate.py` | Per-channel workflow generator. Reads every `channels/<slug>/channel.json` and writes `n8n/workflows/<slug>.json`. | Run, then re-import each workflow into n8n. |
| `channels/<slug>/brand.json` | Channel icon prompts. Edit the `brand_concept` / `color_palette` / `icon_prompts` to redesign the channel's visual mark. | `uv run --project api scripts/gen_brand.py --channel <slug> --only <prompt-name>` and view the PNG. |
| `scripts/gen_brand.py` | Renders icon candidates from a channel's `brand.json`. Already calls `unlink()` before `save_image` so the §2.1 mflux landmine is dodged here. | Run with `--only` for a single candidate. |

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
2. **`/image` change**: regen one word's images (~1–2 min via Space backend), open the PNGs.
3. **`/voice` change**: regen one WAV, play in QuickTime, listen for emoji/markdown bleed-through.
4. **`/assemble` change**: regen one video, extract frames at every beat boundary + outro start, inspect captions and image transitions.
5. **`/upload` change**: trigger with `privacy: "private"`, check it lands in Studio.
6. **n8n change**: Execute Workflow once end-to-end; check the audit trail in `runs` table.

## 6. State and audit

`state.db` (SQLite) has two tables, both **channel-scoped**:

- **`words`** — the queue. Compound PK `(channel, id)` so each channel owns its own 1..N id space. `status IN ('pending','processing','done','failed','skipped')`. `GET /<channel>/state/next` returns the lowest-priority pending row for that channel.
- **`runs`** — one row per `/upload` event. Records: channel, file paths, model names (`script_model`, `image_model`, `tts_voice`), YouTube `video_id` + `url`, timestamps. **This is the audit trail.** When debugging "why did Tuesday's Wordstrata upload look different", you query this with `WHERE channel = 'wordstrata'`.

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

## 7. Future direction

The pipeline is feature-complete for daily Shorts on four channels (Wordstrata, The Mythscape, Open Verdicts, Bright Beasts). Open TODOs (in priority order):

1. **Affiliate footer** in YouTube description (per-channel — script LLM template change in each `channels/<slug>/prompts/script.md`)
2. **Pinned comment auto-post** in `/upload` (needs scope expansion to `youtube.force-ssl`, per-channel OAuth re-consent)
3. **Per-channel brand assets — partial**: `brand.json` + `gen_brand.py` exist for the three new channels (icon candidates only). Still pending: per-channel outro card / watermark, banner generator (2048×1152), and brand assets for `wordstrata` (currently uses a hand-designed icon).
4. **Whisper-based forced alignment** to make sentence captions tightly word-sync'd — currently captions are word-count-proportional within each beat, which trails the audio by ~200–400 ms.
5. **Long-form companion pipeline** — 10–15 min mini-docs sharing the same words queue and audio/image infra, but with different script and video assembly.

Don't add: feature flags, retry/backoff infra, k8s manifests, generic abstraction layers. This is a single-machine pipeline; over-architecting is the failure mode.
