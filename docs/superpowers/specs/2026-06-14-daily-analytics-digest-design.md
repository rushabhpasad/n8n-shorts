# Daily Analytics Digest — Design Spec

**Date:** 2026-06-14
**Status:** Approved (design); pending implementation plan
**Branch:** `feat/daily-analytics-digest`

## Problem

We run four YouTube channels (`wordstrata`, `the-mythscape`, `open-verdicts`,
`bright-beasts`), each posting one Short/day via the existing n8n pipelines.
There is currently no visibility into how the posted content performs. We want a
**daily analytics digest** — total/new subscribers, likes & comments per video,
total watch time over the trailing 30 days, and related metrics — delivered to
**Telegram every morning**, with each day's snapshot **appended to a Google
Sheet** for trend tracking.

## Goals

- Daily, per-channel metrics pushed to Telegram as one readable digest.
- Each run appends one row per channel to a Google Sheet (trend history).
- Reuse the existing FastAPI service and its per-channel Google OAuth plumbing.
- Keep the n8n workflow thin: fetch → store → notify.

## Non-goals

- Real-time / intraday analytics (daily cadence only).
- A web dashboard or charts (Google Sheet is the store; charting is the user's).
- Historical backfill of metrics before this ships (forward-looking only).
- Demographics, traffic sources, revenue (out of scope for v1).

## Constraints discovered

- Existing per-channel OAuth tokens are scoped **`youtube.upload` only**
  (`api/services/youtube.py:24`). Watch time, subscribers-gained, and
  period like/comment metrics require the **YouTube Analytics API**, which needs
  `yt-analytics.readonly` + `youtube.readonly`. **Each channel must be
  re-consented once** to add these read scopes (upload scope retained).
- The per-channel OAuth bootstrap script `scripts/yt_init.py` already exists
  (`SCOPES` hard-coded to upload-only). Widening its `SCOPES` + re-running it per
  channel is the re-consent path — no new script needed.
- n8n holds **no** Google/Telegram/Sheets credentials (uploads go through the
  FastAPI service). Sheets + Telegram credentials are new and created in n8n.
- The `runs` table (`sql/schema.sql`) records `channel`, `youtube_video_id`,
  `status` — the canonical list of "our uploaded shorts." We query this rather
  than scanning each channel's entire upload history.
- YouTube Analytics API has ~hours-to-2-day latency; "today's" deltas are
  estimates that settle over following days. Acceptable for a daily digest.

## Architecture

```
n8n "Daily Analytics Digest"  (Schedule trigger, daily ~06:00, after 01–04:00 uploads)
  │
  ├─▶ HTTP Request: GET http://host.docker.internal:7860/analytics/daily?days=30
  │       └─ FastAPI analytics service (per channel):
  │            • Data API channels.list      → total subs, total views, video count (snapshot)
  │            • Analytics API reports        → new subs (1d & 30d), watch time 30d,
  │                                             avg view duration, period likes/comments
  │            • runs table → our video_ids   → Data API videos.list per-video stats
  │
  ├─▶ Google Sheets (append): 4 rows → "daily" tab
  ├─▶ Code node: format Telegram digest text
  └─▶ Telegram (sendMessage): post digest
        └─ on error (any node): Telegram "⚠️ Analytics digest failed: <reason>"
```

The FastAPI service performs all Google API work, reusing the existing
per-channel OAuth refresh-token flow. n8n orchestrates only.

## Components

### 1. `api/services/analytics.py` (new)
Per-channel functions, each building authorized Data API + Analytics API clients
from the (re-scoped) token. Pure shaping logic separated from I/O where practical.

- `channel_snapshot(channel) -> ChannelSnapshot`
  Data API `channels.list(part=statistics, mine=true)` → `subscriberCount`,
  `viewCount`, `videoCount`.
- `period_metrics(channel, start, end) -> PeriodMetrics`
  Analytics API `reports.query` (ids=`channel==MINE`, metrics=
  `subscribersGained,subscribersLost,estimatedMinutesWatched,views,likes,comments,averageViewDuration`).
- `per_video(channel, video_ids) -> list[VideoAnalytics]`
  Data API `videos.list(part=statistics, id=<batch>)` → `likeCount`,
  `commentCount`, `viewCount` per short (lifetime cumulative). IDs come from the
  `runs` table.
- Helpers compute: `new_subs = gained - lost` (1-day and 30-day windows),
  `avg_likes_per_video`, `avg_comments_per_video`.

### 2. `api/models.py` (extend)
- `ChannelSnapshot`, `PeriodMetrics`, `VideoAnalytics` — typed API shapes.
- `ChannelAnalytics` — rollup combining the above for one channel.
- `DailyAnalyticsReport` — `date` + `list[ChannelAnalytics]` (the `/analytics/daily` body).

### 3. Endpoints (`api/main.py` or a routes module, matching existing style)
- `GET /{channel}/analytics?days=30` → `ChannelAnalytics` (ad-hoc / debugging).
- `GET /analytics/daily?days=30` → `DailyAnalyticsReport` (all 4 channels; what n8n calls).

### 4. `scripts/yt_init.py` (modify) + `api/services/youtube.py` (modify)
Widen the shared scope set to
`["youtube.upload", "yt-analytics.readonly", "youtube.readonly"]` in both files,
then re-run the existing bootstrap once per channel to re-consent:
`uv run scripts/yt_init.py --channel <slug>`. The existing token-refresh path in
`youtube.py` picks up the new scopes after re-consent.

### 5. n8n workflow "Daily Analytics Digest" (new)
Built and validated via n8n-mcp (`validate_workflow` → `create_workflow_from_code`),
imported alongside the 4 existing workflows. Nodes: Schedule trigger → HTTP
Request → Google Sheets (append) → Code (format) → Telegram (send), plus an
error path posting a Telegram failure alert.

## Metrics → API mapping

| Metric | Source | Window |
|---|---|---|
| Total subscribers, total views, video count | Data API `channels.list` | snapshot (now) |
| New subscribers (gained − lost) | Analytics API `subscribersGained/Lost` | yesterday **and** 30d |
| Total watch time + avg view duration | Analytics API `estimatedMinutesWatched`, `averageViewDuration` | trailing 30d |
| Likes / comments per video | Data API `videos.list` over `runs.youtube_video_id` | lifetime cumulative |
| Period likes / comments + avg per video | Analytics API + count of our uploads | trailing 30d |

## Google Sheet — single "daily" tab

One append per channel per run (4 rows/day). Columns:

```
date, channel, total_subscribers, new_subs_1d, new_subs_30d, total_views,
videos_uploaded, watch_time_min_30d, avg_view_duration_s,
avg_likes_per_video, avg_comments_per_video, likes_30d, comments_30d
```

Auth: a **Google service account** + JSON key; share the spreadsheet with the SA
email (no OAuth consent flow — best for headless n8n).

## Telegram digest format

One message, a per-channel block:

```
📊 Daily Analytics — 2026-06-14

🔵 Wordstrata
 Subs 1,204 (+8 today · +210/30d)
 Watch 3,420 min/30d · avg 0:41
 Per short: 👍 14 · 💬 3 (across 32 shorts)
… ×4 channels …
```

Error path: any failed node posts `⚠️ Analytics digest failed: <reason>` rather
than failing silently.

## Error handling

- FastAPI: a per-channel API failure degrades to nulls for that channel's
  affected fields and is flagged in the response (`errors: [...]`) rather than
  failing the whole report — one broken channel must not block the other three.
- n8n: HTTP Request and Sheets nodes use the error path → Telegram alert.
- Empty channel (no uploads yet): per-video list is empty; averages render as 0/—.

## Testing (TDD)

- Unit tests for `analytics.py` with **mocked** Google clients (no live calls):
  snapshot shaping, period aggregation, per-video rollup, `new_subs` math,
  empty-channel edge case, single-channel-failure degradation.
- Endpoint test for `/analytics/daily` with the service layer mocked.
- n8n workflow passes `validate_workflow` before import.

## Defaults chosen

- **Schedule:** 06:00 daily (after the 01–04:00 upload runs).
- **"Likes/comments per video":** lifetime cumulative per short, with 30-day
  period totals also reported.
- **`days` window:** 30 (overridable via query param).

## Prerequisites (user-performed; documented as steps)

1. **Telegram:** BotFather → bot token; obtain chat ID via `getUpdates`.
2. **Google Cloud:** service account + JSON key for Sheets; create the
   spreadsheet; share it with the SA email. Enable YouTube Data API v3 +
   YouTube Analytics API on the project behind the channels' OAuth clients.
3. **Re-consent:** run `scripts/youtube_authorize.py --channel <slug>` once per
   channel to add analytics read scopes.

## Docs to update on ship

- `README.md` — new analytics feature + setup.
- `AGENTS.md` — analytics service, endpoint, re-auth scopes, new workflow.
