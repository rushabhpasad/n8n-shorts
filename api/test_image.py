"""Tests for image-service helpers that don't require the GPU/mflux stack."""

import services.image as image
from services.image import (
    _clear_stale_images,
    _generate_via_space,
    _generate_via_space_with_retries,
    _image_bytes_from_result,
)


def test_clear_stale_images_removes_only_target_word(tmp_path):
    for i in range(3):
        (tmp_path / f"word_0004_{i}.png").write_bytes(b"x")
    for i in range(2):
        (tmp_path / f"word_0005_{i}.png").write_bytes(b"x")
    keep = tmp_path / "word_0004_notes.txt"  # non-PNG, must survive
    keep.write_text("keep me")

    removed = _clear_stale_images(tmp_path, 4)

    assert removed == 3
    assert list(tmp_path.glob("word_0004_*.png")) == []
    assert len(list(tmp_path.glob("word_0005_*.png"))) == 2
    assert keep.exists()


def test_clear_stale_images_when_none_present(tmp_path):
    assert _clear_stale_images(tmp_path, 9) == 0


def test_image_bytes_from_result_local_path_string(tmp_path):
    png = tmp_path / "out.png"
    png.write_bytes(b"PNGDATA")
    # gradio_client downloads file outputs and returns (local_path, seed)
    assert _image_bytes_from_result((str(png), 42)) == b"PNGDATA"


def test_image_bytes_from_result_filedata_dict(tmp_path):
    png = tmp_path / "out.png"
    png.write_bytes(b"DICTPNG")
    result = ({"path": str(png), "url": "https://x/y.png"}, 42)
    assert _image_bytes_from_result(result) == b"DICTPNG"


def test_image_bytes_from_result_raises_when_empty():
    import pytest

    with pytest.raises(RuntimeError):
        _image_bytes_from_result(("", 42))


def test_generate_via_space_passes_height_width_order(tmp_path, monkeypatch):
    png = tmp_path / "img.png"
    png.write_bytes(b"OK")
    calls = {}

    class FakeClient:
        def predict(self, *args, **kwargs):
            calls["args"] = args
            calls["kwargs"] = kwargs
            return (str(png), 7)

    monkeypatch.setattr(image, "_get_space_client", lambda: FakeClient())

    # caller passes (prompt, width, height, steps, seed); the Space expects
    # height BEFORE width, so the predict args must be reordered.
    out = _generate_via_space("a prompt", 768, 1344, 8, 7)

    assert out == b"OK"
    assert calls["args"] == ("a prompt", 1344, 768, 8, 7, False)
    assert calls["kwargs"] == {"api_name": "/generate_image"}


def test_space_retries_succeeds_after_transient_failure(monkeypatch):
    attempts = {"n": 0}
    slept = []

    def flaky(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("ZeroGPU busy")
        return b"PNG"

    monkeypatch.setattr(image, "_generate_via_space", flaky)
    monkeypatch.setattr(image.time, "sleep", lambda s: slept.append(s))

    out = _generate_via_space_with_retries("p", 768, 1344, 8, 7, attempts=3, sleep_s=30.0)

    assert out == b"PNG"
    assert attempts["n"] == 3
    assert slept == [30.0, 30.0]  # one sleep before each retry, none after success


def test_space_retries_raises_after_exhausting_attempts(monkeypatch):
    import pytest

    attempts = {"n": 0}
    slept = []

    def always_fails(*args, **kwargs):
        attempts["n"] += 1
        raise RuntimeError("network down")

    monkeypatch.setattr(image, "_generate_via_space", always_fails)
    monkeypatch.setattr(image.time, "sleep", lambda s: slept.append(s))

    with pytest.raises(RuntimeError, match="network down"):
        _generate_via_space_with_retries("p", 768, 1344, 8, 7, attempts=3, sleep_s=30.0)

    assert attempts["n"] == 3
    assert slept == [30.0, 30.0]  # sleeps between tries, but not after the final failure


def test_space_quota_error_skips_retries(monkeypatch):
    import pytest

    attempts = {"n": 0}
    slept = []

    def quota_exhausted(*args, **kwargs):
        attempts["n"] += 1
        raise RuntimeError(
            "You have exceeded your free ZeroGPU quota (90s requested vs. 0s left). "
            "Try again in 0:00:00"
        )

    monkeypatch.setattr(image, "_generate_via_space", quota_exhausted)
    monkeypatch.setattr(image.time, "sleep", lambda s: slept.append(s))

    with pytest.raises(RuntimeError, match="ZeroGPU quota"):
        _generate_via_space_with_retries("p", 768, 1344, 8, 7, attempts=3, sleep_s=30.0)

    assert attempts["n"] == 1   # no retries on a quota rejection
    assert slept == []          # and no wasted sleep before fallback


def test_image_backend_chain_modal():
    from services.image import _image_backend_chain

    assert _image_backend_chain("modal") == ["modal", "space", "mflux"]


def test_image_backend_chain_space_unchanged():
    from services.image import _image_backend_chain

    assert _image_backend_chain("space") == ["space", "mflux"]


def test_image_backend_chain_mflux_only():
    from services.image import _image_backend_chain

    assert _image_backend_chain("mflux") == ["mflux"]


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
