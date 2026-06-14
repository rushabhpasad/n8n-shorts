# Daily Analytics Digest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collect daily per-channel YouTube analytics (subscribers, likes/comments per video, 30-day watch time) for the 4 channels, append each day's snapshot to a Google Sheet, and push a digest to Telegram.

**Architecture:** Extend the existing FastAPI service with an analytics service + endpoints (reusing the per-channel Google OAuth plumbing, scopes widened to add read access). A new, thin n8n workflow calls `GET /analytics/daily`, fans the result into Google Sheets rows and a formatted Telegram message, and alerts on failure.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, `google-api-python-client` (YouTube Data API v3 + YouTube Analytics API v2), SQLite, pytest; n8n (Schedule, HTTP Request, Code, Google Sheets, Telegram nodes).

---

## Spec

`docs/superpowers/specs/2026-06-14-daily-analytics-digest-design.md` (approved).

## Conventions (verified in codebase)

- Tests are flat files `api/test_*.py`, run from repo root with
  `uv run --project api pytest api/test_<x>.py -v` (pytest prepends the test
  file's dir, so `from models import ...` resolves). Mirror `api/test_models.py`.
- Endpoints live in `api/main.py`, scoped `/{channel}/...`, use `_resolve_channel`,
  `response_model=`, and offload blocking work via `asyncio.to_thread`.
- DB access via the `with db.conn() as c:` context manager; rows are
  `sqlite3.Row` (access by column name). Query helpers return `dict`/`list`.
- `settings` is `from config import settings`; monkeypatch `db.settings.db_path`
  in tests for a temp DB.
- **Library-API caution (per repo rules):** before writing the YouTube Analytics
  calls and the n8n nodes, verify current signatures with the `find-docs` skill
  (YouTube Analytics API v2 `reports.query`; n8n node params via
  `get_node_types`). Do not rely on memory for exact field names.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `api/services/youtube.py` | modify | Widen `SCOPES`; rename `_credentials` → public `credentials` |
| `scripts/yt_init.py` | modify | Widen `SCOPES` to match (re-consent path) |
| `api/db.py` | modify | Add `uploaded_video_ids(channel)` query helper |
| `api/models.py` | modify | Add analytics response models |
| `api/services/analytics.py` | create | YouTube Data + Analytics API calls; per-channel rollup; daily report builder |
| `api/main.py` | modify | Add `GET /{channel}/analytics` and `GET /analytics/daily` |
| `api/test_analytics.py` | create | Unit tests for db helper, service, scopes |
| n8n workflow "Daily Analytics Digest" | create (via n8n-mcp) | Schedule → fetch → Sheets + Telegram |
| `README.md`, `AGENTS.md` | modify | Document feature, setup, scopes, workflow |

---

## Phase 0 — Prerequisites (USER-PERFORMED, manual; no code)

These are environment steps the user runs; document exact commands in `README.md`
(Phase 3). The implementing agent does NOT need these done to write/test code
(all API calls are mocked in tests), but they're required before the live
verification in Phase 3.

- [ ] **Enable APIs** in the Google Cloud project behind each channel's OAuth
      client: *YouTube Data API v3* and *YouTube Analytics API*.
- [ ] **Re-consent each channel** (after Task 1 ships) to grant read scopes:
      `uv run scripts/yt_init.py --channel wordstrata` (repeat for
      `the-mythscape`, `open-verdicts`, `bright-beasts`). Each opens a browser;
      sign in as that channel's owner. Existing upload capability is retained.
- [ ] **Telegram:** message `@BotFather` → `/newbot` → save the bot token. Send
      any message to the new bot, then `GET https://api.telegram.org/bot<TOKEN>/getUpdates`
      and read `result[].message.chat.id` → save the chat ID.
- [ ] **Google Sheets:** create a spreadsheet (e.g. "n8n-shorts analytics") with
      a tab named `daily`. Create a Google Cloud **service account** + JSON key;
      **share the spreadsheet** with the service account's email (Editor). Add
      the header row to `daily` (column order in Task 8).

---

## Phase 1 — FastAPI analytics backend

### Task 1: Widen OAuth scopes + promote credentials helper

**Files:**
- Modify: `api/services/youtube.py:24` (SCOPES) and `:35` (`_credentials`), `:84` (caller)
- Modify: `scripts/yt_init.py:46` (SCOPES)
- Test: `api/test_analytics.py`

- [ ] **Step 1: Write the failing test**

Create `api/test_analytics.py` with:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project api pytest api/test_analytics.py::test_oauth_scopes_include_analytics_read -v`
Expected: FAIL — `AssertionError` (analytics scope missing) or `AttributeError: credentials`.

- [ ] **Step 3: Widen scopes and promote the helper**

In `api/services/youtube.py` replace the `SCOPES` constant (line 24):

```python
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/youtube.readonly",
]
```

Rename `def _credentials(channel: str)` to `def credentials(channel: str)` (line 35),
and update its one caller in `upload_short` (line 84) from
`creds = _credentials(channel)` to `creds = credentials(channel)`.

In `scripts/yt_init.py` replace `SCOPES` (line 46) with the identical list above.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project api pytest api/test_analytics.py::test_oauth_scopes_include_analytics_read -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add api/services/youtube.py scripts/yt_init.py api/test_analytics.py
git commit -m "feat(youtube): widen OAuth scopes for analytics read access

Add yt-analytics.readonly + youtube.readonly to the per-channel scope set
(upload retained) and promote _credentials to a public credentials() helper
for reuse by the analytics service. Re-run yt_init.py per channel to re-consent.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `db.uploaded_video_ids(channel)` helper

**Files:**
- Modify: `api/db.py` (add helper near `next_pending_word`, ~line 268)
- Test: `api/test_analytics.py`

- [ ] **Step 1: Write the failing test**

Append to `api/test_analytics.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project api pytest api/test_analytics.py::test_uploaded_video_ids_filters_to_done_with_video -v`
Expected: FAIL — `AttributeError: module 'db' has no attribute 'uploaded_video_ids'`.

- [ ] **Step 3: Implement the helper**

Add to `api/db.py` (after `next_pending_word`, ~line 281):

```python
def uploaded_video_ids(channel: str) -> list[str]:
    """YouTube video IDs of this channel's successfully-uploaded shorts.

    The canonical 'our content' list — newest first. Drives per-video stats
    so we never scan the channel's entire upload history.
    """
    with conn() as c:
        rows = c.execute(
            """
            SELECT youtube_video_id FROM runs
            WHERE channel = ?
              AND status = 'done'
              AND youtube_video_id IS NOT NULL
            ORDER BY started_at DESC
            """,
            (channel,),
        ).fetchall()
    return [r["youtube_video_id"] for r in rows]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project api pytest api/test_analytics.py::test_uploaded_video_ids_filters_to_done_with_video -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add api/db.py api/test_analytics.py
git commit -m "feat(db): add uploaded_video_ids() query helper

Returns a channel's done-run YouTube video IDs (newest first), the source of
truth for per-video analytics.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Analytics response models

**Files:**
- Modify: `api/models.py` (append at end, after `UploadResponse`)
- Test: covered transitively by Task 4–6 service tests (these are plain data
  shapes with no logic; a dedicated test would only assert field names).

- [ ] **Step 1: Add the models**

Append to `api/models.py`:

```python
# ─── Analytics ────────────────────────────────────────────────────────────────

class ChannelSnapshot(BaseModel):
    """Lifetime channel totals (YouTube Data API, point-in-time)."""
    subscribers: int
    total_views: int
    video_count: int


class PeriodMetrics(BaseModel):
    """Time-ranged channel metrics (YouTube Analytics API)."""
    days: int
    subscribers_gained: int
    subscribers_lost: int
    new_subscribers: int            # gained - lost
    estimated_minutes_watched: int
    views: int
    likes: int
    comments: int
    average_view_duration_s: int


class VideoAnalytics(BaseModel):
    """Lifetime cumulative stats for one uploaded short (Data API)."""
    video_id: str
    views: int
    likes: int
    comments: int


class ChannelAnalytics(BaseModel):
    channel: str
    snapshot: ChannelSnapshot
    new_subs_1d: int                # subscribers gained-lost, yesterday
    period: PeriodMetrics           # trailing `days` window (default 30)
    videos_uploaded: int            # count of our uploaded shorts
    avg_likes_per_video: float      # lifetime cumulative across our shorts
    avg_comments_per_video: float
    videos: list[VideoAnalytics]
    error: str | None = None


class DailyAnalyticsReport(BaseModel):
    date: str                       # report run date, YYYY-MM-DD
    days: int
    channels: list[ChannelAnalytics]
    errors: list[str] = Field(default_factory=list)
```

`Field` is already imported at the top of `models.py`.

- [ ] **Step 2: Verify it imports**

Run: `uv run --project api python -c "import sys; sys.path.insert(0,'api'); from models import DailyAnalyticsReport, ChannelAnalytics; print('ok')"`
Expected: prints `ok`

- [ ] **Step 3: Commit**

```bash
git add api/models.py
git commit -m "feat(models): add analytics response schemas

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Analytics service — snapshot, period, per-video

**Files:**
- Create: `api/services/analytics.py`
- Test: `api/test_analytics.py`

- [ ] **Step 1: Write the failing tests**

Append to `api/test_analytics.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --project api pytest api/test_analytics.py -k "snapshot or period or per_video" -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.analytics'`.

- [ ] **Step 3: Create the service**

Create `api/services/analytics.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --project api pytest api/test_analytics.py -k "snapshot or period or per_video" -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add api/services/analytics.py api/test_analytics.py
git commit -m "feat(analytics): YouTube snapshot, period, and per-video fetchers

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Per-channel rollup `channel_analytics`

**Files:**
- Modify: `api/services/analytics.py`
- Test: `api/test_analytics.py`

- [ ] **Step 1: Write the failing test**

Append to `api/test_analytics.py`:

```python
def test_channel_analytics_rolls_up(monkeypatch):
    from services import analytics
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
    ya = MagicMock()
    ya.reports.return_value.query.return_value.execute.return_value = {
        "rows": [[210, 10, 3420, 8000, 450, 96, 41]]
    }
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


def test_channel_analytics_empty_channel(monkeypatch):
    from services import analytics
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --project api pytest api/test_analytics.py -k channel_analytics -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'channel_analytics'`.

- [ ] **Step 3: Implement the rollup**

Append to `api/services/analytics.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --project api pytest api/test_analytics.py -k channel_analytics -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add api/services/analytics.py api/test_analytics.py
git commit -m "feat(analytics): per-channel rollup with 1d + 30d windows

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Daily report builder with per-channel error isolation

**Files:**
- Modify: `api/services/analytics.py`
- Test: `api/test_analytics.py`

- [ ] **Step 1: Write the failing test**

Append to `api/test_analytics.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project api pytest api/test_analytics.py::test_build_daily_report_isolates_channel_failure -v`
Expected: FAIL — `AttributeError: ... 'build_daily_report'`.

- [ ] **Step 3: Implement the builder**

Append to `api/services/analytics.py`:

```python
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
```

- [ ] **Step 4: Run the full analytics test file**

Run: `uv run --project api pytest api/test_analytics.py -v`
Expected: PASS (all tests across Tasks 1–6)

- [ ] **Step 5: Commit**

```bash
git add api/services/analytics.py api/test_analytics.py
git commit -m "feat(analytics): daily report builder with per-channel error isolation

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Wire endpoints into `main.py`

**Files:**
- Modify: `api/main.py` (imports ~line 24-43; new section after Upload, ~line 438)

- [ ] **Step 1: Add imports**

In `api/main.py`, add to the `from models import (...)` block:
`ChannelAnalytics,` and `DailyAnalyticsReport,`. Add to the service imports
(after `from services import youtube as youtube_svc`):

```python
from services import analytics as analytics_svc
```

- [ ] **Step 2: Add the endpoints**

Insert after the Upload section (before `# ─── Startup ───`):

```python
# ─── Analytics ────────────────────────────────────────────────────────────────

@app.get("/analytics/daily", response_model=DailyAnalyticsReport)
async def analytics_daily(days: int = 30) -> DailyAnalyticsReport:
    """All-channel daily digest. Called by the n8n analytics workflow."""
    channels = channel_registry.list_slugs()
    return await asyncio.to_thread(analytics_svc.build_daily_report, channels, days)


@app.get("/{channel}/analytics", response_model=ChannelAnalytics)
async def analytics_channel(channel: str, days: int = 30) -> ChannelAnalytics:
    _resolve_channel(channel)
    return await asyncio.to_thread(analytics_svc.channel_analytics, channel, days)
```

(`/analytics/daily` is declared first; its literal path can't be captured by
`/{channel}/analytics` since the second segment differs, but declaring it first
is the safe convention.)

- [ ] **Step 3: Verify the app imports cleanly**

Run: `uv run --project api python -c "import sys; sys.path.insert(0,'api'); import main; print([r.path for r in main.app.routes if 'analytics' in r.path])"`
Expected: lists `/analytics/daily` and `/{channel}/analytics`

- [ ] **Step 4: Smoke-test the route with mocked service**

Add this test to `api/test_analytics.py` instead of a one-liner (cleaner and
permanent):

```python
def test_analytics_daily_endpoint(monkeypatch):
    import sys
    sys.path.insert(0, "api")
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
```

Run: `uv run --project api pytest api/test_analytics.py::test_analytics_daily_endpoint -v`
Expected: PASS (`httpx`, which `TestClient` needs, is already a dependency).

- [ ] **Step 5: Commit**

```bash
git add api/main.py
git commit -m "feat(api): add /analytics/daily and /{channel}/analytics endpoints

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Phase 2 — n8n workflow

### Task 8: Build the "Daily Analytics Digest" workflow

**Tooling:** Use the n8n-mcp server. Follow its required order:
`get_sdk_reference` → `get_suggested_nodes` → `search_nodes` → `get_node_types`
(for schedule trigger, HTTP Request, Code, Google Sheets, Telegram) →
`validate_workflow` → `create_workflow_from_code`. Verify exact node param names
via `get_node_types` — do not guess.

**Credentials (create in n8n UI first):**
- Google service account credential (Sheets) — from the Phase 0 JSON key.
- Telegram API credential — the Phase 0 bot token.

**Node graph:**
```
Schedule Trigger (daily 06:00)
  → HTTP Request  (GET http://host.docker.internal:7860/analytics/daily?days=30, timeout 120s)
       → Code "Split to rows"  → Google Sheets (append, sheet "daily")
       → Code "Format digest"  → Telegram (sendMessage)
  (HTTP Request error output) → Code "Format error" → Telegram (sendMessage)
```

- [ ] **Step 1: Read the SDK reference**

Call `get_sdk_reference` (sections: default, `guidelines`, `design`). Do not
write workflow code before this.

- [ ] **Step 2: Discover + type the nodes**

Call `get_suggested_nodes` for the relevant categories, then `search_nodes`
for `["schedule trigger","http request","code","google sheets","telegram"]`,
then `get_node_types` for each chosen node ID (with discriminators, e.g. Google
Sheets `operation: append`, Telegram `operation: sendMessage`).

- [ ] **Step 3: Author the workflow code**

Build per the graph above. Use these exact Code-node bodies.

**Code "Split to rows"** (runs once; emits 4 items → 4 Sheet rows):

```javascript
const report = $input.first().json;
return report.channels.map((c) => ({
  json: {
    date: report.date,
    channel: c.channel,
    total_subscribers: c.snapshot.subscribers,
    new_subs_1d: c.new_subs_1d,
    new_subs_30d: c.period.new_subscribers,
    total_views: c.snapshot.total_views,
    videos_uploaded: c.videos_uploaded,
    watch_time_min_30d: c.period.estimated_minutes_watched,
    avg_view_duration_s: c.period.average_view_duration_s,
    avg_likes_per_video: c.avg_likes_per_video,
    avg_comments_per_video: c.avg_comments_per_video,
    likes_30d: c.period.likes,
    comments_30d: c.period.comments,
  },
}));
```

**Code "Format digest"** (runs once; one Telegram message):

```javascript
const report = $input.first().json;
const fmt = (n) => Number(n).toLocaleString("en-US");
const dur = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
const emoji = {
  wordstrata: "🔵", "the-mythscape": "🟣",
  "open-verdicts": "🟠", "bright-beasts": "🟢",
};
let msg = `📊 Daily Analytics — ${report.date}\n`;
for (const c of report.channels) {
  msg += `\n${emoji[c.channel] || "▫️"} ${c.channel}\n`;
  msg += ` Subs ${fmt(c.snapshot.subscribers)} (+${c.new_subs_1d} today · +${c.period.new_subscribers}/${c.period.days}d)\n`;
  msg += ` Watch ${fmt(c.period.estimated_minutes_watched)} min/${c.period.days}d · avg ${dur(c.period.average_view_duration_s)}\n`;
  msg += ` Per short: 👍 ${c.avg_likes_per_video} · 💬 ${c.avg_comments_per_video} (across ${c.videos_uploaded} shorts)\n`;
}
if (report.errors && report.errors.length) {
  msg += `\n⚠️ Errors: ${report.errors.join("; ")}`;
}
return [{ json: { text: msg } }];
```

**Code "Format error"** (on HTTP failure):

```javascript
const err = $input.first().json;
const reason = err.error || err.message || JSON.stringify(err).slice(0, 300);
return [{ json: { text: `⚠️ Analytics digest failed: ${reason}` } }];
```

Node config notes:
- **Schedule Trigger:** daily at hour 6.
- **HTTP Request:** method GET, URL
  `http://host.docker.internal:7860/analytics/daily?days=30`, response format
  JSON, timeout 120000 ms, `onError` → continue with error output wired to
  "Format error".
- **Google Sheets:** operation `append`, the spreadsheet + `daily` sheet,
  mapping mode = auto-map by column name (input keys match the header row).
- **Telegram:** operation `sendMessage`, chat ID = the Phase 0 chat ID, text =
  `={{ $json.text }}`. (Two Telegram nodes share the credential: success digest
  and error alert.)

- [ ] **Step 4: Validate**

Call `validate_workflow` with the full code. Fix every error/warning and
re-validate until clean.

- [ ] **Step 5: Create**

Call `create_workflow_from_code` with `description`:
"Daily 06:00 analytics digest — pulls /analytics/daily for all 4 channels,
appends a row per channel to the Google Sheet, and posts a Telegram summary
(with an error alert on failure)." Record the returned workflow ID.

- [ ] **Step 6: Commit (no repo file changes; record the ID)**

The workflow lives in n8n, not git. Note the workflow ID in the PR description.
If the repo's `n8n/generate.py` pattern should own this workflow as committed
JSON, that is a follow-up — the 4 existing pipelines are generated, but this
analytics workflow is standalone and created via MCP.

---

## Phase 3 — Docs + verification

### Task 9: Update README and AGENTS docs

**Files:**
- Modify: `README.md`, `AGENTS.md`

- [ ] **Step 1: README — add an "Analytics digest" section**

Document: what it does (daily Telegram digest + Google Sheet history); the
metrics; and the Phase 0 setup steps verbatim (enable APIs, re-run
`yt_init.py --channel <slug>` per channel for read scopes, BotFather token +
chat ID, service account + shared sheet + `daily` tab header row). List the
header columns (Task 8 "Split to rows" key order).

- [ ] **Step 2: AGENTS.md — add operational notes**

Add to the relevant tables/sections: `api/services/analytics.py` (purpose +
how to test: `uv run --project api pytest api/test_analytics.py`), the two new
endpoints, the widened OAuth scopes (and that re-consent is required once per
channel), and the new n8n "Daily Analytics Digest" workflow + its 06:00 slot
(after the 01–04:00 upload runs).

- [ ] **Step 3: Commit**

```bash
git add README.md AGENTS.md
git commit -m "docs: document daily analytics digest feature and setup

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: End-to-end verification (after Phase 0 done on the host)

- [ ] **Step 1: Full test suite green**

Run: `uv run --project api pytest api/ -v`
Expected: all tests pass (existing + `test_analytics.py`), zero new warnings.

- [ ] **Step 2: Live endpoint (requires re-consent done)**

Restart the service (see AGENTS.md restart note), then:
Run: `curl -sS 'http://localhost:7860/analytics/daily?days=30' | jq '.date, (.channels|length), .errors'`
Expected: today's date, `4`, and `[]` (or named errors to triage). If a channel
shows in `errors` with a 403/scope message, its re-consent didn't take — re-run
`yt_init.py --channel <slug>`.

- [ ] **Step 3: Manual workflow run**

In n8n, execute "Daily Analytics Digest" once manually. Confirm:
  - 4 rows appended to the `daily` sheet tab with sane values.
  - One Telegram digest message received, formatted as designed.
  - Stop the service / break the URL once to confirm the error path posts the
    `⚠️ Analytics digest failed` alert, then restore.

- [ ] **Step 4: Activate**

Toggle the workflow active so the 06:00 schedule runs daily.

---

## Self-Review (completed by plan author)

- **Spec coverage:** subscribers/new subs → Tasks 4–5 (`snapshot`, `period`,
  `new_subs_1d`); likes/comments per video → Tasks 4–5 (`per_video`, averages);
  30-day watch time → Task 4 (`period.estimated_minutes_watched`); Telegram +
  Sheets daily → Task 8; storage layout → Task 8 + Phase 0; re-consent/scopes →
  Task 1 + Phase 0; error isolation → Task 6 + Task 8 error path; docs → Task 9;
  verification → Task 10. All spec sections mapped.
- **Placeholder scan:** none — every code/test step is complete.
- **Type consistency:** model field names (`snapshot.subscribers`,
  `period.estimated_minutes_watched`, `period.new_subscribers`, `new_subs_1d`,
  `avg_likes_per_video`, `videos_uploaded`) are identical across models.py,
  analytics.py, the endpoint, and the n8n Code nodes. `credentials()` (not
  `_credentials`) used consistently after Task 1. `_PERIOD_METRICS` column order
  matches the unpack order in `period_metrics` and the test fixtures.
