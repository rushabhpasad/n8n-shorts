# Per-Video Daily Stats Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture per-video daily statistics (views/likes/comments/watch-time/shares — cumulative + daily delta) for every published Short and append them, one row per video per day, to a per-channel tab in the existing analytics Google Sheet.

**Architecture:** Add a `video_snapshots` table to `state.db` mirroring the existing `analytics_snapshots`. A new `channel_video_stats()` function in `api/services/analytics.py` fetches per-video cumulative numbers (views/likes/comments from the Data API; watch-time/shares from the Analytics API), diffs against yesterday's stored snapshot to compute daily deltas, persists today's snapshot, and returns rows. A new `GET /analytics/videos` endpoint serves all channels. The existing live n8n "Daily Analytics Digest" workflow (`ENbQm9ctfNRcnOuT`) gets a second branch that fans each channel's rows into its own auto-created tab.

**Tech Stack:** Python 3 / FastAPI, SQLite (stdlib `sqlite3`), Pydantic v2, `google-api-python-client`, pytest, n8n (live REST patch).

**Spec:** `docs/superpowers/specs/2026-06-17-per-video-daily-stats-design.md`

**Conventions (match existing analytics code):**
- All Google API calls go through functions that accept an optional injected `client` (`data_client` / `analytics_client`) so tests mock the network with `MagicMock` — no network, no credentials.
- DB tests use `monkeypatch.setattr(db.settings, "db_path", tmp_path / "t.db")` then `db.init_schema()`.
- Run tests from the `api/` directory: `cd api && python -m pytest`.
- Per-channel failures are isolated into an `errors[]` list, never aborting the whole report (see `build_daily_report`).

---

### Task 1: `video_snapshots` table in schema

**Files:**
- Modify: `sql/schema.sql` (append new table after `analytics_snapshots`, ~line 63)
- Test: `api/test_video_stats.py` (new file)

**Why no migration/version bump:** `db.init_schema()` re-runs the full `schema.sql` (via `executescript`) on existing DBs as well as fresh ones, and every table uses `CREATE TABLE IF NOT EXISTS`. Adding a new `IF NOT EXISTS` table is therefore picked up automatically on next startup with **no** `TARGET_USER_VERSION` bump and no `_MIGRATIONS` entry — exactly how `analytics_snapshots` was added.

- [ ] **Step 1: Write the failing test**

Create `api/test_video_stats.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && python -m pytest test_video_stats.py::test_video_snapshots_table_exists_and_is_insertable -v`
Expected: FAIL — `sqlite3.OperationalError: no such table: video_snapshots`

- [ ] **Step 3: Add the table to `sql/schema.sql`**

Append after the `analytics_snapshots` table (after line 63):

```sql

-- One row per video per analytics run. Time-series store powering per-video
-- daily-delta columns in each channel's Google Sheets tab. Cumulative numbers
-- come from two APIs: views/likes/comments (Data API, real-time) and
-- watch_minutes/shares (Analytics API, ~1-2 day lag). Deltas are computed
-- against the most recent prior row (spans gaps if a day was missed).
CREATE TABLE IF NOT EXISTS video_snapshots (
    channel        TEXT    NOT NULL,
    video_id       TEXT    NOT NULL,
    date           TEXT    NOT NULL,   -- YYYY-MM-DD snapshot date
    views          INTEGER NOT NULL DEFAULT 0,   -- cumulative lifetime
    likes          INTEGER NOT NULL DEFAULT 0,
    comments       INTEGER NOT NULL DEFAULT 0,
    watch_minutes  INTEGER NOT NULL DEFAULT 0,    -- cumulative (Analytics API)
    shares         INTEGER NOT NULL DEFAULT 0,    -- cumulative (Analytics API)
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, video_id, date)
);

CREATE INDEX IF NOT EXISTS idx_video_snapshots_lookup
    ON video_snapshots(channel, video_id, date DESC);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd api && python -m pytest test_video_stats.py::test_video_snapshots_table_exists_and_is_insertable -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sql/schema.sql api/test_video_stats.py
git commit -m "feat(analytics): add video_snapshots table for per-video daily stats"
```

---

### Task 2: DB helpers `record_video_snapshot` + `video_snapshot_before`

**Files:**
- Modify: `api/db.py` (add after `snapshot_before`, ~line 369, in the "Analytics snapshots" section)
- Test: `api/test_video_stats.py`

- [ ] **Step 1: Write the failing tests**

Append to `api/test_video_stats.py`:

```python
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
    prior = db.video_snapshot_before("wordstrata", "2026-06-17")
    assert (prior["date"], prior["views"], prior["shares"]) == ("2026-06-16", 180, 9)

    prior2 = db.video_snapshot_before("wordstrata", "2026-06-16")
    assert prior2["views"] == 100  # 06-15 row

    # no prior snapshot → None
    assert db.video_snapshot_before("wordstrata", "2026-06-15") is None
    assert db.video_snapshot_before("wordstrata", "2026-06-17", ) is not None
    assert db.video_snapshot_before("the-mythscape", "2026-06-17") is None  # channel-scoped


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd api && python -m pytest test_video_stats.py -k video_snapshot -v`
Expected: FAIL — `AttributeError: module 'db' has no attribute 'record_video_snapshot'`

- [ ] **Step 3: Implement the helpers**

In `api/db.py`, immediately after `snapshot_before` (after line 369), add:

```python
def record_video_snapshot(
    channel: str,
    video_id: str,
    *,
    snapshot_date: str,
    views: int,
    likes: int,
    comments: int,
    watch_minutes: int,
    shares: int,
) -> None:
    """Upsert one video's cumulative totals for `snapshot_date` (idempotent per
    channel/video/day)."""
    with conn() as c:
        c.execute(
            """
            INSERT OR REPLACE INTO video_snapshots
                (channel, video_id, date, views, likes, comments, watch_minutes, shares)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (channel, video_id, snapshot_date, views, likes, comments, watch_minutes, shares),
        )


def video_snapshot_before(channel: str, video_id: str, snapshot_date: str) -> dict | None:
    """Most recent snapshot for (channel, video_id) strictly earlier than
    `snapshot_date`, or None if this video has no prior snapshot. Used to compute
    daily deltas; correctly spans gaps when a day was missed."""
    with conn() as c:
        row = c.execute(
            """
            SELECT * FROM video_snapshots
            WHERE channel = ? AND video_id = ? AND date < ?
            ORDER BY date DESC
            LIMIT 1
            """,
            (channel, video_id, snapshot_date),
        ).fetchone()
        return dict(row) if row else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd api && python -m pytest test_video_stats.py -k video_snapshot -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add api/db.py api/test_video_stats.py
git commit -m "feat(analytics): add record_video_snapshot + video_snapshot_before db helpers"
```

---

### Task 3: Pydantic models `VideoStatRow`, `ChannelVideoStats`, `VideoStatsReport`

**Files:**
- Modify: `api/models.py` (append after `DailyAnalyticsReport`, ~line 294)
- Test: `api/test_video_stats.py`

- [ ] **Step 1: Write the failing test**

Append to `api/test_video_stats.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && python -m pytest test_video_stats.py::test_video_stat_row_and_report_models -v`
Expected: FAIL — `ImportError: cannot import name 'VideoStatRow'`

- [ ] **Step 3: Add the models**

In `api/models.py`, after `DailyAnalyticsReport` (after line 294), add:

```python
class VideoStatRow(BaseModel):
    """One video's stats for one snapshot date — a single row in a channel tab.
    `*_total` are cumulative lifetime; `*_today` are the delta vs the prior
    snapshot (== total on the first-ever snapshot for the video)."""
    date: str                       # snapshot date, YYYY-MM-DD
    video_id: str
    url: str
    title: str
    published_at: str               # ISO 8601 from the Data API snippet
    days_live: int                  # whole days from publish to snapshot date
    views_total: int
    views_today: int
    likes_total: int
    likes_today: int
    comments_total: int
    comments_today: int
    watch_min_total: int            # Analytics API (lags ~1-2 days)
    watch_min_today: int
    shares_total: int               # Analytics API (lags ~1-2 days)
    shares_today: int


class ChannelVideoStats(BaseModel):
    """All per-video rows for one channel (one channel = one Sheets tab)."""
    channel: str
    rows: list[VideoStatRow] = Field(default_factory=list)


class VideoStatsReport(BaseModel):
    """All-channel per-video stats for one run. The n8n Code node flattens
    `channels[].rows[]`, routing each row to a tab named after its channel."""
    date: str                       # run date, YYYY-MM-DD
    channels: list[ChannelVideoStats]
    errors: list[str] = Field(default_factory=list)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd api && python -m pytest test_video_stats.py::test_video_stat_row_and_report_models -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add api/models.py api/test_video_stats.py
git commit -m "feat(analytics): add VideoStatRow / ChannelVideoStats / VideoStatsReport models"
```

---

### Task 4: `video_details()` — Data API per-video snippet+statistics

**Files:**
- Modify: `api/services/analytics.py` (add after `per_video`, ~line 133)
- Test: `api/test_video_stats.py`

**Why a new function (not extend `per_video`):** `per_video` returns `VideoAnalytics` (statistics only) and is consumed by `channel_analytics`. We need `title` + `publishedAt` from the `snippet` part too, keyed by id for lookup. Keep `per_video` untouched; add a sibling that returns a `{video_id: {...}}` dict.

- [ ] **Step 1: Write the failing test**

Append to `api/test_video_stats.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd api && python -m pytest test_video_stats.py -k video_details -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'video_details'`

- [ ] **Step 3: Implement `video_details`**

In `api/services/analytics.py`, after `per_video` (after line 133), add:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd api && python -m pytest test_video_stats.py -k video_details -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add api/services/analytics.py api/test_video_stats.py
git commit -m "feat(analytics): add video_details() Data API helper (snippet+statistics)"
```

---

### Task 5: `video_period_metrics()` — Analytics API per-video watch-time + shares

**Files:**
- Modify: `api/services/analytics.py` (add constant near line 39; function after `video_details`)
- Test: `api/test_video_stats.py`

**API shape:** `reports().query(ids="channel==MINE", startDate, endDate, metrics="estimatedMinutesWatched,shares", dimensions="video", filters="video==id1,id2,...", maxResults=...)`. Rows come back as `[[videoId, minutes, shares], ...]`. We batch the `video==` filter by 200 ids (well under the 500-id filter cap); each batch's row count ≤ its id count, so no per-page `startIndex` paging is needed.

- [ ] **Step 1: Write the failing tests**

Append to `api/test_video_stats.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd api && python -m pytest test_video_stats.py -k video_period_metrics -v`
Expected: FAIL — `AttributeError: ... 'video_period_metrics'`

- [ ] **Step 3: Implement constant + function**

In `api/services/analytics.py`, add the batch constant next to `_VIDEOS_BATCH` (after line 39):

```python
_VIDEO_ANALYTICS_BATCH = 200  # video== filter id cap is 500; 200 keeps rows == ids
```

Then add after `video_details`:

```python
def video_period_metrics(
    channel: str, start: str, end: str, video_ids: list[str], *, client=None,
) -> dict[str, dict]:
    """Per-video cumulative watch_minutes + shares over [start, end], keyed by
    video id. Filter is batched by 200 ids. Videos absent from the response
    (e.g. brand-new, no Analytics data yet) are simply omitted — the caller
    defaults them to 0."""
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
            metrics="estimatedMinutesWatched,shares",
            dimensions="video",
            filters="video==" + ",".join(batch),
            maxResults=_VIDEO_ANALYTICS_BATCH,
        ).execute()
        for row in (resp.get("rows") or []):
            out[str(row[0])] = {"watch_minutes": int(row[1]), "shares": int(row[2])}
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd api && python -m pytest test_video_stats.py -k video_period_metrics -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add api/services/analytics.py api/test_video_stats.py
git commit -m "feat(analytics): add video_period_metrics() Analytics API helper (watch+shares)"
```

---

### Task 6: `channel_video_stats()` — assemble rows with daily deltas

**Files:**
- Modify: `api/services/analytics.py` (add after `video_period_metrics`)
- Test: `api/test_video_stats.py`

**Delta rule:** `*_today = max(0, today_cumulative - prior_cumulative)`; first-ever snapshot (no prior row) → `*_today == *_total`. `days_live = max(0, (snapshot_date - published_date).days)` where `published_date = published_at[:10]` (empty publish date → `days_live = 0`). Persist today's cumulative AFTER reading the prior row. Rows ordered by `uploaded_video_ids` order (newest upload first).

- [ ] **Step 1: Write the failing tests**

Append to `api/test_video_stats.py`:

```python
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
    assert r.days_live == 7                                    # 06-10 → 06-17
    # snapshot persisted for tomorrow's delta
    assert db.video_snapshot_before("wordstrata", "2026-06-18")["views"] == 180


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd api && python -m pytest test_video_stats.py -k channel_video_stats -v`
Expected: FAIL — `AttributeError: ... 'channel_video_stats'`

- [ ] **Step 3: Implement `channel_video_stats`**

First add `ChannelVideoStats` and `VideoStatRow` to the model imports at the top of `api/services/analytics.py` (the `from models import (...)` block, lines 19-29):

```python
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
    YppProgress,
)
```

Then add after `video_period_metrics`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd api && python -m pytest test_video_stats.py -k channel_video_stats -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add api/services/analytics.py api/test_video_stats.py
git commit -m "feat(analytics): add channel_video_stats() with per-video daily deltas"
```

---

### Task 7: `all_video_stats()` — all-channel rollup with error isolation

**Files:**
- Modify: `api/services/analytics.py` (add after `channel_video_stats`)
- Test: `api/test_video_stats.py`

- [ ] **Step 1: Write the failing test**

Append to `api/test_video_stats.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && python -m pytest test_video_stats.py::test_all_video_stats_isolates_channel_failure -v`
Expected: FAIL — `AttributeError: ... 'all_video_stats'`

- [ ] **Step 3: Implement `all_video_stats`**

Add `VideoStatsReport` to the model import block in `api/services/analytics.py` (alongside `VideoStatRow`):

```python
    VideoStatRow,
    VideoStatsReport,
    YppProgress,
```

Then add after `channel_video_stats`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd api && python -m pytest test_video_stats.py::test_all_video_stats_isolates_channel_failure -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add api/services/analytics.py api/test_video_stats.py
git commit -m "feat(analytics): add all_video_stats() all-channel rollup"
```

---

### Task 8: API endpoints `GET /analytics/videos` and `GET /{channel}/analytics/videos`

**Files:**
- Modify: `api/main.py` (in the "Analytics" section, after the digest endpoint ~line 465, BEFORE the `/{channel}/analytics` route at line 470)
- Test: `api/test_video_stats.py`

**Route-ordering rule (critical):** `/analytics/videos` is a literal path and must be declared **before** the `/{channel}` wildcard routes, exactly like `/analytics/daily`. The channel-scoped `/{channel}/analytics/videos` is more specific than `/{channel}/analytics` is fine after it, but to be safe declare it right next to the all-channel route, still before the bare `/{channel}/analytics`. FastAPI matches in declaration order.

- [ ] **Step 1: Write the failing tests**

Append to `api/test_video_stats.py`:

```python
def test_analytics_videos_endpoint(monkeypatch):
    from fastapi.testclient import TestClient
    import main
    from models import VideoStatsReport, ChannelVideoStats
    from services import analytics

    monkeypatch.setattr(
        analytics, "all_video_stats",
        lambda channels, **kw: VideoStatsReport(
            date="2026-06-17",
            channels=[ChannelVideoStats(channel="wordstrata", rows=[])],
            errors=[]),
    )
    client = TestClient(main.app)
    resp = client.get("/analytics/videos")
    assert resp.status_code == 200
    body = resp.json()
    assert body["date"] == "2026-06-17"
    assert body["channels"][0]["channel"] == "wordstrata"


def test_analytics_videos_channel_unknown_returns_404():
    from fastapi.testclient import TestClient
    import main
    client = TestClient(main.app)
    resp = client.get("/definitely-not-a-channel/analytics/videos")
    assert resp.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd api && python -m pytest test_video_stats.py -k analytics_videos -v`
Expected: FAIL — `/analytics/videos` returns 404 (route not defined) / unknown-channel test errors

- [ ] **Step 3: Add the endpoints**

In `api/main.py`, insert AFTER the `/analytics/daily/digest` endpoint (after line 465) and BEFORE the `# NOTE: must stay declared AFTER...` comment at line 468:

```python
@app.get("/analytics/videos", response_model=VideoStatsReport)
async def analytics_videos() -> VideoStatsReport:
    """Per-video daily stats for every channel (one tab per channel in Sheets).
    Called by the n8n analytics workflow's video-stats branch."""
    channels = channel_registry.list_slugs()
    return await asyncio.to_thread(analytics_svc.all_video_stats, channels)


@app.get("/{channel}/analytics/videos", response_model=ChannelVideoStats)
async def analytics_videos_channel(channel: str) -> ChannelVideoStats:
    """Single-channel per-video stats (debugging parity with /{channel}/analytics)."""
    _resolve_channel(channel)
    return await asyncio.to_thread(analytics_svc.channel_video_stats, channel)
```

Then add `VideoStatsReport` and `ChannelVideoStats` to the `from models import ...` block at the top of `api/main.py`. (Locate the existing models import; add both names alphabetically/where they fit.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd api && python -m pytest test_video_stats.py -k analytics_videos -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run the full test suite (no regressions)**

Run: `cd api && python -m pytest -v`
Expected: PASS — all prior analytics tests plus the new `test_video_stats.py` suite green.

- [ ] **Step 6: Commit**

```bash
git add api/main.py api/test_video_stats.py
git commit -m "feat(analytics): add /analytics/videos and /{channel}/analytics/videos endpoints"
```

---

### Task 9: Add the video-stats branch to the live n8n workflow

**Files:**
- Create: `scripts/add_video_stats_branch.py` (live REST patch, modeled on `scripts/patch_telegram_parsemode.py`)
- Reference (read first): `scripts/patch_telegram_parsemode.py`

**Context & gotchas (from project memory):**
- Workflow `ENbQm9ctfNRcnOuT` ("Daily Analytics Digest") is **live-only** — not in `n8n/generate.py`. Patch it via REST.
- Auth: `N8N_API_TOKEN` env var (already used by the existing patch script).
- n8n `PUT /workflows/{id}` **rejects newer `settings` keys** present in the GET response — filter the settings dict at payload-build time and log dropped keys (the existing script already does this; reuse that helper).
- `settings.errorWorkflow` is silently dropped by the API — it's set in the UI, leave it alone.
- The workflow must be **re-published** after update for the change to go live.
- n8n runs in Docker; it reaches the host API at `http://host.docker.internal:7860`.

- [ ] **Step 1: Recon — dump the live workflow and find the existing Sheets node**

Run (via the n8n MCP `get_workflow_details` tool, or REST):

```bash
curl -s -H "X-N8N-API-KEY: $N8N_API_TOKEN" \
  "$N8N_BASE_URL/api/v1/workflows/ENbQm9ctfNRcnOuT" | python -m json.tool > /tmp/$(uuidgen)/digest_workflow.json
```

Identify and record from the existing **"daily" Sheets append** node:
- the Google Sheets **credential id + name**,
- the **documentId** (spreadsheet id),
- the node type version (e.g. `googleSheets` v4),
- the schedule-trigger node name (the new branch hangs off the same trigger).

These exact values are read from the live JSON — do not hardcode guesses.

- [ ] **Step 2: Write `scripts/add_video_stats_branch.py`**

The script must, idempotently (skip if a node named `"Get video stats"` already exists):

1. `GET` the workflow JSON.
2. Append three nodes, reusing the recon values:
   - **HTTP Request** "Get video stats" — `GET http://host.docker.internal:7860/analytics/videos`, response format JSON, generous timeout (e.g. 120000 ms).
   - **Code** "Split video rows" — flatten channels→rows, tagging each item with its channel for routing:
     ```javascript
     const out = [];
     for (const ch of ($json.channels || [])) {
       for (const row of (ch.rows || [])) {
         out.push({ json: { ...row, channel: ch.channel } });
       }
     }
     return out;
     ```
   - **Google Sheets** "Append video stats" — same credential + documentId as the daily node; `operation: append`; `sheetName` set dynamically from `={{ $json.channel }}`; mapping mode that auto-maps by header for the 16 data columns; enable the option that **creates the sheet/tab if missing** (in googleSheets v4 the append/lookup on a non-existent sheet is handled by the node's auto-create; if the installed version lacks it, the Code node prepends a "ensure tab" step — verify against the node version found in Step 1). The `channel` field is the routing key only and is NOT mapped as a column.
3. Wire connections: `<Schedule Trigger>` → "Get video stats" → "Split video rows" → "Append video stats". (The schedule trigger keeps its existing connection to the daily/digest branch; this adds a second outgoing edge.)
4. Build the `PUT` payload filtering disallowed `settings` keys (reuse the existing script's settings-filter helper; log any dropped keys).
5. `PUT` the workflow, then **publish** it (`publish_workflow` MCP tool or the REST activate/publish call the existing script uses).
6. Print a verification diff: re-`GET` and assert the three node names are present and the trigger now has two downstream branches.

- [ ] **Step 3: Dry-run, then apply**

Run the script in a dry-run mode first (print the mutated JSON without PUT), eyeball the new nodes/connections, then apply:

```bash
python scripts/add_video_stats_branch.py --dry-run
python scripts/add_video_stats_branch.py
```

Expected: dry-run prints 3 new nodes + connections; apply returns the updated workflow and a "published" confirmation.

- [ ] **Step 4: Verify in n8n**

Re-fetch the workflow and confirm:
- nodes "Get video stats", "Split video rows", "Append video stats" exist,
- the schedule trigger fans out to both the existing daily branch and the new one,
- existing Telegram/Slack/daily-Sheets nodes and their credentials are unchanged,
- `settings.errorWorkflow` still points to the Pipeline Error Alert workflow.

Optionally trigger the workflow manually once and confirm each channel tab is created and populated with one row per video.

- [ ] **Step 5: Commit**

```bash
git add scripts/add_video_stats_branch.py
git commit -m "feat(n8n): script to add per-video stats branch to live analytics workflow"
```

---

### Task 10: Documentation

**Files:**
- Modify: `README.md` (analytics section — document per-channel tabs, columns, lag note)
- Modify: `AGENTS.md` (new endpoint, table, workflow branch)

- [ ] **Step 1: Update `README.md`**

In the analytics section (near the existing `daily` tab column docs, README ~line 235), add:

```markdown
### Per-video daily stats (per-channel tabs)

In addition to the channel-level `daily` tab, the analytics workflow writes
per-video stats to **one tab per channel** (tab name == channel slug:
`wordstrata`, `the-mythscape`, `open-verdicts`, `bright-beasts`; new channels
auto-create their tab). One row is appended per video **per day** — a time
series for charting growth and daily reach.

Column order:

```
date | video_id | url | title | published_at | days_live |
views_total | views_today | likes_total | likes_today |
comments_total | comments_today | watch_min_total | watch_min_today |
shares_total | shares_today
```

`*_total` are cumulative lifetime; `*_today` are the day-over-day delta computed
by the backend against the prior snapshot. **Note:** `watch_min_*` and
`shares_*` come from the YouTube Analytics API, which lags ~1–2 days, so they
trail the real-time `views/likes/comments` (YouTube Data API) by a day.

Backed by `GET /analytics/videos` and the `video_snapshots` table in `state.db`.
```

- [ ] **Step 2: Update `AGENTS.md`**

Add to the relevant lists/sections:
- Endpoints: `GET /analytics/videos` (all channels, per-video daily stats) and `GET /{channel}/analytics/videos` (single channel, debug).
- Data stores: `video_snapshots` table — per-video cumulative snapshot per day; powers the `*_today` deltas; added via `schema.sql` with no `user_version` bump (re-run-on-startup, `IF NOT EXISTS`).
- n8n: the live "Daily Analytics Digest" workflow (`ENbQm9ctfNRcnOuT`) has a second branch writing per-video rows to per-channel tabs; patched via `scripts/add_video_stats_branch.py` (live-only workflow, not in `generate.py`).

- [ ] **Step 3: Commit**

```bash
git add README.md AGENTS.md
git commit -m "docs: per-video daily stats — endpoints, tabs, columns, video_snapshots table"
```

---

### Task 11: Integration — full suite, push, deploy

**Files:** none (verification + deploy)

- [ ] **Step 1: Run the full test suite**

Run: `cd api && python -m pytest -v`
Expected: all green (existing analytics suite + new `test_video_stats.py`).

- [ ] **Step 2: Push the branch and open a PR**

```bash
git push -u origin feat/per-video-daily-stats
gh pr create --title "feat: per-video daily stats → per-channel Sheets tabs" \
  --body "Implements docs/superpowers/specs/2026-06-17-per-video-daily-stats-design.md. Adds video_snapshots table, channel_video_stats/all_video_stats, GET /analytics/videos, and a live n8n branch writing per-video rows to per-channel tabs."
```

- [ ] **Step 3: After merge — sync STL and restart the API**

On the STL host: `git pull` in the n8n-shorts repo, then restart the uvicorn service (it picks up the new `schema.sql` table on startup and serves the new endpoint). Restart needs the Homebrew PATH for `uv`/`ffmpeg` (see the `stl-fastapi-service-restart` memory).

- [ ] **Step 4: Verify live**

```bash
curl -s "http://<stl-host>:7860/analytics/videos" | python -m json.tool | head -40
```
Expected: a `VideoStatsReport` with each channel's rows. Then confirm the n8n branch (Task 9) writes the per-channel tabs on its next 06:00 run (or a manual trigger).

---

## Self-Review

**Spec coverage:**
- Time-series append (one row/video/day) → Task 6 builds a dated row each run; Task 9 appends. ✓
- Backend-computed deltas + `video_snapshots` table → Tasks 1, 2, 6. ✓
- 16-column layout → Task 3 model + Task 10 docs. ✓
- watch-time + shares via Analytics API (with lag note) → Tasks 5, 6, 10. ✓
- Extend live workflow `ENbQm9ctfNRcnOuT`, auto-create tabs, tab-name-only routing → Task 9. ✓
- Per-channel error isolation → Task 7. ✓
- Tests (first/second/missed-day/missing-analytics/days_live/batching/error isolation) → Tasks 1-8. ✓
- Endpoints (all + per-channel debug) → Task 8. ✓
- Docs (README + AGENTS) → Task 10. ✓

**Placeholder scan:** No TBD/TODO. Task 9's Sheets-node version detail is intentionally read from the live workflow at Step 1 (live infra value), not a placeholder — the procedure to obtain it is explicit.

**Type consistency:** `channel_video_stats(channel, *, today, data_client, analytics_client)`, `all_video_stats(channels, *, today)`, `video_details(channel, ids, *, client)`, `video_period_metrics(channel, start, end, ids, *, client)`, `record_video_snapshot(channel, video_id, *, snapshot_date, views, likes, comments, watch_minutes, shares)`, `video_snapshot_before(channel, video_id, snapshot_date)` — names/signatures match across Tasks 2, 4, 5, 6, 7, 8 and the tests. Model field names (`views_total`/`views_today`/`watch_min_*`/`shares_*`) consistent between Task 3 and Task 6.

**Missed-day coverage note:** the missed-day delta is covered implicitly — `video_snapshot_before` selects the most recent prior row regardless of gap (Task 2 test inserts non-consecutive dates; Task 6 second-snapshot test diffs against a 06-16 row for a 06-17 run). Explicit gap test optional.
