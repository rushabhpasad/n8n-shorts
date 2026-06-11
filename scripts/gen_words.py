#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "httpx>=0.28,<1.0",
# ]
# ///
"""Generate new etymology word candidates using a local LLM + Wiktionary check.

- Reads existing words.csv to dedup.
- Calls Ollama (or any compatible LLM endpoint) with a prompt that includes
  the existing queue as an explicit exclusion list.
- Validates each candidate against the English Wiktionary (page must exist
  AND have an Etymology section).
- Prints accepted rows as CSV to stdout (so you can review before committing)
  and, with --append, also writes them to words.csv.

Usage:
  uv run scripts/gen_words.py --count 20
  uv run scripts/gen_words.py --count 10 --category greek_myth
  uv run scripts/gen_words.py --count 5 --append
  uv run scripts/gen_words.py --count 5 --model qwen2.5:7b
  uv run scripts/gen_words.py --count 5 --skip-wiktionary    # faster, riskier

The script is uv-script-shebanged — it pulls its own deps into an ephemeral
venv. No project setup required.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
CHANNELS_DIR = REPO_ROOT / "channels"
DEFAULT_CHANNEL = "wordstrata"

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "gemma4:latest"

WIKTIONARY_BASE = "https://en.wiktionary.org/api/rest_v1/page/html"
USER_AGENT = "n8n-shorts/1.0 (https://github.com/rushabhpasad/n8n-shorts)"

# Each channel's words.csv may have a different schema (column names for the
# subject and the attribute). We detect the actual column names at runtime
# from the CSV header.
SUBJECT_ALIASES = ("word", "figure_or_myth", "case_name", "subject_name", "subject")
ATTRIBUTE_ALIASES = ("origin_language", "origin_culture", "case_year_or_range", "species", "attribute")


def channel_paths(channel: str) -> tuple[Path, Path]:
    """Return (words_csv, prompt_md) for channel."""
    base = CHANNELS_DIR / channel
    return base / "words.csv", base / "prompts" / "words.md"


def detect_csv_columns(csv_path: Path) -> tuple[list[str], str, str]:
    """Read the CSV header, return (fieldnames, subject_col, attribute_col)."""
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, [])
    if not header:
        raise ValueError(f"empty CSV: {csv_path}")
    subject = next((c for c in SUBJECT_ALIASES if c in header), None)
    attribute = next((c for c in ATTRIBUTE_ALIASES if c in header), None)
    if subject is None or attribute is None:
        raise ValueError(
            f"can't detect subject/attribute columns in {csv_path}; header={header}"
        )
    return header, subject, attribute


def load_existing_subjects(path: Path, subject_col: str) -> tuple[set[str], int]:
    """Returns (lowercase subject set, max id seen)."""
    existing: set[str] = set()
    max_id = 0
    if not path.exists():
        return existing, max_id
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            existing.add(row[subject_col].lower().strip())
            try:
                max_id = max(max_id, int(row["id"]))
            except (ValueError, KeyError, TypeError):
                pass
    return existing, max_id


def load_system_prompt(path: Path) -> str:
    """Extract the '## System prompt' section from prompts/words.md."""
    if not path.exists():
        raise FileNotFoundError(f"missing prompt file: {path}")
    text = path.read_text(encoding="utf-8")
    m = re.search(
        r"##\s+System prompt\s*\n(.*?)(?=\n---\s*\n|\n##\s+(?!#))",
        text,
        re.DOTALL,
    )
    return (m.group(1) if m else text).strip()


def load_example_rows(path: Path, n: int = 10) -> str:
    """Return the first n rows of words.csv as a compact CSV block."""
    if not path.exists():
        return ""
    rows: list[str] = []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)  # skip header
        for i, row in enumerate(reader):
            if i >= n:
                break
            rows.append(",".join(row))
    return "\n".join(rows)


def build_user_message(
    existing: set[str],
    count: int,
    category: str | None,
    examples: str,
) -> str:
    sample_excluded = sorted(existing)[:80]
    more = len(existing) - len(sample_excluded)
    parts = [
        f"Generate {count} new etymology word candidates.",
    ]
    if category:
        parts.append(f"Target category: {category}")
    if examples:
        parts.append("\nExample rows from the existing queue:\n" + examples)
    parts.append(
        "\nDo NOT propose any of these (already in queue):\n"
        + ", ".join(sample_excluded)
        + (f"\n... and {more} more." if more > 0 else "")
    )
    return "\n".join(parts)


def call_ollama(
    system: str, user: str, model: str, url: str, timeout: float = 240.0
) -> dict:
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(
            f"{url}/api/chat",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "format": "json",
                "stream": False,
                "options": {"temperature": 0.85, "top_p": 0.95},
            },
        )
        resp.raise_for_status()
        body = resp.json()
    content = body["message"]["content"]
    return json.loads(content)


def wiktionary_has_etymology(word: str, client: httpx.Client) -> tuple[bool, str]:
    """Return (ok, reason). ok=True → keep; ok=False → skip."""
    url = f"{WIKTIONARY_BASE}/{word}"
    try:
        resp = client.get(url, timeout=15.0, follow_redirects=True)
    except httpx.RequestError as e:
        return False, f"request-error: {type(e).__name__}"
    if resp.status_code == 404:
        return False, "no-wiktionary-page"
    if resp.status_code != 200:
        return False, f"http-{resp.status_code}"
    html = resp.text
    # Any "Etymology" heading qualifies — Wiktionary uses id="Etymology",
    # "Etymology_1", "Etymology_2", etc.
    if 'id="Etymology' in html or ">Etymology<" in html:
        return True, "ok"
    return False, "no-etymology-section"


def write_row(writer: csv.DictWriter, row: dict, fieldnames: list[str],
              also_file: Path | None) -> None:
    writer.writerow(row)
    if also_file is not None:
        with also_file.open("a", newline="", encoding="utf-8") as f:
            csv.DictWriter(
                f, fieldnames=fieldnames, quoting=csv.QUOTE_MINIMAL
            ).writerow(row)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--channel", default=DEFAULT_CHANNEL,
                    help=f"channel slug (default: {DEFAULT_CHANNEL}). Must match "
                         f"channels/<slug>/")
    ap.add_argument("--count", type=int, default=10,
                    help="number of candidates to ask the LLM for")
    ap.add_argument("--category", default=None,
                    help="optional category to focus on")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"Ollama model name (default: {DEFAULT_MODEL})")
    ap.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL,
                    help=f"Ollama server URL (default: {DEFAULT_OLLAMA_URL})")
    ap.add_argument("--append", action="store_true",
                    help="append accepted rows to the channel's words.csv "
                         "(default: stdout review only)")
    ap.add_argument("--skip-wiktionary", action="store_true",
                    help="bypass Wiktionary verification (faster but riskier; "
                         "Wiktionary is etymology-specific so verification is "
                         "skipped automatically for non-etymology channels)")
    args = ap.parse_args()

    words_csv, prompt_file = channel_paths(args.channel)
    if not words_csv.exists():
        print(f"ERROR: {words_csv} not found", file=sys.stderr)
        return 1
    if not prompt_file.exists():
        print(f"ERROR: {prompt_file} not found", file=sys.stderr)
        return 1

    fieldnames, subject_col, attribute_col = detect_csv_columns(words_csv)
    existing, max_id = load_existing_subjects(words_csv, subject_col)
    # Wiktionary lookup only makes sense for etymology — Wordstrata's column
    # is "word". For other channels skip verification by default.
    wiktionary_makes_sense = (subject_col == "word")

    print(
        f"channel={args.channel} loaded {len(existing)} existing entries; "
        f"max_id={max_id} subject_col={subject_col} attribute_col={attribute_col}",
        file=sys.stderr,
    )

    system = load_system_prompt(prompt_file)
    examples = load_example_rows(words_csv, n=10)
    user = build_user_message(existing, args.count, args.category, examples)

    print(
        f"calling {args.model} for {args.count} candidates"
        + (f" (category={args.category})" if args.category else "")
        + "…",
        file=sys.stderr,
    )
    t0 = time.perf_counter()
    raw = call_ollama(system, user, args.model, args.ollama_url)
    print(
        f"  LLM done in {time.perf_counter() - t0:.1f}s",
        file=sys.stderr,
    )

    candidates = raw.get("candidates", [])
    if not candidates:
        print("LLM returned no candidates", file=sys.stderr)
        return 1
    print(
        f"  got {len(candidates)} candidates; verifying…",
        file=sys.stderr,
    )

    accepted: list[dict] = []
    skipped: list[tuple[str, str]] = []
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html"}

    with httpx.Client(headers=headers) as client:
        for cand in candidates:
            subject = (cand.get(subject_col) or cand.get("word") or "").strip().lower()
            # For etymology channel, enforce single-word shape; for others allow
            # multi-word subjects ("mary celeste", "anansi and the box of stories").
            if subject_col == "word":
                shape_ok = bool(re.fullmatch(r"[a-z][a-z'-]*", subject))
            else:
                shape_ok = bool(re.fullmatch(r"[a-z][a-z0-9'\- ]*", subject))
            if not subject or not shape_ok:
                skipped.append((subject or "<blank>", "invalid-subject-shape"))
                continue
            if subject in existing:
                skipped.append((subject, "duplicate-of-queued"))
                continue
            if args.skip_wiktionary or not wiktionary_makes_sense:
                ok, reason = True, "wiktionary-bypassed"
            else:
                ok, reason = wiktionary_has_etymology(subject, client)
                time.sleep(0.3)   # polite pacing
            if not ok:
                skipped.append((subject, reason))
                continue
            cand[subject_col] = subject
            accepted.append(cand)
            existing.add(subject)  # prevent dup within batch

    print(
        f"accepted {len(accepted)}, skipped {len(skipped)}",
        file=sys.stderr,
    )
    for w, r in skipped:
        print(f"  skip {w!r:>20}  {r}", file=sys.stderr)

    if not accepted:
        return 0

    target = words_csv if args.append else None
    writer = csv.DictWriter(
        sys.stdout, fieldnames=fieldnames, quoting=csv.QUOTE_MINIMAL
    )
    next_id = max_id + 1
    for cand in accepted:
        row = {
            "id": next_id,
            subject_col: cand[subject_col],
            "category": (cand.get("category") or "uncategorized").strip(),
            attribute_col: (cand.get(attribute_col) or cand.get("origin_language") or "unknown").strip(),
            "priority": cand.get("priority", 20),
            "hook": (cand.get("hook") or "").strip(),
        }
        # Fill any extra columns the channel CSV may carry with defaults.
        for f in fieldnames:
            row.setdefault(f, "")
        write_row(writer, row, fieldnames, target)
        next_id += 1

    if args.append:
        print(
            f"\n→ appended {len(accepted)} rows to {words_csv}",
            file=sys.stderr,
        )
    else:
        print(
            "\nReview the rows above. To commit, re-run with --append.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
