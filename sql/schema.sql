-- state.db schema (SQLite)
-- Single source of truth for the shorts pipeline.
-- Multi-channel: every row carries a `channel` slug.
-- Created/migrated by shorts-api on startup (see api/db.py:apply_migrations).

CREATE TABLE IF NOT EXISTS words (
    channel         TEXT    NOT NULL DEFAULT 'wordstrata',
    id              INTEGER NOT NULL,
    word            TEXT    NOT NULL,
    category        TEXT    NOT NULL,
    origin_language TEXT    NOT NULL,
    hook            TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending','processing','done','failed','skipped')),
    priority        INTEGER NOT NULL DEFAULT 100,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, id),
    UNIQUE (channel, word)
);

CREATE INDEX IF NOT EXISTS idx_words_status_priority
    ON words(channel, status, priority, id);

CREATE TABLE IF NOT EXISTS runs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    channel            TEXT    NOT NULL DEFAULT 'wordstrata',
    word_id            INTEGER NOT NULL,
    started_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    finished_at        TEXT,
    status             TEXT    NOT NULL DEFAULT 'running'
                          CHECK (status IN ('running','done','failed')),
    script_path        TEXT,
    image_paths        TEXT,
    audio_path         TEXT,
    video_path         TEXT,
    script_model       TEXT,
    image_model        TEXT,
    tts_voice          TEXT,
    youtube_video_id   TEXT,
    youtube_url        TEXT,
    error              TEXT,
    metrics_json       TEXT,
    FOREIGN KEY (channel, word_id) REFERENCES words(channel, id)
);

CREATE INDEX IF NOT EXISTS idx_runs_channel_word ON runs(channel, word_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);

-- One row per channel per analytics run. Powers day-over-day trends,
-- milestone-crossing detection, and anomaly alerts in the daily digest
-- without re-querying historical windows from the YouTube API.
CREATE TABLE IF NOT EXISTS analytics_snapshots (
    channel               TEXT    NOT NULL,
    date                  TEXT    NOT NULL,   -- YYYY-MM-DD (report run date)
    subscribers           INTEGER NOT NULL,
    total_views           INTEGER NOT NULL,
    views_period          INTEGER NOT NULL,   -- trailing-window views at snapshot time
    watch_minutes_period  INTEGER NOT NULL,
    shares_period         INTEGER NOT NULL DEFAULT 0,
    created_at            TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, date)
);

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
    watch_minutes  INTEGER NOT NULL DEFAULT 0,   -- cumulative (Analytics API)
    shares         INTEGER NOT NULL DEFAULT 0,   -- cumulative (Analytics API)
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, video_id, date)
);

CREATE INDEX IF NOT EXISTS idx_video_snapshots_lookup
    ON video_snapshots(channel, video_id, date DESC);
