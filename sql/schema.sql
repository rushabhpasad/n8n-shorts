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
