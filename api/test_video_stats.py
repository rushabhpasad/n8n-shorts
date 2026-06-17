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


def test_video_period_metrics_parses_rows():
    from services import analytics
    ya = MagicMock()
    ya.reports.return_value.query.return_value.execute.return_value = {
        "rows": [["v1", 55, 9], ["v2", 12, 1]]
    }
    out = analytics.video_period_metrics(
        "wordstrata", "2026-06-01", "2026-06-17", ["v1", "v2"], client=ya)
    assert out["v1"] == {"watch_minutes": 55, "shares": 9}
    assert out["v2"] == {"watch_minutes": 12, "shares": 1}
    _, kwargs = ya.reports.return_value.query.call_args
    assert kwargs["dimensions"] == "video"
    assert kwargs["metrics"] == "estimatedMinutesWatched,shares"
    assert kwargs["filters"] == "video==v1,v2"


def test_video_period_metrics_empty_and_missing_rows():
    from services import analytics
    assert analytics.video_period_metrics("wordstrata", "a", "b", [], client=MagicMock()) == {}
    ya = MagicMock()
    ya.reports.return_value.query.return_value.execute.return_value = {}  # no rows key
    assert analytics.video_period_metrics("wordstrata", "a", "b", ["v1"], client=ya) == {}


def _detail(views, likes, comments, title="t", published="2026-06-10T09:00:00Z"):
    return {"views": views, "likes": likes, "comments": comments,
            "title": title, "published_at": published}


def test_channel_video_stats_first_snapshot_delta_equals_total(monkeypatch, tmp_path):
    import db
    from services import analytics
    monkeypatch.setattr(db.settings, "db_path", tmp_path / "t.db")
    db.init_schema()

    monkeypatch.setattr(analytics.db, "uploaded_video_ids", lambda ch: ["v1"])
    monkeypatch.setattr(analytics, "video_details",
                        lambda ch, ids, *, client=None: {"v1": _detail(180, 15, 3)})
    monkeypatch.setattr(analytics, "video_period_metrics",
                        lambda ch, s, e, ids, *, client=None: {"v1": {"watch_minutes": 55, "shares": 9}})

    cvs = analytics.channel_video_stats(
        "wordstrata", today=date(2026, 6, 17),
        data_client=MagicMock(), analytics_client=MagicMock())

    assert cvs.channel == "wordstrata"
    r = cvs.rows[0]
    assert (r.views_total, r.views_today) == (180, 180)        # first snapshot
    assert (r.watch_min_total, r.watch_min_today) == (55, 55)
    assert (r.shares_total, r.shares_today) == (9, 9)
    assert r.url == "https://www.youtube.com/shorts/v1"
    assert r.days_live == 7                                    # 06-10 -> 06-17
    # snapshot persisted for tomorrow's delta
    assert db.video_snapshot_before("wordstrata", "v1", "2026-06-18")["views"] == 180


def test_channel_video_stats_second_snapshot_computes_delta(monkeypatch, tmp_path):
    import db
    from services import analytics
    monkeypatch.setattr(db.settings, "db_path", tmp_path / "t.db")
    db.init_schema()
    db.record_video_snapshot("wordstrata", "v1", snapshot_date="2026-06-16",
                             views=100, likes=10, comments=2, watch_minutes=30, shares=5)

    monkeypatch.setattr(analytics.db, "uploaded_video_ids", lambda ch: ["v1"])
    monkeypatch.setattr(analytics, "video_details",
                        lambda ch, ids, *, client=None: {"v1": _detail(180, 15, 3)})
    monkeypatch.setattr(analytics, "video_period_metrics",
                        lambda ch, s, e, ids, *, client=None: {"v1": {"watch_minutes": 55, "shares": 9}})

    cvs = analytics.channel_video_stats(
        "wordstrata", today=date(2026, 6, 17),
        data_client=MagicMock(), analytics_client=MagicMock())
    r = cvs.rows[0]
    assert (r.views_total, r.views_today) == (180, 80)     # 180 - 100
    assert (r.likes_total, r.likes_today) == (15, 5)
    assert (r.comments_total, r.comments_today) == (3, 1)
    assert (r.watch_min_total, r.watch_min_today) == (55, 25)
    assert (r.shares_total, r.shares_today) == (9, 4)


def test_channel_video_stats_missing_analytics_defaults_zero(monkeypatch, tmp_path):
    import db
    from services import analytics
    monkeypatch.setattr(db.settings, "db_path", tmp_path / "t.db")
    db.init_schema()
    monkeypatch.setattr(analytics.db, "uploaded_video_ids", lambda ch: ["v1"])
    monkeypatch.setattr(analytics, "video_details",
                        lambda ch, ids, *, client=None: {"v1": _detail(10, 1, 0)})
    monkeypatch.setattr(analytics, "video_period_metrics",
                        lambda ch, s, e, ids, *, client=None: {})  # no analytics row yet

    cvs = analytics.channel_video_stats(
        "wordstrata", today=date(2026, 6, 17),
        data_client=MagicMock(), analytics_client=MagicMock())
    r = cvs.rows[0]
    assert (r.watch_min_total, r.watch_min_today) == (0, 0)
    assert (r.shares_total, r.shares_today) == (0, 0)


def test_channel_video_stats_no_videos_returns_empty(monkeypatch, tmp_path):
    import db
    from services import analytics
    monkeypatch.setattr(db.settings, "db_path", tmp_path / "t.db")
    db.init_schema()
    monkeypatch.setattr(analytics.db, "uploaded_video_ids", lambda ch: [])
    cvs = analytics.channel_video_stats(
        "wordstrata", today=date(2026, 6, 17),
        data_client=MagicMock(), analytics_client=MagicMock())
    assert cvs.channel == "wordstrata" and cvs.rows == []


def test_channel_video_stats_skips_video_absent_from_details(monkeypatch, tmp_path):
    """A video in uploaded_video_ids but missing from the Data API response
    (deleted/private since upload) is dropped — no row, no snapshot persisted."""
    import db
    from services import analytics
    monkeypatch.setattr(db.settings, "db_path", tmp_path / "t.db")
    db.init_schema()
    monkeypatch.setattr(analytics.db, "uploaded_video_ids", lambda ch: ["v1", "gone"])
    monkeypatch.setattr(analytics, "video_details",
                        lambda ch, ids, *, client=None: {"v1": _detail(10, 1, 0)})
    monkeypatch.setattr(analytics, "video_period_metrics",
                        lambda ch, s, e, ids, *, client=None: {})

    cvs = analytics.channel_video_stats(
        "wordstrata", today=date(2026, 6, 17),
        data_client=MagicMock(), analytics_client=MagicMock())
    assert [r.video_id for r in cvs.rows] == ["v1"]                 # "gone" skipped
    assert db.video_snapshot_before("wordstrata", "gone", "2026-06-18") is None


def test_all_video_stats_isolates_channel_failure(monkeypatch):
    from services import analytics
    from models import ChannelVideoStats

    def fake(channel, *, today=None):
        if channel == "bad":
            raise RuntimeError("quota exceeded")
        return ChannelVideoStats(channel=channel, rows=[])

    monkeypatch.setattr(analytics, "channel_video_stats", fake)
    report = analytics.all_video_stats(["good", "bad"], today=date(2026, 6, 17))

    assert report.date == "2026-06-17"
    assert [c.channel for c in report.channels] == ["good"]
    assert len(report.errors) == 1
    assert "bad" in report.errors[0] and "quota" in report.errors[0]
