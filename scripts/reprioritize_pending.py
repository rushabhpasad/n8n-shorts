#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Update pending words' priority in the live SQLite DB to match words.csv.

Reads each channel's words.csv and issues:

    UPDATE words SET priority=? WHERE channel=? AND id=? AND status='pending'

for every row in the CSV. Only pending rows are touched — done/processing/
failed/skipped rows are left alone so history is never rewritten.

The join key is (channel, id): the CSV `id` column is the same integer that
was loaded as `words.id` during db.load_words_if_empty (see api/db.py).

Usage
-----
Dry-run (default — prints plan, writes nothing):
    uv run scripts/reprioritize_pending.py

Apply changes to the production DB:
    uv run scripts/reprioritize_pending.py --apply

You must run this from the repository root so the api/ package is on sys.path
and config.py can find the default DB at ~/n8n-shorts/state.db (or whatever
$DB_PATH / .env specifies).
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

# ── Path bootstrap ────────────────────────────────────────────────────────────
# The api/ package is not installed; add it so `import db` and `import config`
# resolve correctly whether the script is run from the repo root or elsewhere.
REPO_ROOT = Path(__file__).resolve().parent.parent
API_DIR = REPO_ROOT / "api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

import db  # noqa: E402  (after sys.path fixup)
from channels import list_slugs  # noqa: E402


def load_csv_priorities(slug: str) -> dict[int, int]:
    """Return {id: priority} for all rows in channels/<slug>/words.csv."""
    csv_path = REPO_ROOT / "channels" / slug / "words.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"No words.csv found for channel {slug!r}: {csv_path}")

    priorities: dict[int, int] = {}
    with csv_path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            priorities[int(row["id"])] = int(row["priority"])
    return priorities


def reprioritize_channel(
    slug: str,
    priorities: dict[int, int],
    *,
    apply: bool,
) -> dict[int, int]:
    """Update pending rows for `slug`.

    Returns a dict {new_priority: count} of rows that would be / were changed.
    Rows already at the correct priority are skipped (no unnecessary writes).
    """
    changed: dict[int, int] = defaultdict(int)  # {new_priority: n_rows}

    with db.conn() as c:
        # Fetch all pending rows for this channel so we know current state.
        pending = c.execute(
            "SELECT id, priority FROM words WHERE channel = ? AND status = 'pending'",
            (slug,),
        ).fetchall()

        if not pending:
            return dict(changed)

        for row in pending:
            word_id = row["id"]
            current_priority = row["priority"]
            new_priority = priorities.get(word_id)

            if new_priority is None:
                # CSV doesn't have this id — skip (shouldn't happen if DB was
                # seeded from the same CSV, but be defensive).
                continue

            if new_priority == current_priority:
                # Already correct — skip.
                continue

            changed[new_priority] += 1

            if apply:
                c.execute(
                    "UPDATE words SET priority = ?, updated_at = datetime('now') "
                    "WHERE channel = ? AND id = ? AND status = 'pending'",
                    (new_priority, slug, word_id),
                )

    return dict(changed)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-prioritize pending words in the live DB from words.csv values.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Write changes to the DB (default: dry-run only).",
    )
    parser.add_argument(
        "--channel",
        metavar="SLUG",
        help="Restrict to a single channel slug (default: all channels).",
    )
    args = parser.parse_args()

    slugs = [args.channel] if args.channel else list_slugs()
    if not slugs:
        print("No channels found.", file=sys.stderr)
        sys.exit(1)

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"\n{'=' * 60}")
    print(f"reprioritize_pending.py  [{mode}]")
    if not args.apply:
        print("  Pass --apply to write changes to the DB.")
    print(f"{'=' * 60}\n")

    grand_total = 0
    for slug in slugs:
        try:
            priorities = load_csv_priorities(slug)
        except FileNotFoundError as exc:
            print(f"  [{slug}] SKIP — {exc}")
            continue

        changed = reprioritize_channel(slug, priorities, apply=args.apply)
        n_changed = sum(changed.values())
        grand_total += n_changed

        if n_changed == 0:
            print(f"  [{slug}] No pending rows need updating (all already correct).")
        else:
            tier_str = ", ".join(
                f"tier {p}×{count}"
                for p, count in sorted(changed.items())
            )
            action = "Updated" if args.apply else "Would update"
            print(f"  [{slug}] {action} {n_changed} pending rows — {tier_str}")

    print(f"\n{'=' * 60}")
    total_action = "Updated" if args.apply else "Would update"
    print(f"{total_action} {grand_total} pending rows across {len(slugs)} channel(s).")
    if not args.apply:
        print("Run with --apply to write these changes to the production DB.")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    main()
