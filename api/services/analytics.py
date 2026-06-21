"""YouTube analytics for the shorts pipeline.

Two Google APIs per channel, built from the channel's own OAuth credentials:
  - Data API v3         → lifetime snapshot + per-video cumulative stats
  - Analytics API v2    → time-ranged metrics (watch time, subs gained, ...)

Each public function accepts an optional pre-built `client` so the network
boundary can be mocked in tests.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from googleapiclient.discovery import build

import db
from models import (
    ChannelAnalytics,
    ChannelSnapshot,
    ChannelVideoStats,
    CountryViews,
    DailyAnalyticsReport,
    PeriodMetrics,
    TopVideo,
    TrendDelta,
    VideoAnalytics,
    VideoStatRow,
    VideoStatsReport,
    YppProgress,
)
from services.youtube import credentials

log = logging.getLogger("shorts-api.analytics")

# Column order returned by reports.query — keep parsing in lockstep.
_PERIOD_METRICS = (
    "subscribersGained,subscribersLost,estimatedMinutesWatched,"
    "views,likes,comments,averageViewDuration,shares,averageViewPercentage"
)
_VIDEOS_BATCH = 50  # Data API videos.list id cap
_VIDEO_ANALYTICS_BATCH = 200  # video== filter id cap is 500; 200 keeps rows == ids

# Per-video Analytics column order (after the `video` dimension key) — keep
# parsing in lockstep with the row indexing in video_period_metrics.
_VIDEO_METRICS = "estimatedMinutesWatched,shares"

# (#2) The insightTrafficSourceType value for the Shorts feed.
_SHORTS_TRAFFIC_SOURCE = "SHORTS"

# (#6) YouTube Partner Program Shorts thresholds.
_YPP_SUBS_TARGET = 1000
_YPP_SHORTS_VIEWS_TARGET = 10_000_000
_YPP_WINDOW_DAYS = 90

# (#7) Milestone thresholds — a digest callout fires when a snapshot crosses one.
_SUB_MILESTONES = (100, 500, 1_000, 5_000, 10_000, 50_000, 100_000, 1_000_000)
_VIEW_MILESTONES = (1_000, 10_000, 100_000, 1_000_000, 10_000_000, 100_000_000)

# (#10) Flag a view spike when a single day clears this multiple of the daily avg.
_SPIKE_MULTIPLE = 2.0

# Display label + colour per channel slug (shared by Telegram & Slack).
_CHANNEL_DISPLAY = {
    "wordstrata": ("🔵", "Wordstrata"),
    "the-mythscape": ("🟣", "The Mythscape"),
    "open-verdicts": ("🟠", "Open Verdicts"),
    "bright-beasts": ("🟢", "Bright Beasts"),
}


def _data_client(channel: str):
    return build("youtube", "v3", credentials=credentials(channel))


def _analytics_client(channel: str):
    return build("youtubeAnalytics", "v2", credentials=credentials(channel))


def channel_snapshot(channel: str, *, client=None) -> ChannelSnapshot:
    yt = client or _data_client(channel)
    resp = yt.channels().list(part="statistics", mine=True).execute()
    items = resp.get("items", [])
    if not items:
        raise RuntimeError(f"no YouTube channel for '{channel}' (check OAuth account)")
    s = items[0].get("statistics", {})
    # Lifetime totals reported directly from the YouTube Data API. YouTube
    # periodically revises these figures downward (spam-view audits, bot-sub
    # removals, re-counts). A lower value vs the prior snapshot is legitimate,
    # not a bug — do NOT floor these to the previous snapshot.
    return ChannelSnapshot(
        subscribers=int(s.get("subscriberCount", 0)),
        total_views=int(s.get("viewCount", 0)),
        video_count=int(s.get("videoCount", 0)),
    )


def period_metrics(channel: str, start: str, end: str, *, client=None) -> PeriodMetrics:
    ya = client or _analytics_client(channel)
    resp = ya.reports().query(
        ids="channel==MINE",
        startDate=start,
        endDate=end,
        metrics=_PERIOD_METRICS,
    ).execute()
    rows = resp.get("rows") or [[0, 0, 0, 0, 0, 0, 0, 0, 0.0]]
    row = rows[0]
    gained, lost, minutes, views, likes, comments, avg_dur, shares = (
        int(row[i]) for i in range(8)
    )
    avg_view_pct = float(row[8])
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    return PeriodMetrics(
        days=days,
        subscribers_gained=gained,
        subscribers_lost=lost,
        new_subscribers=gained - lost,
        estimated_minutes_watched=minutes,
        views=views,
        likes=likes,
        comments=comments,
        average_view_duration_s=avg_dur,
        shares=shares,
        average_view_percentage=avg_view_pct,
    )


def per_video(channel: str, video_ids: list[str], *, client=None) -> list[VideoAnalytics]:
    if not video_ids:
        return []
    yt = client or _data_client(channel)
    out: list[VideoAnalytics] = []
    for i in range(0, len(video_ids), _VIDEOS_BATCH):
        batch = video_ids[i:i + _VIDEOS_BATCH]
        resp = yt.videos().list(part="statistics", id=",".join(batch)).execute()
        for item in resp.get("items", []):
            st = item.get("statistics", {})
            out.append(VideoAnalytics(
                video_id=item["id"],
                views=int(st.get("viewCount", 0)),
                likes=int(st.get("likeCount", 0)),
                comments=int(st.get("commentCount", 0)),
            ))
    return out


def video_details(channel: str, video_ids: list[str], *, client=None) -> dict[str, dict]:
    """Per-video cumulative views/likes/comments + title/published_at, keyed by
    video id. Batched by the Data API's 50-id cap."""
    if not video_ids:
        return {}
    yt = client or _data_client(channel)
    out: dict[str, dict] = {}
    for i in range(0, len(video_ids), _VIDEOS_BATCH):
        batch = video_ids[i:i + _VIDEOS_BATCH]
        resp = yt.videos().list(part="snippet,statistics", id=",".join(batch)).execute()
        for item in resp.get("items", []):
            st = item.get("statistics", {})
            sn = item.get("snippet", {})
            out[item["id"]] = {
                "views": int(st.get("viewCount", 0)),
                "likes": int(st.get("likeCount", 0)),
                "comments": int(st.get("commentCount", 0)),
                "title": sn.get("title", ""),
                "published_at": sn.get("publishedAt", ""),
            }
    return out


def video_period_metrics(
    channel: str, start: str, end: str, video_ids: list[str], *, client=None,
) -> dict[str, dict]:
    """Per-video cumulative watch_minutes + shares over [start, end], keyed by
    video id. Filter is batched by 200 ids. Videos absent from the response
    (e.g. brand-new, no Analytics data yet) are simply omitted — the caller
    defaults them to 0.

    `maxResults` caps each batch at the batch size; since the `video==` filter
    bounds results to the batched ids, rows == ids and nothing is truncated. If
    the batch size were ever raised past the response cap this would silently
    drop rows — keep `_VIDEO_ANALYTICS_BATCH` at or below the API row limit."""
    if not video_ids:
        return {}
    ya = client or _analytics_client(channel)
    out: dict[str, dict] = {}
    for i in range(0, len(video_ids), _VIDEO_ANALYTICS_BATCH):
        batch = video_ids[i:i + _VIDEO_ANALYTICS_BATCH]
        resp = ya.reports().query(
            ids="channel==MINE",
            startDate=start,
            endDate=end,
            metrics=_VIDEO_METRICS,
            dimensions="video",
            filters="video==" + ",".join(batch),
            maxResults=_VIDEO_ANALYTICS_BATCH,
        ).execute()
        # row = [videoId, *_VIDEO_METRICS] → [videoId, watch_minutes, shares]
        for row in (resp.get("rows") or []):
            out[str(row[0])] = {"watch_minutes": int(row[1]), "shares": int(row[2])}
    return out


def _delta(today_cumulative: int, prior: dict | None, key: str) -> int:
    """Daily delta vs the prior snapshot; == cumulative on the first snapshot.
    Clamped at 0 so a deleted-then-recounted edge case never shows negative."""
    if prior is None:
        return today_cumulative
    return max(0, today_cumulative - int(prior[key]))


def _days_live(published_at: str, snapshot_date: str) -> int:
    if not published_at:
        return 0
    published = date.fromisoformat(published_at[:10])
    return max(0, (date.fromisoformat(snapshot_date) - published).days)


def channel_video_stats(
    channel: str,
    *,
    today: date | None = None,
    data_client=None,
    analytics_client=None,
) -> ChannelVideoStats:
    """Per-video rows for one channel: cumulative totals (Data + Analytics APIs)
    plus daily deltas vs the prior stored snapshot. Persists today's snapshot."""
    today = today or date.today()
    snapshot_date = today.isoformat()
    video_ids = db.uploaded_video_ids(channel)
    if not video_ids:
        return ChannelVideoStats(channel=channel, rows=[])

    details = video_details(channel, video_ids, client=data_client)
    # Analytics window starts at the earliest publish date we know about.
    publish_dates = [d["published_at"][:10] for d in details.values() if d.get("published_at")]
    start = min(publish_dates) if publish_dates else snapshot_date
    period = video_period_metrics(
        channel, start, snapshot_date, video_ids, client=analytics_client)

    rows: list[VideoStatRow] = []
    for vid in video_ids:
        d = details.get(vid)
        if d is None:
            continue  # video deleted/private since upload — skip, no row
        a = period.get(vid, {"watch_minutes": 0, "shares": 0})
        prior = db.video_snapshot_before(channel, vid, snapshot_date)
        views, likes, comments = d["views"], d["likes"], d["comments"]
        watch, shares = a["watch_minutes"], a["shares"]
        # *_total fields are the raw YouTube Data API cumulative values.
        # YouTube revises these downward (spam-view audits, re-counts), so a
        # lower value than yesterday's snapshot is expected — do NOT floor to
        # the prior snapshot value.
        rows.append(VideoStatRow(
            date=snapshot_date,
            video_id=vid,
            url=f"https://www.youtube.com/shorts/{vid}",
            title=d["title"],
            published_at=d["published_at"],
            days_live=_days_live(d["published_at"], snapshot_date),
            views_total=views, views_today=_delta(views, prior, "views"),
            likes_total=likes, likes_today=_delta(likes, prior, "likes"),
            comments_total=comments, comments_today=_delta(comments, prior, "comments"),
            watch_min_total=watch, watch_min_today=_delta(watch, prior, "watch_minutes"),
            shares_total=shares, shares_today=_delta(shares, prior, "shares"),
        ))
        db.record_video_snapshot(
            channel, vid, snapshot_date=snapshot_date,
            views=views, likes=likes, comments=comments,
            watch_minutes=watch, shares=shares,
        )
    return ChannelVideoStats(channel=channel, rows=rows)


def all_video_stats(
    channels: list[str], *, today: date | None = None,
) -> VideoStatsReport:
    """Per-video stats for every channel; a failing channel is recorded in
    `errors` rather than failing the whole report (mirrors build_daily_report)."""
    today = today or date.today()
    results: list[ChannelVideoStats] = []
    errors: list[str] = []
    for channel in channels:
        try:
            results.append(channel_video_stats(channel, today=today))
        except Exception as e:  # noqa: BLE001 — one bad channel must not block the rest
            errors.append(f"{channel}: {type(e).__name__}: {e}")
            log.warning("video stats failed for channel=%s: %s", channel, e)
    return VideoStatsReport(date=today.isoformat(), channels=results, errors=errors)


def traffic_sources(channel: str, start: str, end: str, *, client=None) -> dict[str, int]:
    """(#2) Views per insightTrafficSourceType over the window → {source: views}."""
    ya = client or _analytics_client(channel)
    resp = ya.reports().query(
        ids="channel==MINE",
        startDate=start,
        endDate=end,
        metrics="views",
        dimensions="insightTrafficSourceType",
        sort="-views",
    ).execute()
    return {str(src): int(views) for src, views in (resp.get("rows") or [])}


def top_countries(
    channel: str, start: str, end: str, *, limit: int = 3, client=None,
) -> list[CountryViews]:
    """(#8) Top viewing countries over the window, descending by views."""
    ya = client or _analytics_client(channel)
    resp = ya.reports().query(
        ids="channel==MINE",
        startDate=start,
        endDate=end,
        metrics="views",
        dimensions="country",
        sort="-views",
        maxResults=limit,
    ).execute()
    rows = (resp.get("rows") or [])[:limit]
    return [CountryViews(country=str(cc), views=int(v)) for cc, v in rows]


def shorts_feed_share(sources: dict[str, int]) -> float:
    """(#2) Fraction of windowed views that came from the Shorts feed."""
    total = sum(sources.values())
    if not total:
        return 0.0
    return sources.get(_SHORTS_TRAFFIC_SOURCE, 0) / total


def top_video(videos: list[VideoAnalytics]) -> TopVideo | None:
    """(#5) Best-performing uploaded short by lifetime views (no extra API call)."""
    if not videos:
        return None
    best = max(videos, key=lambda v: v.views)
    return TopVideo(
        video_id=best.video_id,
        url=f"https://www.youtube.com/shorts/{best.video_id}",
        views=best.views,
        likes=best.likes,
        comments=best.comments,
    )


def ypp_progress(subscribers: int, shorts_views_90d: int) -> YppProgress:
    """(#6) Progress toward the YPP Shorts bar (1k subs + 10M 90d Shorts views)."""
    return YppProgress(
        subscribers=subscribers,
        subs_target=_YPP_SUBS_TARGET,
        subs_progress=min(subscribers / _YPP_SUBS_TARGET, 1.0),
        shorts_views_90d=shorts_views_90d,
        shorts_views_target=_YPP_SHORTS_VIEWS_TARGET,
        shorts_views_progress=min(shorts_views_90d / _YPP_SHORTS_VIEWS_TARGET, 1.0),
        eligible=(
            subscribers >= _YPP_SUBS_TARGET
            and shorts_views_90d >= _YPP_SHORTS_VIEWS_TARGET
        ),
    )


def _crossed(prev: int, cur: int, thresholds) -> list[int]:
    return [t for t in thresholds if prev < t <= cur]


def compute_milestones(
    *,
    prev_subscribers: int | None,
    subscribers: int,
    prev_total_views: int | None,
    total_views: int,
) -> list[str]:
    """(#7) Callouts for thresholds crossed since the prior snapshot. The first
    run (no prior snapshot) yields nothing — a crossing needs a 'before'."""
    if prev_subscribers is None or prev_total_views is None:
        return []
    out = [
        f"🎉 crossed {t:,} subscribers"
        for t in _crossed(prev_subscribers, subscribers, _SUB_MILESTONES)
    ]
    out += [
        f"🎉 crossed {t:,} total views"
        for t in _crossed(prev_total_views, total_views, _VIEW_MILESTONES)
    ]
    return out


def detect_alerts(
    *,
    views_1d: int,
    period_views: int,
    period_days: int,
    uploads_24h: int,
) -> list[str]:
    """(#10) Anomaly flags: a view spike vs the daily average, and a stalled
    upload pipeline."""
    alerts: list[str] = []
    avg_daily = period_views / period_days if period_days else 0
    if avg_daily > 0 and views_1d >= _SPIKE_MULTIPLE * avg_daily:
        alerts.append(f"🔥 views {views_1d / avg_daily:.1f}× daily average")
    if uploads_24h == 0:
        alerts.append("⚠️ no uploads in last 24h")
    return alerts


def channel_analytics(
    channel: str,
    days: int = 30,
    *,
    today: date | None = None,
    data_client=None,
    analytics_client=None,
) -> ChannelAnalytics:
    today = today or date.today()
    end = today.isoformat()
    start = (today - timedelta(days=days - 1)).isoformat()
    yesterday = (today - timedelta(days=1)).isoformat()
    window_90d_start = (today - timedelta(days=_YPP_WINDOW_DAYS - 1)).isoformat()

    snapshot = channel_snapshot(channel, client=data_client)
    period = period_metrics(channel, start, end, client=analytics_client)
    one_day = period_metrics(channel, yesterday, yesterday, client=analytics_client)
    ninety_day = period_metrics(channel, window_90d_start, end, client=analytics_client)
    sources = traffic_sources(channel, start, end, client=analytics_client)
    countries = top_countries(channel, start, end, client=analytics_client)
    videos = per_video(channel, db.uploaded_video_ids(channel), client=data_client)

    # Count and average denominator are the same set: shorts YouTube returned
    # stats for (== our uploads, minus any deleted/private videos). Keeping them
    # in lockstep means the digest's "avg X across N shorts" is self-consistent.
    n = len(videos)
    avg_likes = round(sum(v.likes for v in videos) / n, 1) if n else 0.0
    avg_comments = round(sum(v.comments for v in videos) / n, 1) if n else 0.0

    # (#4/#7) Trend + milestones come from the prior snapshot — zero extra API
    # calls. Read the baseline BEFORE recording today's row.
    prior = db.snapshot_before(channel, end)
    trend = _trend_from_snapshot(prior, snapshot, period, today)
    milestones = compute_milestones(
        prev_subscribers=prior["subscribers"] if prior else None,
        subscribers=snapshot.subscribers,
        prev_total_views=prior["total_views"] if prior else None,
        total_views=snapshot.total_views,
    )

    # (#9) Pipeline tie-ins straight from state.db.
    queue_pending = db.pending_word_count(channel)
    uploads_24h = db.uploads_since(channel, f"{yesterday} 00:00:00")

    alerts = detect_alerts(
        views_1d=one_day.views,
        period_views=period.views,
        period_days=period.days,
        uploads_24h=uploads_24h,
    )

    db.record_analytics_snapshot(
        channel,
        snapshot_date=end,
        subscribers=snapshot.subscribers,
        total_views=snapshot.total_views,
        views_period=period.views,
        watch_minutes_period=period.estimated_minutes_watched,
        shares_period=period.shares,
    )

    return ChannelAnalytics(
        channel=channel,
        snapshot=snapshot,
        new_subs_1d=one_day.new_subscribers,
        period=period,
        videos_uploaded=n,
        avg_likes_per_video=avg_likes,
        avg_comments_per_video=avg_comments,
        videos=videos,
        traffic_sources=sources,
        shorts_feed_share=shorts_feed_share(sources),
        top_countries=countries,
        top_video=top_video(videos),
        ypp=ypp_progress(snapshot.subscribers, ninety_day.views),
        views_90d=ninety_day.views,
        queue_pending=queue_pending,
        uploads_24h=uploads_24h,
        trend=trend,
        milestones=milestones,
        alerts=alerts,
    )


def _trend_from_snapshot(prior, snapshot, period, today) -> TrendDelta | None:
    """(#4) Delta of totals vs the prior snapshot, or None on the first run.

    Negative deltas (e.g. subscribers −2, views −11) are intentional and
    correct: YouTube revises lifetime counts downward during spam/bot audits.
    These are NOT clamped to zero — clamping would hide real corrections and
    make the Sheet's trend columns misleading. A negative delta in the Sheet
    is a signal to investigate, not a pipeline bug.
    """
    if not prior:
        return None
    compared = (today - date.fromisoformat(prior["date"])).days
    return TrendDelta(
        compared_days=compared,
        subscribers=snapshot.subscribers - prior["subscribers"],
        total_views=snapshot.total_views - prior["total_views"],
        period_views=period.views - prior["views_period"],
    )


def build_daily_report(
    channels: list[str],
    days: int = 30,
    *,
    today: date | None = None,
) -> DailyAnalyticsReport:
    """Fetch analytics for each channel; a failing channel is recorded in
    `errors` rather than failing the whole report."""
    today = today or date.today()
    results: list[ChannelAnalytics] = []
    errors: list[str] = []
    for channel in channels:
        try:
            results.append(channel_analytics(channel, days, today=today))
        except Exception as e:  # noqa: BLE001 — one bad channel must not block the rest
            errors.append(f"{channel}: {type(e).__name__}: {e}")
            log.warning("analytics failed for channel=%s: %s", channel, e)
    report = DailyAnalyticsReport(
        date=today.isoformat(), days=days, channels=results, errors=errors,
    )
    report.digest_text = render_digest(report)
    return report


# ─── Digest rendering (the Telegram / Slack message body) ───────────────────

def _fmt_duration(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def _channel_label(slug: str) -> str:
    emoji, name = _CHANNEL_DISPLAY.get(slug, ("⚪", slug))
    return f"{emoji} {name}"


def _trend_suffix(trend: TrendDelta | None) -> str:
    if not trend or trend.subscribers == 0:
        return ""
    arrow = "▲" if trend.subscribers > 0 else "▼"
    return f"  {arrow}{abs(trend.subscribers):,} subs/{trend.compared_days}d"


def _render_channel(ca: ChannelAnalytics) -> list[str]:
    p = ca.period
    lines = [f"{_channel_label(ca.channel)}{_trend_suffix(ca.trend)}"]
    lines.append(
        f" Subs {ca.snapshot.subscribers:,} "
        f"(+{ca.new_subs_1d} today · +{p.new_subscribers}/{p.days}d)"
    )
    lines.append(
        f" Watch {p.estimated_minutes_watched:,} min/{p.days}d "
        f"· avg {_fmt_duration(p.average_view_duration_s)} "
        f"· {round(p.average_view_percentage)}% retention"
    )
    lines.append(
        f" Views {p.views:,}/{p.days}d · 👍 {p.likes:,} · 💬 {p.comments:,} "
        f"· ↗ {p.shares:,} shares"
    )
    lines.append(f" Feed: {round(ca.shorts_feed_share * 100)}% from Shorts feed")
    if ca.top_countries:
        geo = " · ".join(f"{c.country} {c.views:,}" for c in ca.top_countries)
        lines.append(f" Geo: {geo}")
    if ca.top_video:
        tv = ca.top_video
        lines.append(f" 🏆 Top short: {tv.views:,} views (👍 {tv.likes:,}) {tv.url}")
    lines.append(
        f" Per short: 👍 {ca.avg_likes_per_video:g} · 💬 {ca.avg_comments_per_video:g} "
        f"(across {ca.videos_uploaded} shorts)"
    )
    if ca.ypp:
        y = ca.ypp
        status = "✅ eligible" if y.eligible else (
            f"subs {round(y.subs_progress * 100)}% · "
            f"Shorts views {round(y.shorts_views_progress * 100)}% "
            f"({y.shorts_views_90d / 1_000_000:.1f}M/"
            f"{y.shorts_views_target // 1_000_000}M)"
        )
        lines.append(f" YPP: {status}")
    lines.append(f" Queue: {ca.queue_pending:,} pending · {ca.uploads_24h} uploaded/24h")
    lines.extend(f" {m}" for m in ca.milestones)
    lines.extend(f" {a}" for a in ca.alerts)
    return lines


def render_digest(report: DailyAnalyticsReport) -> str:
    """Render the daily report as the Telegram/Slack message body. Pure — the
    n8n Code node just forwards this string."""
    out = [f"📊 Daily Analytics — {report.date}", ""]
    for ca in report.channels:
        out.extend(_render_channel(ca))
        out.append("")
    if report.errors:
        out.append("⚠️ Errors:")
        out.extend(f" • {e}" for e in report.errors)
    return "\n".join(out).rstrip()
