-- state.db schema (SQLite)
-- Single source of truth for the etymology-shorts pipeline.
-- Created/migrated by shorts-api on startup.

CREATE TABLE IF NOT EXISTS words (
    id              INTEGER PRIMARY KEY,
    word            TEXT    NOT NULL UNIQUE,
    category        TEXT    NOT NULL,
    origin_language TEXT    NOT NULL,
    hook            TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending','processing','done','failed','skipped')),
    priority        INTEGER NOT NULL DEFAULT 100,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_words_status_priority
    ON words(status, priority, id);

CREATE TABLE IF NOT EXISTS runs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    word_id            INTEGER NOT NULL REFERENCES words(id),
    started_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    finished_at        TEXT,
    status             TEXT    NOT NULL DEFAULT 'running'
                          CHECK (status IN ('running','done','failed')),
    -- step-level artifacts / IDs
    script_path        TEXT,
    image_paths        TEXT,             -- JSON array
    audio_path         TEXT,
    video_path         TEXT,
    -- LLM / model attribution
    script_model       TEXT,
    image_model        TEXT,
    tts_voice          TEXT,
    -- YouTube outcome
    youtube_video_id   TEXT,
    youtube_url        TEXT,
    -- diagnostics
    error              TEXT,
    metrics_json       TEXT              -- {step_durations_ms, file_sizes, ...}
);

CREATE INDEX IF NOT EXISTS idx_runs_word ON runs(word_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);

-- Used by n8n's daily cron to pick the next word.
-- Lowest priority value wins; ties broken by id.
CREATE VIEW IF NOT EXISTS next_word AS
    SELECT id, word, category, origin_language, hook, priority
    FROM   words
    WHERE  status = 'pending'
    ORDER  BY priority ASC, id ASC
    LIMIT  1;
