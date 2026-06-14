"""Tests for image-service helpers that don't require the GPU/mflux stack."""

from services.image import _clear_stale_images


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
