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
