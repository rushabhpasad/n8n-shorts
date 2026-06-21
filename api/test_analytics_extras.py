"""Tests for the enriched analytics digest (items 1-10).

Covers the new persistence helpers (daily snapshots, queue depth, recent
uploads) plus the pure analytics helpers (traffic-source share, top short,
YPP progress, milestones, anomaly alerts) and the backend digest renderer.

All Google API calls are mocked; SQLite uses a tmp db. No network, no creds.
"""

from datetime import date
from unittest.mock import MagicMock


# ─── DB: daily snapshots, queue depth, recent uploads ───────────────────────

def _insert_word(c, channel, wid, word, status="pending"):
    c.execute(
        "INSERT INTO words (channel, id, word, category, origin_language, hook, status) "
        "VALUES (?, ?, ?, 'n', 'n', 'h', ?)",
        (channel, wid, word, status),
    )


def test_record_and_read_prior_snapshot(monkeypatch, tmp_path):
    import db
    monkeypatch.setattr(db.settings, "db_path", tmp_path / "t.db")
    db.init_schema()

    db.record_analytics_snapshot(
        "wordstrata", snapshot_date="2026-06-13", subscribers=1000,
        total_views=50000, views_period=4000, watch_minutes_period=3000,
        shares_period=20,
    )
    db.record_analytics_snapshot(
        "wordstrata", snapshot_date="2026-06-14", subscribers=1010,
        total_views=51000, views_period=4200, watch_minutes_period=3100,
        shares_period=25,
    )

    # Prior snapshot for the 14th is the 13th's row (strictly earlier date).
    prior = db.snapshot_before("wordstrata", "2026-06-14")
    assert prior["subscribers"] == 1000
    assert prior["total_views"] == 50000
    # No earlier snapshot than the first one.
    assert db.snapshot_before("wordstrata", "2026-06-13") is None
    # Unknown channel → None.
    assert db.snapshot_before("the-mythscape", "2026-06-14") is None


def test_record_snapshot_is_idempotent_per_day(monkeypatch, tmp_path):
    import db
    monkeypatch.setattr(db.settings, "db_path", tmp_path / "t.db")
    db.init_schema()
    db.record_analytics_snapshot(
        "wordstrata", snapshot_date="2026-06-14", subscribers=10,
        total_views=1, views_period=1, watch_minutes_period=1, shares_period=1,
    )
    # Re-running the same day overwrites rather than duplicating.
    db.record_analytics_snapshot(
        "wordstrata", snapshot_date="2026-06-14", subscribers=99,
        total_views=2, views_period=2, watch_minutes_period=2, shares_period=2,
    )
    with db.conn() as c:
        (n,) = c.execute(
            "SELECT COUNT(*) FROM analytics_snapshots WHERE channel='wordstrata'"
        ).fetchone()
    assert n == 1


def test_pending_word_count(monkeypatch, tmp_path):
    import db
    monkeypatch.setattr(db.settings, "db_path", tmp_path / "t.db")
    db.init_schema()
    with db.conn() as c:
        _insert_word(c, "wordstrata", 1, "a", status="pending")
        _insert_word(c, "wordstrata", 2, "b", status="pending")
        _insert_word(c, "wordstrata", 3, "c", status="done")
        _insert_word(c, "the-mythscape", 1, "m", status="pending")
    assert db.pending_word_count("wordstrata") == 2
    assert db.pending_word_count("the-mythscape") == 1
    assert db.pending_word_count("open-verdicts") == 0


def test_uploads_since_counts_done_runs_after_cutoff(monkeypatch, tmp_path):
    import db
    monkeypatch.setattr(db.settings, "db_path", tmp_path / "t.db")
    db.init_schema()
    with db.conn() as c:
        _insert_word(c, "wordstrata", 1, "a")
        c.execute(
            "INSERT INTO runs (channel, word_id, status, started_at, youtube_video_id) "
            "VALUES ('wordstrata', 1, 'done', '2026-06-14 08:00:00', 'v1')"
        )
        c.execute(
            "INSERT INTO runs (channel, word_id, status, started_at, youtube_video_id) "
            "VALUES ('wordstrata', 1, 'done', '2026-06-10 08:00:00', 'v0')"
        )
    # Only the run on/after the cutoff counts.
    assert db.uploads_since("wordstrata", "2026-06-13 00:00:00") == 1
    assert db.uploads_since("wordstrata", "2026-06-01 00:00:00") == 2


# ─── Pure analytics helpers ─────────────────────────────────────────────────

def test_traffic_sources_parses_dimension_rows():
    from services import analytics
    client = MagicMock()
    # reports.query with insightTrafficSourceType dim → [source, views] rows.
    client.reports.return_value.query.return_value.execute.return_value = {
        "rows": [["SHORTS", 8000], ["YT_SEARCH", 1500], ["SUBSCRIBER", 500]]
    }
    sources = analytics.traffic_sources("wordstrata", "2026-05-16", "2026-06-14", client=client)
    assert sources == {"SHORTS": 8000, "YT_SEARCH": 1500, "SUBSCRIBER": 500}


def test_shorts_feed_share_computes_fraction():
    from services import analytics
    sources = {"SHORTS": 8000, "YT_SEARCH": 1500, "SUBSCRIBER": 500}
    assert analytics.shorts_feed_share(sources) == 0.8     # 8000 / 10000
    assert analytics.shorts_feed_share({}) == 0.0          # no traffic → 0


def test_top_countries_parses_and_caps_to_three():
    from services import analytics
    client = MagicMock()
    client.reports.return_value.query.return_value.execute.return_value = {
        "rows": [["US", 4500], ["IN", 1200], ["GB", 800], ["CA", 300]]
    }
    countries = analytics.top_countries(
        "wordstrata", "2026-05-16", "2026-06-14", client=client, limit=3)
    assert [c.country for c in countries] == ["US", "IN", "GB"]
    assert countries[0].views == 4500


def test_top_video_picks_highest_views():
    from services import analytics
    from models import VideoAnalytics
    videos = [
        VideoAnalytics(video_id="v1", views=100, likes=14, comments=4),
        VideoAnalytics(video_id="v2", views=4200, likes=300, comments=40),
        VideoAnalytics(video_id="v3", views=40, likes=6, comments=2),
    ]
    top = analytics.top_video(videos)
    assert top.video_id == "v2"
    assert top.views == 4200
    assert top.url == "https://www.youtube.com/shorts/v2"
    # Empty list → no top video.
    assert analytics.top_video([]) is None


def test_ypp_progress_below_threshold():
    from services import analytics
    ypp = analytics.ypp_progress(subscribers=620, shorts_views_90d=1_800_000)
    assert ypp.subscribers == 620
    assert ypp.subs_target == 1000
    assert round(ypp.subs_progress, 2) == 0.62
    assert ypp.shorts_views_target == 10_000_000
    assert round(ypp.shorts_views_progress, 2) == 0.18
    assert ypp.eligible is False


def test_ypp_progress_eligible_when_both_met():
    from services import analytics
    ypp = analytics.ypp_progress(subscribers=1500, shorts_views_90d=12_000_000)
    assert ypp.subs_progress == 1.0          # capped at 1.0
    assert ypp.shorts_views_progress == 1.0  # capped at 1.0
    assert ypp.eligible is True


def test_compute_milestones_fires_only_on_crossing():
    from services import analytics
    # Subs crossed 1000; views crossed nothing new.
    crossed = analytics.compute_milestones(
        prev_subscribers=980, subscribers=1010,
        prev_total_views=49000, total_views=51000,
    )
    assert any("1,000 subscribers" in m for m in crossed)
    # No prior snapshot (first run) → no milestones (can't know it's a crossing).
    assert analytics.compute_milestones(
        prev_subscribers=None, subscribers=1010,
        prev_total_views=None, total_views=51000,
    ) == []
    # No threshold crossed → empty.
    assert analytics.compute_milestones(
        prev_subscribers=1010, subscribers=1020,
        prev_total_views=51000, total_views=51500,
    ) == []


def test_detect_alerts_flags_view_spike_and_no_uploads():
    from services import analytics
    # views_1d 2.5x the daily average over the period → spike.
    alerts = analytics.detect_alerts(
        views_1d=250, period_views=3000, period_days=30, uploads_24h=0,
    )
    assert any("🔥" in a for a in alerts)     # 250 vs avg 100 → ≥2x
    assert any("⚠️" in a and "upload" in a.lower() for a in alerts)


def test_detect_alerts_quiet_when_nominal():
    from services import analytics
    alerts = analytics.detect_alerts(
        views_1d=110, period_views=3000, period_days=30, uploads_24h=1,
    )
    assert alerts == []


# ─── Digest renderer ────────────────────────────────────────────────────────

def _rich_channel(channel="wordstrata"):
    from models import (
        ChannelAnalytics, ChannelSnapshot, CountryViews, PeriodMetrics,
        TopVideo, TrendDelta, YppProgress,
    )
    return ChannelAnalytics(
        channel=channel,
        snapshot=ChannelSnapshot(subscribers=1204, total_views=55000, video_count=32),
        new_subs_1d=8,
        period=PeriodMetrics(
            days=30, subscribers_gained=220, subscribers_lost=10, new_subscribers=210,
            estimated_minutes_watched=3420, views=8000, likes=450, comments=96,
            average_view_duration_s=41, shares=120, average_view_percentage=58.5,
        ),
        videos_uploaded=32, avg_likes_per_video=14.0, avg_comments_per_video=3.0,
        videos=[],
        traffic_sources={"SHORTS": 6400, "YT_SEARCH": 1600},
        shorts_feed_share=0.8,
        top_countries=[CountryViews(country="US", views=4500),
                       CountryViews(country="IN", views=1200)],
        top_video=TopVideo(video_id="vX", url="https://www.youtube.com/shorts/vX",
                           views=4200, likes=300, comments=40),
        ypp=YppProgress(subscribers=1204, subs_target=1000, subs_progress=1.0,
                        shorts_views_90d=2_500_000, shorts_views_target=10_000_000,
                        shorts_views_progress=0.25, eligible=False),
        views_90d=2_500_000,
        queue_pending=388, uploads_24h=1,
        trend=TrendDelta(compared_days=7, subscribers=56, total_views=4200, period_views=900),
        milestones=["🎉 crossed 1,000 subscribers"],
        alerts=["🔥 views 2.3× daily average"],
    )


def test_render_digest_includes_new_signals():
    from models import DailyAnalyticsReport
    from services import analytics
    report = DailyAnalyticsReport(
        date="2026-06-14", days=30, channels=[_rich_channel()], errors=[],
    )
    text = analytics.render_digest(report)
    assert "2026-06-14" in text
    assert "🔵" in text and "Wordstrata" in text          # channel color/name
    assert "80%" in text                                   # shorts feed share
    assert "120" in text                                   # shares
    assert "🏆" in text                                    # top short marker
    assert "Geo:" in text and "US" in text                 # top geography
    assert "388" in text                                   # queue depth
    assert "🎉 crossed 1,000 subscribers" in text          # milestone
    assert "🔥" in text                                    # anomaly alert
    assert "YPP" in text or "Monetization" in text         # ypp progress


def test_build_daily_report_embeds_digest_text(monkeypatch):
    """The structured report carries the rendered digest so n8n needs ONE call:
    the Sheets path reads channels[], the chat path reads digest_text."""
    from datetime import date
    from services import analytics

    def fake(channel, days, *, today=None):
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

    monkeypatch.setattr(analytics, "channel_analytics", fake)
    report = analytics.build_daily_report(["wordstrata"], days=30, today=date(2026, 6, 14))
    assert report.digest_text.startswith("📊 Daily Analytics — 2026-06-14")
    assert "Wordstrata" in report.digest_text
    # and it equals what render_digest would produce from the same report
    assert report.digest_text == analytics.render_digest(report)


def test_render_digest_lists_errors_block():
    from models import DailyAnalyticsReport
    from services import analytics
    report = DailyAnalyticsReport(
        date="2026-06-14", days=30, channels=[],
        errors=["bright-beasts: RuntimeError: quota exceeded"],
    )
    text = analytics.render_digest(report)
    assert "⚠️" in text
    assert "quota exceeded" in text


# ─── YouTube count revision: negative deltas are legitimate, not bugs ────────

def test_trend_delta_is_negative_on_youtube_subscriber_revision(monkeypatch, tmp_path):
    """YouTube removes bot subs during audits → subscribers can drop day-over-day.
    _trend_from_snapshot must yield a negative delta, NOT clamp to 0."""
    import db
    from services import analytics

    monkeypatch.setattr(db.settings, "db_path", tmp_path / "t.db")
    db.init_schema()
    # Record a prior snapshot with higher subscriber and view counts.
    db.record_analytics_snapshot(
        "wordstrata", snapshot_date="2026-06-13",
        subscribers=1010, total_views=52000,
        views_period=4200, watch_minutes_period=3100, shares_period=25,
    )
    prior = db.snapshot_before("wordstrata", "2026-06-14")

    from models import ChannelSnapshot, PeriodMetrics
    # Today YouTube reports FEWER subscribers (bot purge) and FEWER views
    # (spam-view audit) than the prior snapshot.
    snapshot = ChannelSnapshot(subscribers=1005, total_views=51800, video_count=32)
    period = PeriodMetrics(
        days=1, subscribers_gained=0, subscribers_lost=0, new_subscribers=0,
        estimated_minutes_watched=0, views=4000, likes=0, comments=0,
        average_view_duration_s=0,
    )

    trend = analytics._trend_from_snapshot(prior, snapshot, period, date(2026, 6, 14))

    assert trend is not None
    # Both deltas are negative — legitimate YouTube count revision, not a bug.
    assert trend.subscribers == -5, (
        "subscriber delta must be negative on a downward revision; "
        "clamping to 0 would mask real corrections"
    )
    assert trend.total_views == -200, (
        "total_views delta must be negative on a downward revision; "
        "clamping would corrupt the analytics"
    )


def test_trend_delta_negative_period_views_on_revision(monkeypatch, tmp_path):
    """period_views can also decrease if YouTube re-audits the window."""
    import db
    from services import analytics

    monkeypatch.setattr(db.settings, "db_path", tmp_path / "t.db")
    db.init_schema()
    db.record_analytics_snapshot(
        "wordstrata", snapshot_date="2026-06-13",
        subscribers=1000, total_views=50000,
        views_period=4500, watch_minutes_period=3000, shares_period=20,
    )
    prior = db.snapshot_before("wordstrata", "2026-06-14")

    from models import ChannelSnapshot, PeriodMetrics
    snapshot = ChannelSnapshot(subscribers=1000, total_views=50000, video_count=30)
    period = PeriodMetrics(
        days=1, subscribers_gained=0, subscribers_lost=0, new_subscribers=0,
        estimated_minutes_watched=0, views=4200, likes=0, comments=0,
        average_view_duration_s=0,
    )

    trend = analytics._trend_from_snapshot(prior, snapshot, period, date(2026, 6, 14))

    assert trend is not None
    assert trend.period_views == -300, (
        "period_views delta must pass through as negative; "
        "YouTube revision deltas must never be clamped"
    )


def test_video_cumulative_total_decreases_preserved(monkeypatch, tmp_path):
    """Per-video *_total fields come straight from the YouTube Data API.
    If YouTube revises a video's view count down (spam removal), views_total
    must reflect the lower value — it must NOT be floored to the prior snapshot.
    """
    import db
    from services import analytics

    monkeypatch.setattr(db.settings, "db_path", tmp_path / "t.db")
    db.init_schema()
    # Yesterday's snapshot had higher counts.
    db.record_video_snapshot(
        "wordstrata", "v1", snapshot_date="2026-06-16",
        views=845, likes=20, comments=5, watch_minutes=120, shares=8,
    )

    monkeypatch.setattr(analytics.db, "uploaded_video_ids", lambda ch: ["v1"])
    # Today YouTube reports lower views (834) and lower likes (18) — a revision.
    monkeypatch.setattr(
        analytics, "video_details",
        lambda ch, ids, *, client=None: {
            "v1": {
                "views": 834, "likes": 18, "comments": 5,
                "title": "Etymology of OK", "published_at": "2026-06-10T09:00:00Z",
            }
        },
    )
    monkeypatch.setattr(
        analytics, "video_period_metrics",
        lambda ch, s, e, ids, *, client=None: {"v1": {"watch_minutes": 120, "shares": 8}},
    )

    cvs = analytics.channel_video_stats(
        "wordstrata", today=date(2026, 6, 17),
        data_client=MagicMock(), analytics_client=MagicMock(),
    )

    r = cvs.rows[0]
    # Cumulative totals must reflect the revised (lower) YouTube figure,
    # not be clamped/floored to the prior snapshot.
    assert r.views_total == 834, (
        "views_total must be the current YouTube count even when it decreased; "
        "flooring to prior would corrupt the analytics"
    )
    assert r.likes_total == 18, (
        "likes_total must reflect the YouTube revision downward"
    )
