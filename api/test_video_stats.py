"""Tests for per-video daily stats: schema, db helpers, service rollup, endpoints.

All Google API calls are mocked — no network, no credentials needed.
"""

from datetime import date
from unittest.mock import MagicMock


def test_video_snapshots_table_exists_and_is_insertable(monkeypatch, tmp_path):
    import db
    monkeypatch.setattr(db.settings, "db_path", tmp_path / "t.db")
    db.init_schema()
    with db.conn() as c:
        c.execute(
            "INSERT INTO video_snapshots "
            "(channel, video_id, date, views, likes, comments, watch_minutes, shares) "
            "VALUES ('wordstrata', 'v1', '2026-06-17', 100, 14, 4, 33, 7)"
        )
        row = c.execute(
            "SELECT views, likes, comments, watch_minutes, shares "
            "FROM video_snapshots WHERE channel='wordstrata' AND video_id='v1' AND date='2026-06-17'"
        ).fetchone()
    assert (row["views"], row["likes"], row["comments"], row["watch_minutes"], row["shares"]) == (100, 14, 4, 33, 7)


def test_record_and_read_video_snapshot_before(monkeypatch, tmp_path):
    import db
    monkeypatch.setattr(db.settings, "db_path", tmp_path / "t.db")
    db.init_schema()

    db.record_video_snapshot(
        "wordstrata", "v1", snapshot_date="2026-06-15",
        views=100, likes=10, comments=2, watch_minutes=30, shares=5,
    )
    db.record_video_snapshot(
        "wordstrata", "v1", snapshot_date="2026-06-16",
        views=180, likes=15, comments=3, watch_minutes=55, shares=9,
    )

    # most recent row strictly before the given date
    prior = db.video_snapshot_before("wordstrata", "v1", "2026-06-17")
    assert (prior["date"], prior["views"], prior["shares"]) == ("2026-06-16", 180, 9)

    prior2 = db.video_snapshot_before("wordstrata", "v1", "2026-06-16")
    assert prior2["views"] == 100  # 06-15 row

    # no prior snapshot → None
    assert db.video_snapshot_before("wordstrata", "v1", "2026-06-15") is None
    assert db.video_snapshot_before("wordstrata", "v1", "2026-06-17") is not None
    assert db.video_snapshot_before("the-mythscape", "v1", "2026-06-17") is None  # channel-scoped


def test_record_video_snapshot_is_idempotent_per_day(monkeypatch, tmp_path):
    import db
    monkeypatch.setattr(db.settings, "db_path", tmp_path / "t.db")
    db.init_schema()
    db.record_video_snapshot("wordstrata", "v1", snapshot_date="2026-06-16",
                             views=100, likes=1, comments=1, watch_minutes=1, shares=1)
    db.record_video_snapshot("wordstrata", "v1", snapshot_date="2026-06-16",
                             views=200, likes=2, comments=2, watch_minutes=2, shares=2)
    with db.conn() as c:
        rows = c.execute(
            "SELECT views FROM video_snapshots WHERE channel='wordstrata' AND video_id='v1'"
        ).fetchall()
    assert len(rows) == 1 and rows[0]["views"] == 200  # upsert, latest wins


def test_video_stat_row_and_report_models():
    from models import VideoStatRow, ChannelVideoStats, VideoStatsReport
    row = VideoStatRow(
        date="2026-06-17", video_id="v1",
        url="https://www.youtube.com/shorts/v1", title="Etymology of OK",
        published_at="2026-06-10T09:00:00Z", days_live=7,
        views_total=180, views_today=80, likes_total=15, likes_today=5,
        comments_total=3, comments_today=1, watch_min_total=55, watch_min_today=25,
        shares_total=9, shares_today=4,
    )
    report = VideoStatsReport(
        date="2026-06-17",
        channels=[ChannelVideoStats(channel="wordstrata", rows=[row])],
        errors=[],
    )
    assert report.channels[0].rows[0].views_today == 80
    assert report.channels[0].channel == "wordstrata"
    # defaults
    assert VideoStatsReport(date="2026-06-17", channels=[]).errors == []


def test_video_details_parses_snippet_and_statistics():
    from services import analytics
    client = MagicMock()
    client.videos.return_value.list.return_value.execute.return_value = {
        "items": [
            {"id": "v1",
             "snippet": {"title": "Etymology of OK", "publishedAt": "2026-06-10T09:00:00Z"},
             "statistics": {"viewCount": "180", "likeCount": "15", "commentCount": "3"}},
            {"id": "v2",
             "snippet": {"title": "Origin of Salary", "publishedAt": "2026-06-11T09:00:00Z"},
             "statistics": {"viewCount": "40", "likeCount": "6", "commentCount": "2"}},
        ]
    }
    out = analytics.video_details("wordstrata", ["v1", "v2"], client=client)
    assert out["v1"] == {"views": 180, "likes": 15, "comments": 3,
                         "title": "Etymology of OK", "published_at": "2026-06-10T09:00:00Z"}
    assert out["v2"]["views"] == 40
    # part must request snippet AND statistics
    _, kwargs = client.videos.return_value.list.call_args
    assert "snippet" in kwargs["part"] and "statistics" in kwargs["part"]


def test_video_details_empty_returns_empty():
    from services import analytics
    assert analytics.video_details("wordstrata", [], client=MagicMock()) == {}
