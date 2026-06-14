"""Tests for the analytics backend: OAuth scopes, db helper, and service rollup.

All Google API calls are mocked — no network, no credentials needed.
"""

from datetime import date
from unittest.mock import MagicMock

import pytest


def test_oauth_scopes_include_analytics_read():
    from services import youtube
    assert "https://www.googleapis.com/auth/youtube.upload" in youtube.SCOPES
    assert "https://www.googleapis.com/auth/yt-analytics.readonly" in youtube.SCOPES
    assert "https://www.googleapis.com/auth/youtube.readonly" in youtube.SCOPES
    assert callable(youtube.credentials)


def _insert_word(c, channel, wid, word):
    c.execute(
        "INSERT INTO words (channel, id, word, category, origin_language, hook) "
        "VALUES (?, ?, ?, 'n', 'n', 'h')",
        (channel, wid, word),
    )


def test_uploaded_video_ids_filters_to_done_with_video(monkeypatch, tmp_path):
    import db
    monkeypatch.setattr(db.settings, "db_path", tmp_path / "t.db")
    db.init_schema()
    with db.conn() as c:
        _insert_word(c, "wordstrata", 1, "a")
        _insert_word(c, "wordstrata", 2, "b")
        _insert_word(c, "the-mythscape", 1, "m")
    db.record_completed_run("wordstrata", 1, youtube_video_id="vidA", youtube_url="u")
    db.record_completed_run("wordstrata", 2)  # no video id → excluded
    db.record_completed_run("the-mythscape", 1, youtube_video_id="vidM", youtube_url="u")

    assert db.uploaded_video_ids("wordstrata") == ["vidA"]
    assert db.uploaded_video_ids("the-mythscape") == ["vidM"]
    assert db.uploaded_video_ids("open-verdicts") == []
