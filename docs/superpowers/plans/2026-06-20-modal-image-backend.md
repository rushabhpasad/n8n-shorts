# Modal Image Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fast, paid Z-Image-Turbo GPU backend on Modal as the primary image tier, with the free HF Space and local mflux as automatic fallbacks (Modal → Space → mflux).

**Architecture:** A Modal app (`modal_app/zimage_app.py`) loads Z-Image-Turbo onto an L4 GPU and exposes an authenticated HTTPS `POST /generate` endpoint returning PNG bytes. The stl FastAPI calls it via `httpx` from `api/services/image.py`, which is refactored from a hardcoded two-step fallback into an ordered tier chain. Any Modal failure falls through to the existing Space chain, so runs always complete. The Modal app auto-deploys via GitHub Actions on merge to `main`.

**Tech Stack:** Modal (v1.5.0 client), diffusers (from git — provides `ZImagePipeline`), torch (bfloat16, CUDA), FastAPI, httpx, pydantic-settings, pytest.

## Global Constraints

- **Never commit to `main`** — all work on branch `feat/modal-image-backend`.
- **Conventional commits**; subject = what, body = why.
- **TDD**: failing test → minimal impl → pass → commit. New behavior ships with tests.
- **Config from env** (12-factor): all Modal settings via `config.py` Field defaults + env override; secrets only in `api/.env` (never committed) and Modal Secrets / GitHub Secrets.
- **Named exports / explicit functions**; no `any`-style escape hatches.
- **Source files < 600 lines** (hard ceiling 1000). `image.py` is ~300 now; keep the refactor lean.
- **Monetization-safe model only**: Z-Image-Turbo (Tongyi-MAI, open license) — same model as Space + mflux. Do not introduce gated/NC models.
- **diffusers MUST be installed from git** (`git+https://github.com/huggingface/diffusers`) — that is where `ZImagePipeline` / Z-Image support lives.
- **Turbo requires `guidance_scale=0.0`** in the diffusers pipeline call.
- **Directory is `modal_app/`, NOT `modal/`** — a top-level `modal/` package shadows the `modal` pip SDK and breaks `import modal` / `modal deploy`.
- **Default `image_backend` stays `"space"`** on merge; the flip to `"modal"` is a deliberate post-deploy step (Task 7).
- **Image resolution** stays 768×1344, steps 8 (from existing `settings.mflux_*`).

---

### Task 1: Config additions + backend-chain helper

**Files:**
- Modify: `api/config.py` (the `image_backend` field block, ~line 48-62)
- Modify: `api/services/image.py` (add `_image_backend_chain` near the top, after imports)
- Test: `api/test_image.py` (append)

**Interfaces:**
- Produces: `settings.image_backend: Literal["modal","space","mflux"]`; `settings.modal_image_url: str | None`; `settings.modal_image_token: str | None`; `settings.modal_attempts: int`; `settings.modal_retry_sleep_s: float`; `settings.modal_timeout_s: float`
- Produces: `image._image_backend_chain(backend: str) -> list[str]` returning the ordered tier list.

- [ ] **Step 1: Write the failing test**

Append to `api/test_image.py`:

```python
from services.image import _image_backend_chain


def test_image_backend_chain_modal():
    assert _image_backend_chain("modal") == ["modal", "space", "mflux"]


def test_image_backend_chain_space_unchanged():
    assert _image_backend_chain("space") == ["space", "mflux"]


def test_image_backend_chain_mflux_only():
    assert _image_backend_chain("mflux") == ["mflux"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && uv run pytest test_image.py::test_image_backend_chain_modal -v`
Expected: FAIL — `ImportError: cannot import name '_image_backend_chain'`

- [ ] **Step 3: Add the config fields**

In `api/config.py`, change the `image_backend` line and add the Modal block. Replace:

```python
    image_backend: Literal["space", "mflux"] = Field(default="space")
```

with:

```python
    image_backend: Literal["modal", "space", "mflux"] = Field(default="space")
```

Then, immediately after the `hf_token` field (end of the image-gen block, ~line 62), add:

```python
    # Modal GPU image backend. When image_backend="modal" the per-image chain is
    # Modal → Space → mflux: fast paid GPU first, free Space next, local mflux last.
    # URL + token come from the deployed Modal app and a shared bearer token; set
    # both via env on stl (MODAL_IMAGE_URL, MODAL_IMAGE_TOKEN), never commit them.
    modal_image_url: str | None = Field(default=None)
    modal_image_token: str | None = Field(default=None)
    modal_attempts: int = Field(default=2)            # total tries (1 + 1 retry)
    modal_retry_sleep_s: float = Field(default=5.0)
    modal_timeout_s: float = Field(default=120.0)     # covers cold start + generation
```

- [ ] **Step 4: Add the chain helper**

In `api/services/image.py`, after the module-level logger/`_model` declarations (after line ~31, before `_get_model`), add:

```python
def _image_backend_chain(backend: str) -> list[str]:
    """Ordered per-image tier list for a given image_backend setting.

    mflux is always the terminal tier — it runs locally and always completes,
    so a run never fails for lack of a backend.
    """
    if backend == "modal":
        return ["modal", "space", "mflux"]
    if backend == "space":
        return ["space", "mflux"]
    return ["mflux"]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd api && uv run pytest test_image.py -k "image_backend_chain" -v`
Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
git add api/config.py api/services/image.py api/test_image.py
git commit -m "feat(api): add modal image-backend config + tier-chain helper"
```

---

### Task 2: Modal client (`_generate_via_modal` + retries)

**Files:**
- Modify: `api/services/image.py` (add `import httpx` at top; add the two functions after `_generate_via_space_with_retries`, ~line 207)
- Test: `api/test_image.py` (append)

**Interfaces:**
- Consumes: `settings.modal_image_url`, `settings.modal_image_token`, `settings.modal_timeout_s`, `settings.modal_attempts`, `settings.modal_retry_sleep_s` (Task 1).
- Produces: `image._generate_via_modal(prompt, width, height, steps, seed) -> bytes`
- Produces: `image._generate_via_modal_with_retries(prompt, width, height, steps, seed, attempts, sleep_s, label="") -> bytes`

- [ ] **Step 1: Write the failing tests**

Append to `api/test_image.py`:

```python
from services.image import _generate_via_modal, _generate_via_modal_with_retries


class _FakeResp:
    def __init__(self, content=b"PNG", status_ok=True):
        self.content = content
        self._ok = status_ok

    def raise_for_status(self):
        if not self._ok:
            raise RuntimeError("modal 500")


def test_generate_via_modal_posts_payload(monkeypatch):
    monkeypatch.setattr(image.settings, "modal_image_url", "https://x.modal.run/")
    monkeypatch.setattr(image.settings, "modal_image_token", "tok123")
    monkeypatch.setattr(image.settings, "modal_timeout_s", 99.0)
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        return _FakeResp(content=b"MODALPNG")

    monkeypatch.setattr(image.httpx, "post", fake_post)

    out = _generate_via_modal("a prompt", 768, 1344, 8, 42)

    assert out == b"MODALPNG"
    assert captured["url"] == "https://x.modal.run/generate"   # rstrip slash + /generate
    assert captured["headers"] == {"Authorization": "Bearer tok123"}
    assert captured["json"] == {"prompt": "a prompt", "width": 768,
                                "height": 1344, "steps": 8, "seed": 42}
    assert captured["timeout"] == 99.0


def test_generate_via_modal_raises_when_unconfigured(monkeypatch):
    import pytest
    monkeypatch.setattr(image.settings, "modal_image_url", None)
    monkeypatch.setattr(image.settings, "modal_image_token", None)
    with pytest.raises(RuntimeError, match="modal backend not configured"):
        _generate_via_modal("p", 768, 1344, 8, 42)


def test_modal_retries_succeeds_after_transient_failure(monkeypatch):
    attempts = {"n": 0}
    slept = []

    def flaky(*a, **k):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise RuntimeError("cold start timeout")
        return b"OK"

    monkeypatch.setattr(image, "_generate_via_modal", flaky)
    monkeypatch.setattr(image.time, "sleep", lambda s: slept.append(s))

    out = _generate_via_modal_with_retries("p", 768, 1344, 8, 42, attempts=2, sleep_s=5.0)

    assert out == b"OK"
    assert attempts["n"] == 2
    assert slept == [5.0]


def test_modal_retries_raises_after_exhausting(monkeypatch):
    import pytest
    attempts = {"n": 0}

    def always_fails(*a, **k):
        attempts["n"] += 1
        raise RuntimeError("modal down")

    monkeypatch.setattr(image, "_generate_via_modal", always_fails)
    monkeypatch.setattr(image.time, "sleep", lambda s: None)

    with pytest.raises(RuntimeError, match="modal down"):
        _generate_via_modal_with_retries("p", 768, 1344, 8, 42, attempts=2, sleep_s=5.0)
    assert attempts["n"] == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd api && uv run pytest test_image.py -k modal -v`
Expected: FAIL — `ImportError: cannot import name '_generate_via_modal'`

- [ ] **Step 3: Add `import httpx` at the top of `image.py`**

In `api/services/image.py`, in the import block (after `import time`, ~line 22), add:

```python
import httpx
```

(httpx is already a runtime dependency; importing at module level makes it monkeypatchable as `image.httpx`.)

- [ ] **Step 4: Implement the client functions**

In `api/services/image.py`, after `_generate_via_space_with_retries` (ends ~line 206), add:

```python
def _generate_via_modal(prompt: str, width: int, height: int, steps: int, seed: int) -> bytes:
    """Generate one image via the Modal Z-Image-Turbo endpoint. Returns PNG bytes.

    Raises on missing config / non-200 / network error so the caller can fall
    back to the Space chain.
    """
    if not (settings.modal_image_url and settings.modal_image_token):
        raise RuntimeError("modal backend not configured (set MODAL_IMAGE_URL/MODAL_IMAGE_TOKEN)")
    resp = httpx.post(
        f"{settings.modal_image_url.rstrip('/')}/generate",
        headers={"Authorization": f"Bearer {settings.modal_image_token}"},
        json={"prompt": prompt, "width": width, "height": height, "steps": steps, "seed": seed},
        timeout=settings.modal_timeout_s,
    )
    resp.raise_for_status()
    return resp.content


def _generate_via_modal_with_retries(
    prompt: str, width: int, height: int, steps: int, seed: int,
    attempts: int, sleep_s: float, label: str = "",
) -> bytes:
    """Call Modal up to `attempts` times, sleeping `sleep_s` between tries.

    Cold-start/transient failures usually clear on a retry. Raises the last
    exception if every attempt fails so the caller can fall back to the Space.
    """
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _generate_via_modal(prompt, width, height, steps, seed)
        except Exception as e:
            last_exc = e
            if attempt < attempts:
                log.warning(
                    "%smodal attempt %d/%d failed (%s) — retrying in %.0fs",
                    label, attempt, attempts, str(e)[:200], sleep_s,
                )
                time.sleep(sleep_s)
    assert last_exc is not None
    raise last_exc
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd api && uv run pytest test_image.py -k modal -v`
Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
git add api/services/image.py api/test_image.py
git commit -m "feat(api): add Modal image client with retry/fallback semantics"
```

---

### Task 3: Refactor `generate_images` to the tier chain

**Files:**
- Modify: `api/services/image.py` (`generate_images`, ~line 232-303)
- Test: `api/test_image.py` (append)

**Interfaces:**
- Consumes: `_image_backend_chain` (Task 1), `_generate_via_modal_with_retries` (Task 2), existing `_generate_via_space_with_retries`, `_get_model`, `_render_mflux`.
- Produces: unchanged public signature `generate_images(script, output_dir, word_id) -> list[dict]`; per-image dict may now record `"modal"` in backend accounting.

- [ ] **Step 1: Write the failing tests**

Append to `api/test_image.py`:

```python
from types import SimpleNamespace
from services.image import generate_images


def _script(prompts):
    return SimpleNamespace(image_prompts=prompts)


def test_generate_images_modal_success(tmp_path, monkeypatch):
    monkeypatch.setattr(image.settings, "image_backend", "modal")
    monkeypatch.setattr(image, "_generate_via_modal_with_retries",
                        lambda *a, **k: b"MODAL")

    def boom(*a, **k):
        raise AssertionError("should not be reached")
    monkeypatch.setattr(image, "_generate_via_space_with_retries", boom)
    monkeypatch.setattr(image, "_get_model", boom)

    res = generate_images(_script(["p0"]), tmp_path, 7)

    assert len(res) == 1
    out = tmp_path / "word_0007_0.png"
    assert out.read_bytes() == b"MODAL"


def test_generate_images_modal_falls_to_space(tmp_path, monkeypatch):
    monkeypatch.setattr(image.settings, "image_backend", "modal")

    def modal_fail(*a, **k):
        raise RuntimeError("modal down")
    monkeypatch.setattr(image, "_generate_via_modal_with_retries", modal_fail)
    monkeypatch.setattr(image, "_generate_via_space_with_retries",
                        lambda *a, **k: b"SPACE")

    def boom(*a, **k):
        raise AssertionError("mflux should not load")
    monkeypatch.setattr(image, "_get_model", boom)

    res = generate_images(_script(["p0"]), tmp_path, 8)

    assert (tmp_path / "word_0008_0.png").read_bytes() == b"SPACE"
    assert len(res) == 1


def test_generate_images_modal_space_fall_to_mflux(tmp_path, monkeypatch):
    monkeypatch.setattr(image.settings, "image_backend", "modal")

    def fail(*a, **k):
        raise RuntimeError("backend down")
    monkeypatch.setattr(image, "_generate_via_modal_with_retries", fail)
    monkeypatch.setattr(image, "_generate_via_space_with_retries", fail)
    monkeypatch.setattr(image, "_get_model", lambda: "FAKE_MODEL")

    def fake_render(model, prompt, seed, out_path):
        assert model == "FAKE_MODEL"
        out_path.write_bytes(b"MFLUX")
    monkeypatch.setattr(image, "_render_mflux", fake_render)

    res = generate_images(_script(["p0"]), tmp_path, 9)

    assert (tmp_path / "word_0009_0.png").read_bytes() == b"MFLUX"
    assert len(res) == 1


def test_generate_images_space_backend_unchanged(tmp_path, monkeypatch):
    """Regression: image_backend='space' never touches Modal."""
    monkeypatch.setattr(image.settings, "image_backend", "space")

    def boom(*a, **k):
        raise AssertionError("modal must not be called for space backend")
    monkeypatch.setattr(image, "_generate_via_modal_with_retries", boom)
    monkeypatch.setattr(image, "_generate_via_space_with_retries",
                        lambda *a, **k: b"SPACE")
    res = generate_images(_script(["p0"]), tmp_path, 5)
    assert (tmp_path / "word_0005_0.png").read_bytes() == b"SPACE"
    assert len(res) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd api && uv run pytest test_image.py -k generate_images -v`
Expected: FAIL — the current `generate_images` ignores Modal (`AssertionError` / wrong bytes).

- [ ] **Step 3: Refactor `generate_images`**

In `api/services/image.py`, replace the body of `generate_images` from the `backend = settings.image_backend` line through the end of the per-image `for` loop (the block that does space-or-mflux, ~lines 246-297) with the tier-chain version below. Keep the function signature, the `_clear_stale_images` call, the `results`/`backends_used` setup, the result-dict shape, and the final summary log unchanged.

Replace:

```python
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
            label = f"[{i + 1}/{n}] "
            try:
                log.info(
                    "%sspace generate seed=%d %dx%d steps=%d → %s",
                    label, seed, width, height, steps, out_path.name,
                )
                out_path.write_bytes(_generate_via_space_with_retries(
                    prompt, width, height, steps, seed,
                    settings.zimage_space_attempts,
                    settings.zimage_space_retry_sleep_s,
                    label=label,
                ))
                used = "space"
            except Exception as e:
                log.warning(
                    "%sspace backend failed after %d attempts (%s) — falling back to mflux",
                    label, settings.zimage_space_attempts, str(e)[:200],
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
```

with:

```python
    chain = _image_backend_chain(settings.image_backend)
    width, height, steps = settings.mflux_width, settings.mflux_height, settings.mflux_steps
    mflux_model = None  # lazy — only loaded if the mflux tier is actually reached

    results: list[dict] = []
    backends_used: list[str] = []
    for i, prompt in enumerate(script.image_prompts):
        out_path = output_dir / f"word_{word_id:04d}_{i}.png"
        seed = settings.mflux_seed_base + i
        label = f"[{i + 1}/{n}] "
        used: str | None = None

        for idx, tier in enumerate(chain):
            is_last = idx == len(chain) - 1
            try:
                if tier == "modal":
                    log.info("%smodal generate seed=%d %dx%d steps=%d → %s",
                             label, seed, width, height, steps, out_path.name)
                    out_path.write_bytes(_generate_via_modal_with_retries(
                        prompt, width, height, steps, seed,
                        settings.modal_attempts, settings.modal_retry_sleep_s,
                        label=label,
                    ))
                elif tier == "space":
                    log.info("%sspace generate seed=%d %dx%d steps=%d → %s",
                             label, seed, width, height, steps, out_path.name)
                    out_path.write_bytes(_generate_via_space_with_retries(
                        prompt, width, height, steps, seed,
                        settings.zimage_space_attempts,
                        settings.zimage_space_retry_sleep_s, label=label,
                    ))
                else:  # mflux — terminal tier
                    if mflux_model is None:
                        mflux_model = _get_model()
                    log.info("%smflux generate seed=%d %dx%d steps=%d → %s",
                             label, seed, width, height, steps, out_path.name)
                    _render_mflux(mflux_model, prompt, seed, out_path)
                used = tier
                break
            except Exception as e:
                if is_last:
                    raise
                log.warning("%s%s backend failed (%s) — falling back to %s",
                            label, tier, str(e)[:200], chain[idx + 1])

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
        "image gen done: %d images (modal=%d space=%d mflux=%d)",
        n, backends_used.count("modal"),
        backends_used.count("space"), backends_used.count("mflux"),
    )
    return results
```

- [ ] **Step 4: Run the full image test suite to verify pass + no regressions**

Run: `cd api && uv run pytest test_image.py -v`
Expected: all pass (existing space/stale/bytes tests + new chain/integration tests).

- [ ] **Step 5: Commit**

```bash
git add api/services/image.py api/test_image.py
git commit -m "refactor(api): generate_images uses ordered tier chain (modal→space→mflux)"
```

---

### Task 4: Modal app (`modal_app/zimage_app.py`)

**Files:**
- Create: `modal_app/__init__.py` (empty)
- Create: `modal_app/zimage_app.py`

**Interfaces:**
- Produces: a deployed Modal app `n8n-shorts-zimage` exposing `POST /generate` (bearer-authed) → `image/png` bytes. Consumed at runtime by `_generate_via_modal` (Task 2). No Python import coupling to the `api/` package.

- [ ] **Step 1: Create the package marker**

```bash
mkdir -p modal_app
: > modal_app/__init__.py
```

- [ ] **Step 2: Write the Modal app**

Create `modal_app/zimage_app.py`:

```python
"""Modal app: Z-Image-Turbo on a GPU, exposed as an authenticated HTTPS endpoint.

Deploy:  modal deploy -m modal_app.zimage_app
(auto-deployed by .github/workflows/deploy-modal.yml on push to main touching
modal_app/**). The stl FastAPI calls POST {url}/generate with a bearer token
(api/services/image.py::_generate_via_modal); on any failure stl falls back to
the HF Space, then local mflux.

The shared bearer token is injected from the Modal Secret `zimage-token`
(key ZIMAGE_TOKEN). Model weights are baked into the image at build time via
snapshot_download, so cold starts never re-download the ~15GB checkpoint.
"""

import io
import os

import modal

MODEL_ID = "Tongyi-MAI/Z-Image-Turbo"
GPU = "L4"


def _download_weights() -> None:
    from huggingface_hub import snapshot_download

    snapshot_download(MODEL_ID)


image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "torch",
        "git+https://github.com/huggingface/diffusers",  # provides ZImagePipeline
        "transformers",
        "accelerate",
        "safetensors",
        "sentencepiece",
        "huggingface_hub",
        "Pillow",
        "fastapi[standard]",
    )
    .run_function(_download_weights)  # bake weights into the image layer
)

app = modal.App("n8n-shorts-zimage")


@app.cls(
    gpu=GPU,
    image=image,
    secrets=[modal.Secret.from_name("zimage-token")],
    scaledown_window=60,   # keep warm ~60s for retries/re-runs, then scale to zero
    timeout=600,
)
class ZImage:
    @modal.enter()
    def load(self) -> None:
        import torch
        from diffusers import DiffusionPipeline

        self.torch = torch
        self.pipe = DiffusionPipeline.from_pretrained(
            MODEL_ID, torch_dtype=torch.bfloat16, low_cpu_mem_usage=False
        )
        self.pipe.to("cuda")

    @modal.asgi_app()
    def web(self):
        from fastapi import FastAPI, Header, HTTPException, Response
        from pydantic import BaseModel

        web_app = FastAPI()
        expected = os.environ["ZIMAGE_TOKEN"]

        class GenReq(BaseModel):
            prompt: str
            width: int = 768
            height: int = 1344
            steps: int = 8
            seed: int = 42

        @web_app.post("/generate")
        def generate(req: GenReq, authorization: str = Header(default="")):
            if authorization != f"Bearer {expected}":
                raise HTTPException(status_code=401, detail="invalid token")
            generator = self.torch.Generator("cuda").manual_seed(int(req.seed))
            result = self.pipe(
                prompt=req.prompt,
                height=int(req.height),
                width=int(req.width),
                num_inference_steps=int(req.steps),
                guidance_scale=0.0,   # required 0 for Turbo
                generator=generator,
            )
            buf = io.BytesIO()
            result.images[0].save(buf, format="PNG")
            return Response(content=buf.getvalue(), media_type="image/png")

        return web_app
```

- [ ] **Step 3: Local syntax/import gate (no GPU needed)**

The app imports only `io`, `os`, `modal` at module load (torch/diffusers import inside methods, which run in the Modal container). Verify it parses and the SDK objects resolve:

Run: `python -m py_compile modal_app/zimage_app.py && echo OK`
Expected: `OK`

(Full GPU generation is validated by the deploy + curl smoke test in Task 7 — there is no GPU in CI.)

- [ ] **Step 4: Commit**

```bash
git add modal_app/__init__.py modal_app/zimage_app.py
git commit -m "feat(modal): Z-Image-Turbo GPU app with authenticated /generate endpoint"
```

---

### Task 5: GitHub Actions auto-deploy workflow

**Files:**
- Create: `.github/workflows/deploy-modal.yml`

**Interfaces:**
- Consumes: GitHub repo secrets `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET` (already set 2026-06-20).
- Produces: automatic `modal deploy` on every push to `main` that touches `modal_app/**`.

- [ ] **Step 1: Write the workflow**

Create `.github/workflows/deploy-modal.yml`:

```yaml
name: Deploy Modal app
on:
  push:
    branches: [main]
    paths: ['modal_app/**', '.github/workflows/deploy-modal.yml']
jobs:
  deploy:
    runs-on: ubuntu-latest
    env:
      MODAL_TOKEN_ID: ${{ secrets.MODAL_TOKEN_ID }}
      MODAL_TOKEN_SECRET: ${{ secrets.MODAL_TOKEN_SECRET }}
    steps:
      - uses: actions/checkout@v6
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install modal
      - run: python -m py_compile modal_app/zimage_app.py   # fast syntax gate
      - run: modal deploy -m modal_app.zimage_app
```

- [ ] **Step 2: Validate YAML locally**

Run: `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/deploy-modal.yml')); print('valid yaml')"`
Expected: `valid yaml`

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/deploy-modal.yml
git commit -m "ci: auto-deploy Modal app on push to main (modal_app/**)"
```

---

### Task 6: Documentation

**Files:**
- Modify: `AGENTS.md` (add a §2.x subsection in the image-backend area)
- Modify: `README.md` (image-gen row, ~line 396 area)

**Interfaces:** none (docs only).

- [ ] **Step 1: Add the AGENTS.md subsection**

In `AGENTS.md`, after the existing image-backend coverage (near §2.x for image gen), add a new subsection. Use the next free subsection number in that file:

```markdown
### 2.x Modal image backend (paid GPU, primary tier)

`image_backend="modal"` makes image gen a 3-tier chain: **Modal → Space → mflux**
(`api/services/image.py::_image_backend_chain`). Modal is the fast paid GPU path;
the free HF Space is the backstop; local mflux is the last resort (always completes).

- **Modal app:** `modal_app/zimage_app.py` — Z-Image-Turbo on an L4 GPU, exposed as
  `POST /generate` (bearer-authed) returning PNG bytes. Scale-to-zero; weights baked
  into the image (no runtime re-download). Deploy: `modal deploy -m modal_app.zimage_app`.
  **The directory is `modal_app/`, not `modal/`, to avoid shadowing the `modal` SDK.**
- **Auto-deploy:** `.github/workflows/deploy-modal.yml` runs `modal deploy` on push to
  `main` touching `modal_app/**`. Needs repo secrets `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET`.
- **Endpoint auth:** Modal Secret `zimage-token` (key `ZIMAGE_TOKEN`) must match stl's
  `MODAL_IMAGE_TOKEN`. stl also needs `MODAL_IMAGE_URL` (the deployed endpoint URL).
- **Smoke test:**
  `curl -X POST "$MODAL_IMAGE_URL/generate" -H "Authorization: Bearer $MODAL_IMAGE_TOKEN" -H 'content-type: application/json' -d '{"prompt":"a red fox","width":768,"height":1344,"steps":8,"seed":42}' -o /tmp/modal_test.png`

| Setting | Default | Notes |
|---|---|---|
| `image_backend` | `"space"` | `"modal"`, `"space"`, or `"mflux"`; `"modal"` → Modal→Space→mflux |
| `modal_image_url` | `None` | Deployed Modal endpoint base URL (env `MODAL_IMAGE_URL`) |
| `modal_image_token` | `None` | Shared bearer token (env `MODAL_IMAGE_TOKEN`) |
| `modal_attempts` | `2` | total tries before falling to Space |
| `modal_retry_sleep_s` | `5.0` | sleep between Modal retries |
| `modal_timeout_s` | `120` | HTTP timeout (covers cold start) |
```

- [ ] **Step 2: Update the README image-gen row**

In `README.md`, update the image-gen pipeline-stage row to mention the Modal backend. Change the existing image row (the one naming Space/mflux) to also note: "Optional **Modal** GPU backend (`image_backend=modal`) → Modal→Space→mflux — see AGENTS.md."

- [ ] **Step 3: Commit**

```bash
git add AGENTS.md README.md
git commit -m "docs: document Modal image backend, chain, and auto-deploy"
```

---

### Task 7: Deploy, wire secrets, and cut over (ops)

**Files:** none (runtime ops on Modal + stl). Run after the PR merges to `main`.

**Interfaces:** Consumes the merged code + auto-deployed Modal app. Produces a live `image_backend=modal` on stl.

- [ ] **Step 1: Create the shared bearer token as a Modal Secret**

Generate a token and store it on Modal (run locally where `modal` is authed):

```bash
TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
modal secret create zimage-token ZIMAGE_TOKEN="$TOKEN"
echo "$TOKEN"   # capture once for stl .env; do not commit
```

- [ ] **Step 2: Confirm the Modal app deployed**

After the PR merges, GitHub Actions deploys it. Verify:

```bash
modal app list                          # expect n8n-shorts-zimage = deployed
modal app logs n8n-shorts-zimage | tail # optional
```

Capture the endpoint URL from the deploy output / dashboard (stable across redeploys), e.g. `https://<workspace>--n8n-shorts-zimage-zimage-web.modal.run`.

- [ ] **Step 3: Smoke-test the endpoint**

```bash
curl -sS -X POST "$MODAL_IMAGE_URL/generate" \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"prompt":"a red fox in snow","width":768,"height":1344,"steps":8,"seed":42}' \
  -o /tmp/modal_test.png && file /tmp/modal_test.png
```

Expected: `/tmp/modal_test.png: PNG image data, 768 x 1344`. (First call pays cold start ~20-30s.)

- [ ] **Step 4: Wire stl env + flip the backend**

On stl, append to `api/.env` (timestamped backup first, per the service-restart runbook):

```bash
ssh stl 'cp ~/n8n-shorts/api/.env ~/n8n-shorts/api/.env.bak.$(date +%s) && \
  printf "MODAL_IMAGE_URL=%s\nMODAL_IMAGE_TOKEN=%s\nIMAGE_BACKEND=modal\n" "<URL>" "<TOKEN>" >> ~/n8n-shorts/api/.env'
```

(`IMAGE_BACKEND` env overrides the `image_backend` default; pydantic-settings is case-insensitive.)

- [ ] **Step 5: Restart the launchd-managed service**

```bash
ssh stl 'launchctl kickstart -k gui/501/com.n8n-shorts.api'
ssh stl 'for i in $(seq 1 15); do c=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:7860/health); [ "$c" = 200 ] && { echo ok; break; }; sleep 3; done'
```

- [ ] **Step 6: Verify end-to-end on a real run**

Trigger one channel pipeline (or wait for the next scheduled run) and confirm the uvicorn log shows `modal generate` as the serving tier and the per-image latency drop vs mflux:

```bash
ssh stl 'tail -40 ~/n8n-shorts/api/uvicorn.log | tr -d "\000" | grep -E "modal|space|mflux generate|image gen done"'
```

Expected: lines showing `modal generate …` and a final `image gen done: N images (modal=N space=0 mflux=0)`.

---

## Self-Review

**Spec coverage:**
- Modal backend running Z-Image-Turbo on GPU → Task 4. ✓
- Chain Modal→Space→mflux → Tasks 1 + 3. ✓
- Same model/res/steps/seed across tiers → Task 4 uses 768×1344/steps/seed from client; client sends `settings.mflux_*` (Task 3). ✓
- Scale-to-zero → Task 4 (`scaledown_window=60`, no `min_containers`). ✓
- Auto-deploy via GitHub Actions → Task 5. ✓
- Always completes (Modal failure → Space → mflux) → Task 3 (`is_last` re-raise only on terminal mflux) + Tasks 2/3 tests. ✓
- HTTPS endpoint called via httpx → Tasks 2 + 4. ✓
- Config knobs → Task 1. ✓
- Tests (client, chain, regression) → Tasks 2 + 3. ✓
- Docs (AGENTS.md, README.md) → Task 6. ✓
- Cutover (secret, env, flip, restart, verify) → Task 7. ✓

**Deviations from spec (flagged):**
1. **Directory `modal_app/` not `modal/`** — avoids shadowing the `modal` pip SDK (breaks `import modal`/`modal deploy`). Necessary correction.
2. **Weights baked into the image** (`run_function(snapshot_download)`) instead of a runtime `modal.Volume` — same outcome (no re-download on cold start), simpler and more deterministic (no Volume commit/reload semantics). Acceptable per "improve code you're working in / YAGNI."
3. **No pytest for the Modal app** — importing it requires the `modal` SDK (not an `api/` test dep) and torch/diffusers (GPU). Validation is `py_compile` (CI gate) + `modal deploy` (which fails red on broken code) + the curl smoke test. Equivalent coverage without a GPU in CI.

**Placeholder scan:** No TBD/TODO; every code/command step is concrete. URL/TOKEN angle-brackets in Task 7 are runtime values captured during deploy, not plan placeholders. ✓

**Type consistency:** `_generate_via_modal(prompt,width,height,steps,seed)` and `_generate_via_modal_with_retries(...,attempts,sleep_s,label)` match between Task 2 (def + tests) and Task 3 (call site). `_image_backend_chain` return lists match across Tasks 1/3. Endpoint payload keys (`prompt/width/height/steps/seed`) match between client (Task 2) and `GenReq` (Task 4). Secret key `ZIMAGE_TOKEN` matches between Task 4 (`os.environ`) and Task 7 (`modal secret create`). ✓
