# Per-Video Daily Stats → Per-Channel Sheets Tabs — Design Spec

**Date:** 2026-06-17
**Status:** Approved (design); pending implementation plan
**Branch:** `feat/per-video-daily-stats`

## Problem

The existing **Daily Analytics Digest** (workflow `ENbQm9ctfNRcnOuT`) writes one
*channel-level* aggregate row per day to a single `daily` tab. It tells us how a
channel is doing overall, but not how each individual Short performs over time.
We want **per-video daily statistics** for every published video, captured daily
and stored in the **same Google Sheets document** — with **each channel getting
its own tab/sheet** — so we can see and measure per-video outreach day over day.

## Goals

- Daily, per-video metrics for every published Short, per channel.
- One tab per channel in the existing analytics spreadsheet.
- **Time-series append:** one row per video *per day* (enables growth charts and
  velocity analysis).
- **Daily deltas** (`*_today`) computed server-side alongside cumulative
  (`*_total`) numbers, so the sheet needs zero formulas.
- Reuse the existing FastAPI service, per-channel OAuth, and the existing Sheets
  service-account credential and 06:00 workflow.

## Non-goals

- Real-time / intraday stats (daily cadence only).
- Historical backfill before this ships (forward-looking; deltas begin once
  snapshots accumulate).
- A web dashboard or charts (the Sheet is the store; charting is the user's).
- Per-video demographics, revenue, or traffic-source breakdowns.

## Decisions (from brainstorming)

1. **Time-series append**, not latest-snapshot upsert. The sheet accumulates one
   row per video per day.
2. **Backend computes deltas** via a new `video_snapshots` table (mirrors the
   existing channel-level `analytics_snapshots`). Self-healing across missed days
   — a delta spans whatever gap exists to the previous stored snapshot.
3. **Columns** (16): identity + cumulative + daily-delta for five metrics.
4. **Extend the existing Digest workflow** (`ENbQm9ctfNRcnOuT`) with a second
   branch; **auto-create tabs** so adding a 5th channel needs no manual setup.

## Constraints discovered

- The YouTube **Data API** (`videos.list`, `part="snippet,statistics"`) provides
  per-video **views, likes, comments** (cumulative lifetime) plus title and
  publishedAt — but **not shares and not watch time**.
- Per-video **shares** and **watch time** are only available from the **YouTube
  Analytics API v2** (`reports.query`, `dimensions=video`,
  `metrics=estimatedMinutesWatched,shares`). The existing OAuth tokens already
  carry `yt-analytics.readonly` + `youtube.readonly` (added for the channel-level
  digest), so **no re-consent is needed**.
- Analytics API data lags **~1–2 days**, so `watch_min_*` and `shares_*` will
  trail the real-time `views/likes/comments` (Data API) by a day. Acceptable for
  trend tracking; documented so the lag is not mistaken for a bug.
- The Analytics API `dimensions=video` query is bounded (paginates beyond ~200
  rows). Current channels have tens of videos; pagination is implemented for
  safety but rarely exercised.
- `api/services/analytics.py` already has a `per_video()` helper and a
  `VideoAnalytics` model (Data-API views/likes/comments only). The new work
  enriches this with watch time + shares and the snapshot/delta layer.

## Architecture

```
n8n "Daily Analytics Digest" (ENbQm9ctfNRcnOuT, 06:00)
  ├─ [existing] GET /analytics/daily ─► Split rows ─► Sheets append (tab "daily")
  │                                  └► Telegram + Slack digest
  └─ [NEW]      GET /analytics/videos ─► Code "Split video rows"
                                          ─► Sheets append (tab = row.channel, auto-create)

FastAPI (:7860)
  GET /analytics/videos          → all_video_stats()  → VideoStatsReport
  GET /{channel}/analytics/videos→ channel_video_stats(channel)  (debug parity)

channel_video_stats(channel):
  uploaded_video_ids ─► Data API videos.list (batch 50: snippet+statistics)
                     ─► Analytics API reports.query (dim=video: watch+shares)
                     ─► per video: diff vs video_snapshot_before → *_today
                     ─► record_video_snapshot(today cumulative)
                     ─► VideoStatRow[]
```

### 1. Data model & storage (`sql/schema.sql`, `api/db.py`)

```sql
CREATE TABLE video_snapshots (
    channel        TEXT    NOT NULL,
    video_id       TEXT    NOT NULL,
    date           TEXT    NOT NULL,          -- YYYY-MM-DD snapshot date
    views          INTEGER NOT NULL DEFAULT 0,   -- cumulative lifetime
    likes          INTEGER NOT NULL DEFAULT 0,
    comments       INTEGER NOT NULL DEFAULT 0,
    watch_minutes  INTEGER NOT NULL DEFAULT 0,   -- cumulative (Analytics API)
    shares         INTEGER NOT NULL DEFAULT 0,    -- cumulative (Analytics API)
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, video_id, date)
);
CREATE INDEX idx_video_snapshots_lookup
    ON video_snapshots(channel, video_id, date DESC);
```

`db.py` helpers (named for symmetry with `record_analytics_snapshot` /
`snapshot_before`):

- `record_video_snapshot(channel, video_id, snapshot_date, views, likes, comments, watch_minutes, shares)` — idempotent upsert on `(channel, video_id, date)`.
- `video_snapshot_before(channel, video_id, snapshot_date)` → most recent prior
  row (dict) or `None`. Used for deltas; spans gaps if a day was missed.

### 2. Models (`api/models.py`)

```python
class VideoStatRow(BaseModel):
    date: str
    video_id: str
    url: str
    title: str
    published_at: str
    days_live: int
    views_total: int
    views_today: int
    likes_total: int
    likes_today: int
    comments_total: int
    comments_today: int
    watch_min_total: int
    watch_min_today: int
    shares_total: int
    shares_today: int

class ChannelVideoStats(BaseModel):
    channel: str
    rows: list[VideoStatRow]

class VideoStatsReport(BaseModel):
    date: str
    channels: list[ChannelVideoStats]
    errors: list[str] = []
```

### 3. Analytics service (`api/services/analytics.py`)

- `channel_video_stats(channel) -> ChannelVideoStats`:
  1. `db.uploaded_video_ids(channel)` → IDs (empty → empty rows, no error).
  2. **Data API** `videos.list(part="snippet,statistics", id=…)` batched 50 →
     `{video_id: {views, likes, comments, title, published_at}}`.
  3. **Analytics API** `reports.query(ids="channel==MINE", dimensions="video",
     metrics="estimatedMinutesWatched,shares", startDate=<earliest publish>,
     endDate=<latest complete day>, filters="video==id1,id2,…")` →
     `{video_id: {watch_minutes, shares}}`. Paginate via `startIndex` if needed.
     Videos absent from the Analytics response default to 0 (e.g. brand-new).
  4. For each video: read `video_snapshot_before` → compute each `*_today`
     (`max(0, today_cumulative - prior_cumulative)`; first-ever snapshot →
     `*_today == *_total`). Compute `days_live` from `published_at` vs `date`.
  5. `record_video_snapshot(...)` with today's cumulative numbers.
  6. Build `VideoStatRow[]`, newest publish first.
- `all_video_stats() -> VideoStatsReport`: loop channels, isolating each
  channel's failure into `errors[]` (same try/except-per-channel pattern as the
  existing daily report). `date` = today (matches existing report convention).

### 4. API endpoints (`api/main.py`)

- `GET /analytics/videos` → `all_video_stats()` (consumed by n8n).
- `GET /{channel}/analytics/videos` → `channel_video_stats(channel)` wrapped in a
  single-channel report, for debugging parity with `GET /{channel}/analytics`.

### 5. n8n wiring (extend `ENbQm9ctfNRcnOuT`)

Add a branch off the same 06:00 schedule trigger:

`HTTP GET http://host.docker.internal:7860/analytics/videos`
→ **Code** "Split video rows": flatten `channels[].rows[]`, carrying
`channel` onto each emitted item.
→ **Google Sheets** (append): same service-account credential and spreadsheet;
`sheetName` set dynamically from `{{ $json.channel }}`; **auto-create tab**
enabled; map columns by header (the 16 fields above, minus the internal
`channel` routing key — or keep `channel` as a leading column; see open
questions).

**Source-of-truth check (do in planning):** confirm whether `ENbQm9ctfNRcnOuT`
is regenerated by `n8n/generate.py` or is a live-only workflow (like the
Error-Alert workflow, which lives only in n8n and is patched via the API). Apply
the branch through whichever is authoritative. If patched live, follow the
established surgical-update pattern (`scripts/patch_telegram_parsemode.py` style)
and the known n8n API gotchas (drop newer `settings` keys on PUT;
`errorWorkflow` silently dropped; publish required).

### 6. Tests (TDD — `api/test_video_stats.py`)

- First-ever snapshot → every `*_today` equals its `*_total`.
- Second snapshot → deltas equal the cumulative difference; non-negative clamp.
- Missed-day → delta correctly spans the gap to the previous stored snapshot.
- Videos missing from the Analytics response → watch/shares default to 0.
- `days_live` computed correctly from `published_at`.
- Per-channel error isolation: one channel raising still yields rows for the
  others plus an `errors[]` entry.
- Batching: >50 videos issues multiple Data API calls; rows still complete.
- All YouTube API calls mocked (no network), matching existing test style.

### 7. Docs

- **README:** document the per-channel tabs and the 16-column layout, plus the
  Analytics-API lag note for `watch_min_*` / `shares_*`.
- **AGENTS.md:** record the new endpoint, the `video_snapshots` table, and the
  extended Digest workflow branch.

## Column reference

```
date | video_id | url | title | published_at | days_live |
views_total | views_today | likes_total | likes_today |
comments_total | comments_today | watch_min_total | watch_min_today |
shares_total | shares_today
```

## Open questions (resolve in planning)

- Keep `channel` as a literal leading column in each tab, or rely on the tab name
  alone? (Leaning: tab name is sufficient; omit the column to keep tabs clean.)
- Whether `n8n/generate.py` owns the Digest workflow — determines patch vs
  regenerate (see §5).
