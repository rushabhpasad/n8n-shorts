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
    .apt_install("git")  # needed to pip-install diffusers from its git URL
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
