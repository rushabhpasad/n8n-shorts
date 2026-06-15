#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Merge web-verified candidate JSON files into a channel's words.csv.

Reads every *.json file in a workdir (each shaped like {"candidates": [...]})
where every candidate object's keys match the channel's CSV columns (minus the
leading `id`, plus an optional `source` that is dropped). Dedupes case-insensitively
on the subject column (col 2) against the existing queue AND within the new batch,
assigns sequential ids continuing from the current max, optionally trims to a net
target, and appends with proper CSV quoting.

Usage:
  merge_candidates.py --channel the-mythscape --workdir /tmp/.../the-mythscape \
      --target 350 [--apply]

Without --apply it does a dry run (prints stats + sample rows, writes nothing).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_existing(csv_path: Path) -> tuple[list[str], list[dict], int]:
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        header = list(reader.fieldnames or [])
        rows = list(reader)
    max_id = max((int(r["id"]) for r in rows), default=0)
    return header, rows, max_id


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--target", type=int, default=None,
                    help="net new rows to keep (trim extras); default keep all")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    csv_path = REPO_ROOT / "channels" / args.channel / "words.csv"
    header, existing, max_id = load_existing(csv_path)
    subject_col = header[1]  # col after id is always the subject

    seen = {r[subject_col].strip().lower() for r in existing}
    workdir = Path(args.workdir)

    kept: list[dict] = []
    dropped_dupes = 0
    per_file: dict[str, int] = {}
    for jf in sorted(workdir.glob("*.json")):
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"!! could not parse {jf.name}: {e}", file=sys.stderr)
            continue
        cands = data.get("candidates", data if isinstance(data, list) else [])
        n = 0
        for c in cands:
            subj = str(c.get(subject_col, "")).strip().lower()
            if not subj:
                continue
            if subj in seen:
                dropped_dupes += 1
                continue
            seen.add(subj)
            kept.append(c)
            n += 1
        per_file[jf.name] = n

    if args.target is not None and len(kept) > args.target:
        # keep lower priority numbers first (10 before 20), stable otherwise
        kept.sort(key=lambda c: int(c.get("priority", 99)))
        trimmed = len(kept) - args.target
        kept = kept[: args.target]
    else:
        trimmed = 0

    print(f"channel={args.channel}")
    print(f"  existing rows: {len(existing)} (max id {max_id})")
    print(f"  per-file kept: {per_file}")
    print(f"  total unique new: {len(kept) + 0}  dropped dupes: {dropped_dupes}  trimmed: {trimmed}")
    print(f"  final count after append: {len(existing) + len(kept)}")
    if kept[:3]:
        print("  sample new rows:")
        for c in kept[:3]:
            print("   ", {k: c.get(k) for k in header[1:]})

    if not args.apply:
        print("  (dry run — no write. re-run with --apply to append)")
        return 0

    next_id = max_id + 1
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        for c in kept:
            row = [next_id] + [c.get(col, "") for col in header[1:]]
            writer.writerow(row)
            next_id += 1
    print(f"  appended {len(kept)} rows -> {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
