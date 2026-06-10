"""SQLite state — schema bootstrap, words.csv loader, simple queries.

Uses the stdlib sqlite3 module (sync) — fine for this workload because
each pipeline step is sequential and per-word; no concurrent writers.
"""

from __future__ import annotations

import csv
import logging
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from config import settings

log = logging.getLogger("shorts-api.db")

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"
WORDS_CSV = Path(__file__).resolve().parent.parent / "words.csv"


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


def init_schema() -> None:
    """Apply schema.sql idempotently."""
    sql = SCHEMA_PATH.read_text()
    with conn() as c:
        c.executescript(sql)
    log.info("schema applied (%s)", SCHEMA_PATH)


def load_words_if_empty() -> int:
    """Load words.csv into `words` table only if the table is empty.

    Returns the number of rows loaded (0 if already populated).
    Words that already exist (by `word` UNIQUE) are skipped — re-running is safe.
    """
    with conn() as c:
        (existing,) = c.execute("SELECT COUNT(*) FROM words").fetchone()
        if existing > 0:
            log.info("words already populated (%d rows) — skipping CSV load", existing)
            return 0

        loaded = 0
        with WORDS_CSV.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Skip the placeholder skip rows
                if row.get("priority") == "99":
                    continue
                c.execute(
                    """
                    INSERT OR IGNORE INTO words
                        (id, word, category, origin_language, hook, priority)
                    VALUES
                        (:id, :word, :category, :origin_language, :hook, :priority)
                    """,
                    {
                        "id": int(row["id"]),
                        "word": row["word"].strip(),
                        "category": row["category"].strip(),
                        "origin_language": row["origin_language"].strip(),
                        "hook": row["hook"].strip(),
                        "priority": int(row["priority"]),
                    },
                )
                loaded += 1
    log.info("loaded %d words from %s", loaded, WORDS_CSV)
    return loaded


def next_pending_word() -> dict | None:
    """Return the next word to process (lowest priority, then id). None if empty."""
    with conn() as c:
        row = c.execute("SELECT * FROM next_word").fetchone()
        return dict(row) if row else None


def get_word(word_id: int) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM words WHERE id = ?", (word_id,)).fetchone()
        return dict(row) if row else None


def set_word_status(word_id: int, status: str) -> None:
    with conn() as c:
        c.execute(
            "UPDATE words SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (status, word_id),
        )


def start_run(word_id: int, script_model: str | None = None) -> int:
    with conn() as c:
        cur = c.execute(
            """
            INSERT INTO runs (word_id, script_model, status)
            VALUES (?, ?, 'running')
            """,
            (word_id, script_model),
        )
        return cur.lastrowid or 0


def finish_run(
    run_id: int,
    status: str,
    *,
    script_path: str | None = None,
    image_paths: list[str] | None = None,
    audio_path: str | None = None,
    video_path: str | None = None,
    youtube_video_id: str | None = None,
    youtube_url: str | None = None,
    error: str | None = None,
) -> None:
    import json

    with conn() as c:
        c.execute(
            """
            UPDATE runs SET
                status = ?,
                finished_at = datetime('now'),
                script_path = COALESCE(?, script_path),
                image_paths = COALESCE(?, image_paths),
                audio_path = COALESCE(?, audio_path),
                video_path = COALESCE(?, video_path),
                youtube_video_id = COALESCE(?, youtube_video_id),
                youtube_url = COALESCE(?, youtube_url),
                error = COALESCE(?, error)
            WHERE id = ?
            """,
            (
                status,
                script_path,
                json.dumps(image_paths) if image_paths is not None else None,
                audio_path,
                video_path,
                youtube_video_id,
                youtube_url,
                error,
                run_id,
            ),
        )


def record_completed_run(
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
    """Insert a single 'done' row capturing one full pipeline → upload event.
    Returns the new runs.id. We don't track partial runs here — /upload is
    the terminal step, so we just record the whole event atomically."""
    import json

    with conn() as c:
        cur = c.execute(
            """
            INSERT INTO runs (
                word_id, status, finished_at,
                script_path, image_paths, audio_path, video_path,
                youtube_video_id, youtube_url,
                script_model, image_model, tts_voice
            )
            VALUES (?, 'done', datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
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
