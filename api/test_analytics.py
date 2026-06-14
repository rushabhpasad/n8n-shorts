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


def test_channel_snapshot_parses_statistics():
    from services import analytics
    client = MagicMock()
    client.channels.return_value.list.return_value.execute.return_value = {
        "items": [{"statistics": {
            "subscriberCount": "1204", "viewCount": "55000", "videoCount": "32"}}]
    }
    snap = analytics.channel_snapshot("wordstrata", client=client)
    assert (snap.subscribers, snap.total_views, snap.video_count) == (1204, 55000, 32)


def test_period_metrics_computes_new_subscribers_and_days():
    from services import analytics
    client = MagicMock()
    # column order MUST match analytics._PERIOD_METRICS:
    # gained, lost, minutes, views, likes, comments, avgDuration
    client.reports.return_value.query.return_value.execute.return_value = {
        "rows": [[210, 10, 3420, 8000, 450, 96, 41]]
    }
    pm = analytics.period_metrics("wordstrata", "2026-05-16", "2026-06-14", client=client)
    assert pm.new_subscribers == 200
    assert pm.estimated_minutes_watched == 3420
    assert pm.average_view_duration_s == 41
    assert pm.days == 30


def test_period_metrics_handles_empty_rows():
    from services import analytics
    client = MagicMock()
    client.reports.return_value.query.return_value.execute.return_value = {}
    pm = analytics.period_metrics("wordstrata", "2026-06-14", "2026-06-14", client=client)
    assert pm.new_subscribers == 0
    assert pm.estimated_minutes_watched == 0


def test_per_video_empty_returns_empty():
    from services import analytics
    assert analytics.per_video("wordstrata", [], client=MagicMock()) == []


def test_per_video_parses_statistics():
    from services import analytics
    client = MagicMock()
    client.videos.return_value.list.return_value.execute.return_value = {
        "items": [
            {"id": "v1", "statistics": {"viewCount": "100", "likeCount": "14", "commentCount": "4"}},
            {"id": "v2", "statistics": {"viewCount": "40", "likeCount": "6", "commentCount": "2"}},
        ]
    }
    vids = analytics.per_video("wordstrata", ["v1", "v2"], client=client)
    assert [v.video_id for v in vids] == ["v1", "v2"]
    assert (vids[0].likes, vids[0].comments, vids[0].views) == (14, 4, 100)
