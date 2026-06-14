"""Tests for the analytics backend: OAuth scopes, db helper, and service rollup.

All Google API calls are mocked — no network, no credentials needed.
"""

from datetime import date
from unittest.mock import MagicMock


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
    # gained, lost, minutes, views, likes, comments, avgDuration, shares, avgViewPct
    client.reports.return_value.query.return_value.execute.return_value = {
        "rows": [[210, 10, 3420, 8000, 450, 96, 41, 120, 58.5]]
    }
    pm = analytics.period_metrics("wordstrata", "2026-05-16", "2026-06-14", client=client)
    assert pm.new_subscribers == 200
    assert pm.estimated_minutes_watched == 3420
    assert pm.average_view_duration_s == 41
    assert pm.shares == 120
    assert pm.average_view_percentage == 58.5
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


def _analytics_mock(period_row, traffic_rows, country_rows=None):
    """A reports() mock that routes by query shape: the country dimension gets
    country rows, other dimensioned queries get the traffic-source rows, and
    plain metric queries get the period row."""
    ya = MagicMock()
    country_rows = country_rows or [["US", 4500], ["IN", 1200]]

    def query(**kwargs):
        q = MagicMock()
        dim = kwargs.get("dimensions")
        if dim == "country":
            q.execute.return_value = {"rows": country_rows}
        elif dim:
            q.execute.return_value = {"rows": traffic_rows}
        else:
            q.execute.return_value = {"rows": [period_row]}
        return q

    ya.reports.return_value.query.side_effect = query
    return ya


def test_channel_analytics_rolls_up(monkeypatch, tmp_path):
    import db
    from services import analytics
    monkeypatch.setattr(db.settings, "db_path", tmp_path / "t.db")
    db.init_schema()

    data = MagicMock()
    data.channels.return_value.list.return_value.execute.return_value = {
        "items": [{"statistics": {
            "subscriberCount": "1204", "viewCount": "55000", "videoCount": "32"}}]
    }
    data.videos.return_value.list.return_value.execute.return_value = {
        "items": [
            {"id": "v1", "statistics": {"viewCount": "100", "likeCount": "14", "commentCount": "4"}},
            {"id": "v2", "statistics": {"viewCount": "40", "likeCount": "6", "commentCount": "2"}},
        ]
    }
    ya = _analytics_mock(
        period_row=[210, 10, 3420, 8000, 450, 96, 41, 120, 58.5],
        traffic_rows=[["SHORTS", 6400], ["YT_SEARCH", 1600]],
    )
    monkeypatch.setattr(analytics.db, "uploaded_video_ids", lambda ch: ["v1", "v2"])

    ca = analytics.channel_analytics(
        "wordstrata", days=30, today=date(2026, 6, 14),
        data_client=data, analytics_client=ya,
    )
    assert ca.snapshot.subscribers == 1204
    assert ca.videos_uploaded == 2
    assert ca.avg_likes_per_video == 10.0     # (14+6)/2
    assert ca.avg_comments_per_video == 3.0   # (4+2)/2
    assert ca.period.new_subscribers == 200
    assert ca.new_subs_1d == 200              # 1-day query reuses mocked rows
    assert ca.period.days == 30
    # enriched signals
    assert ca.shorts_feed_share == 0.8        # 6400 / 8000
    assert [c.country for c in ca.top_countries] == ["US", "IN"]
    assert ca.top_video.video_id == "v1"      # 100 > 40 lifetime views
    assert ca.ypp.subscribers == 1204
    # first run for this channel → no prior snapshot → no trend / milestones
    assert ca.trend is None
    assert ca.milestones == []
    # snapshot persisted for the next run's trend baseline
    assert db.snapshot_before("wordstrata", "2026-06-15")["subscribers"] == 1204


def test_channel_analytics_empty_channel(monkeypatch, tmp_path):
    import db
    from services import analytics
    monkeypatch.setattr(db.settings, "db_path", tmp_path / "t.db")
    db.init_schema()

    data = MagicMock()
    data.channels.return_value.list.return_value.execute.return_value = {
        "items": [{"statistics": {"subscriberCount": "0", "viewCount": "0", "videoCount": "0"}}]
    }
    ya = MagicMock()
    ya.reports.return_value.query.return_value.execute.return_value = {}
    monkeypatch.setattr(analytics.db, "uploaded_video_ids", lambda ch: [])

    ca = analytics.channel_analytics(
        "wordstrata", days=30, today=date(2026, 6, 14),
        data_client=data, analytics_client=ya,
    )
    assert ca.videos_uploaded == 0
    assert ca.avg_likes_per_video == 0.0
    assert ca.videos == []


def _minimal_channel_analytics(channel):
    from models import ChannelAnalytics, ChannelSnapshot, PeriodMetrics
    return ChannelAnalytics(
        channel=channel,
        snapshot=ChannelSnapshot(subscribers=1, total_views=1, video_count=1),
        new_subs_1d=0,
        period=PeriodMetrics(
            days=30, subscribers_gained=0, subscribers_lost=0, new_subscribers=0,
            estimated_minutes_watched=0, views=0, likes=0, comments=0,
            average_view_duration_s=0,
        ),
        videos_uploaded=0, avg_likes_per_video=0.0, avg_comments_per_video=0.0,
        videos=[],
    )


def test_build_daily_report_isolates_channel_failure(monkeypatch):
    from services import analytics

    def fake(channel, days, *, today=None):
        if channel == "bad":
            raise RuntimeError("quota exceeded")
        return _minimal_channel_analytics(channel)

    monkeypatch.setattr(analytics, "channel_analytics", fake)
    report = analytics.build_daily_report(
        ["good", "bad"], days=30, today=date(2026, 6, 14))

    assert report.date == "2026-06-14"
    assert report.days == 30
    assert [c.channel for c in report.channels] == ["good"]
    assert len(report.errors) == 1
    assert "bad" in report.errors[0] and "quota" in report.errors[0]


def test_analytics_daily_endpoint(monkeypatch):
    from fastapi.testclient import TestClient
    import main
    from models import DailyAnalyticsReport
    from services import analytics

    monkeypatch.setattr(
        analytics, "build_daily_report",
        lambda channels, days=30: DailyAnalyticsReport(
            date="2026-06-14", days=days, channels=[], errors=[]),
    )
    client = TestClient(main.app)
    resp = client.get("/analytics/daily?days=30")
    assert resp.status_code == 200
    assert resp.json()["date"] == "2026-06-14"


def test_analytics_daily_digest_endpoint_returns_text(monkeypatch):
    from fastapi.testclient import TestClient
    import main
    from models import DailyAnalyticsReport
    from services import analytics

    monkeypatch.setattr(
        analytics, "build_daily_report",
        lambda channels, days=30: DailyAnalyticsReport(
            date="2026-06-14", days=days, channels=[], errors=["x: boom"]),
    )
    client = TestClient(main.app)
    resp = client.get("/analytics/daily/digest?days=30")
    assert resp.status_code == 200
    body = resp.json()
    assert "📊 Daily Analytics — 2026-06-14" in body["text"]
    assert "boom" in body["text"]


def test_analytics_channel_unknown_returns_404():
    import main
    from fastapi.testclient import TestClient

    client = TestClient(main.app)
    resp = client.get("/definitely-not-a-channel/analytics")
    assert resp.status_code == 404
