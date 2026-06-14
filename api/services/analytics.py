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
    DailyAnalyticsReport,
    PeriodMetrics,
    VideoAnalytics,
)
from services.youtube import credentials

log = logging.getLogger("shorts-api.analytics")

# Column order returned by reports.query — keep parsing in lockstep.
_PERIOD_METRICS = (
    "subscribersGained,subscribersLost,estimatedMinutesWatched,"
    "views,likes,comments,averageViewDuration"
)
_VIDEOS_BATCH = 50  # Data API videos.list id cap


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
    rows = resp.get("rows") or [[0, 0, 0, 0, 0, 0, 0]]
    gained, lost, minutes, views, likes, comments, avg_dur = (int(x) for x in rows[0])
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

    snapshot = channel_snapshot(channel, client=data_client)
    period = period_metrics(channel, start, end, client=analytics_client)
    one_day = period_metrics(channel, yesterday, yesterday, client=analytics_client)
    videos = per_video(channel, db.uploaded_video_ids(channel), client=data_client)

    # Count and average denominator are the same set: shorts YouTube returned
    # stats for (== our uploads, minus any deleted/private videos). Keeping them
    # in lockstep means the digest's "avg X across N shorts" is self-consistent.
    n = len(videos)
    avg_likes = round(sum(v.likes for v in videos) / n, 1) if n else 0.0
    avg_comments = round(sum(v.comments for v in videos) / n, 1) if n else 0.0

    return ChannelAnalytics(
        channel=channel,
        snapshot=snapshot,
        new_subs_1d=one_day.new_subscribers,
        period=period,
        videos_uploaded=n,
        avg_likes_per_video=avg_likes,
        avg_comments_per_video=avg_comments,
        videos=videos,
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
            errors.append(f"{channel}: {e}")
            log.warning("analytics failed for channel=%s: %s", channel, e)
    return DailyAnalyticsReport(
        date=today.isoformat(), days=days, channels=results, errors=errors,
    )
