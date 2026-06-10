"""SQLite state — schema bootstrap + multi-channel queries.

Each row in `words` and `runs` carries a `channel` slug. PK on words is
compound `(channel, id)` so each channel keeps its own 1..N id space matching
its `words.csv` file. Stdlib sqlite3 is fine — pipeline steps are sequential.
"""

from __future__ import annotations

import csv
import json
import logging
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from config import settings
from channels import load as load_channel

log = logging.getLogger("shorts-api.db")

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"

# Bump whenever schema.sql changes. apply_migrations() walks PRAGMA user_version
# from whatever the DB currently has up to TARGET, executing each step.
TARGET_USER_VERSION = 1


@contextmanager
def conn() -> Generator[sqlite3.Connection, None, None]:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(settings.db_path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def _current_user_version(c: sqlite3.Connection) -> int:
    return c.execute("PRAGMA user_version").fetchone()[0]


def _column_exists(c: sqlite3.Connection, table: str, column: str) -> bool:
    rows = c.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _migrate_to_v1(c: sqlite3.Connection) -> None:
    """v0 → v1: add channel column + compound PK to words/runs.

    v0 schema:
      words(id PK, word UNIQUE, ...)
      runs(id PK, word_id FK→words.id, ...)
      VIEW next_word

    v1 schema (channels/<slug>/words.csv lives here):
      words(channel + id PK, channel + word UNIQUE, ...)
      runs(id PK, channel + word_id FK→words(channel,id), ...)
      view dropped — query in Python

    FKs are disabled during the rebuild because `DROP TABLE words` does an
    implicit DELETE that the runs→words FK (default NO ACTION = RESTRICT)
    would block.
    """
    c.execute("PRAGMA foreign_keys = OFF")
    # Defensive: a previous half-run may have left _new tables behind.
    # DROP IF EXISTS so the migration is restartable.
    c.execute("DROP TABLE IF EXISTS words_new")
    c.execute("DROP TABLE IF EXISTS runs_new")
    if not _column_exists(c, "words", "channel"):
        log.info("migrating words → multi-channel (channel column + compound PK)")
        c.executescript(
            """
            CREATE TABLE words_new (
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
            INSERT INTO words_new
                (channel, id, word, category, origin_language, hook, status, priority, created_at, updated_at)
            SELECT
                'wordstrata', id, word, category, origin_language, hook, status, priority, created_at, updated_at
            FROM words;
            DROP TABLE words;
            ALTER TABLE words_new RENAME TO words;
            CREATE INDEX IF NOT EXISTS idx_words_status_priority
                ON words(channel, status, priority, id);
            """
        )

    if not _column_exists(c, "runs", "channel"):
        log.info("migrating runs → multi-channel (channel column + compound FK)")
        c.executescript(
            """
            CREATE TABLE runs_new (
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
            INSERT INTO runs_new
                (id, channel, word_id, started_at, finished_at, status,
                 script_path, image_paths, audio_path, video_path,
                 script_model, image_model, tts_voice,
                 youtube_video_id, youtube_url, error, metrics_json)
            SELECT
                id, 'wordstrata', word_id, started_at, finished_at, status,
                script_path, image_paths, audio_path, video_path,
                script_model, image_model, tts_voice,
                youtube_video_id, youtube_url, error, metrics_json
            FROM runs;
            DROP TABLE runs;
            ALTER TABLE runs_new RENAME TO runs;
            CREATE INDEX IF NOT EXISTS idx_runs_channel_word
                ON runs(channel, word_id, started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
            """
        )

    # The old next_word VIEW (paramless) doesn't fit multi-channel; drop it.
    c.execute("DROP VIEW IF EXISTS next_word")

    c.execute("PRAGMA foreign_keys = ON")


_MIGRATIONS = {1: _migrate_to_v1}


def init_schema() -> None:
    """Bring the DB to TARGET_USER_VERSION.

    Order matters: schema.sql declares the *current* schema with the *current*
    indexes. On an existing pre-v1 DB those indexes reference columns that
    don't exist yet, so we must run migrations FIRST when the tables already
    exist. On a fresh DB we just run schema.sql and stamp user_version.
    """
    sql = SCHEMA_PATH.read_text()
    with conn() as c:
        has_words = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='words'"
        ).fetchone() is not None

        if has_words:
            # Existing DB — migrate first, then re-run schema.sql so any new
            # IF NOT EXISTS clauses (added in later schema versions) take effect.
            v = _current_user_version(c)
            while v < TARGET_USER_VERSION:
                v += 1
                migrate = _MIGRATIONS.get(v)
                if migrate is None:
                    raise RuntimeError(f"no migration registered for v{v}")
                migrate(c)
                c.execute(f"PRAGMA user_version = {v}")
                log.info("migrated db to user_version=%d", v)
            c.executescript(sql)
        else:
            # Fresh DB — schema.sql creates tables already at TARGET schema.
            c.executescript(sql)
            c.execute(f"PRAGMA user_version = {TARGET_USER_VERSION}")
    log.info("schema applied (%s)", SCHEMA_PATH)


# ─── Words queue ────────────────────────────────────────────────────────────

def load_words_if_empty(channel: str) -> int:
    """Load `channels/<channel>/words.csv` into the words table.

    Per-channel idempotent: if any rows for this channel already exist, no-op.
    Words that already exist in the (channel, word) pair are skipped — so
    re-running is safe even if the CSV has been extended.
    """
    csv_path = load_channel(channel).words_csv_path
    if not csv_path.exists():
        raise FileNotFoundError(f"no words.csv for channel {channel}: {csv_path}")

    with conn() as c:
        (existing,) = c.execute(
            "SELECT COUNT(*) FROM words WHERE channel = ?", (channel,)
        ).fetchone()
        if existing > 0:
            log.info(
                "channel=%s already populated (%d rows) — skipping CSV load",
                channel, existing,
            )
            return 0

        loaded = 0
        with csv_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            # The CSV subject column varies by channel (word/figure_or_myth/
            # case_name/subject_name). We coerce to the canonical 'word' column.
            fieldnames = list(reader.fieldnames or [])
            subject_col = _detect_subject_column(fieldnames)
            attribute_col = _detect_attribute_column(fieldnames)
            for row in reader:
                if row.get("priority") == "99":
                    continue
                c.execute(
                    """
                    INSERT OR IGNORE INTO words
                        (channel, id, word, category, origin_language, hook, priority)
                    VALUES
                        (:channel, :id, :word, :category, :origin_language, :hook, :priority)
                    """,
                    {
                        "channel": channel,
                        "id": int(row["id"]),
                        "word": row[subject_col].strip(),
                        "category": row["category"].strip(),
                        "origin_language": row[attribute_col].strip(),
                        "hook": row["hook"].strip(),
                        "priority": int(row["priority"]),
                    },
                )
                loaded += 1
    log.info("loaded %d rows from %s for channel=%s", loaded, csv_path, channel)
    return loaded


def _detect_subject_column(fieldnames: list[str]) -> str:
    """Each channel's CSV uses a different name for the main subject column.
    Pick the first match from the canonical aliases."""
    for c in ("word", "figure_or_myth", "case_name", "subject_name", "subject"):
        if c in fieldnames:
            return c
    raise ValueError(f"no subject column found in CSV; fields={fieldnames}")


def _detect_attribute_column(fieldnames: list[str]) -> str:
    """Each channel's CSV uses a different name for the 'attribute' column —
    origin_language for Wordstrata, origin_culture for Mythscape, species for
    Bright Beasts, case_year_or_range for Open Verdicts."""
    for c in ("origin_language", "origin_culture", "case_year_or_range", "species", "attribute"):
        if c in fieldnames:
            return c
    raise ValueError(f"no attribute column found in CSV; fields={fieldnames}")


def next_pending_word(channel: str) -> dict | None:
    """Next word for `channel`: lowest priority, ties broken by id."""
    with conn() as c:
        row = c.execute(
            """
            SELECT * FROM words
            WHERE channel = ? AND status = 'pending'
            ORDER BY priority ASC, id ASC
            LIMIT 1
            """,
            (channel,),
        ).fetchone()
        return dict(row) if row else None


def get_word(channel: str, word_id: int) -> dict | None:
    with conn() as c:
        row = c.execute(
            "SELECT * FROM words WHERE channel = ? AND id = ?",
            (channel, word_id),
        ).fetchone()
        return dict(row) if row else None


def set_word_status(channel: str, word_id: int, status: str) -> None:
    with conn() as c:
        c.execute(
            "UPDATE words SET status = ?, updated_at = datetime('now') "
            "WHERE channel = ? AND id = ?",
            (status, channel, word_id),
        )


# ─── Runs (audit trail) ─────────────────────────────────────────────────────

def record_completed_run(
    channel: str,
    word_id: int,
    *,
    script_path: str | None = None,
    image_paths: list[str] | None = None,
    audio_path: str | None = None,
    video_path: str | None = None,
    youtube_video_id: str | None = None,
    youtube_url: str | None = None,
    script_model: str | None = None,
    image_model: str | None = None,
    tts_voice: str | None = None,
) -> int:
    """Insert a single 'done' row capturing one full pipeline → upload event."""
    with conn() as c:
        cur = c.execute(
            """
            INSERT INTO runs (
                channel, word_id, status, finished_at,
                script_path, image_paths, audio_path, video_path,
                youtube_video_id, youtube_url,
                script_model, image_model, tts_voice
            )
            VALUES (?, ?, 'done', datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                channel,
                word_id,
                script_path,
                json.dumps(image_paths) if image_paths is not None else None,
                audio_path,
                video_path,
                youtube_video_id,
                youtube_url,
                script_model,
                image_model,
                tts_voice,
            ),
        )
        return cur.lastrowid or 0
