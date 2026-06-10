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
WORDS_CSV = REPO_ROOT / "words.csv"
PROMPT_FILE = REPO_ROOT / "prompts" / "words.md"

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "gemma4:latest"

WIKTIONARY_BASE = "https://en.wiktionary.org/api/rest_v1/page/html"
USER_AGENT = "etymology-shorts/1.0 (https://github.com/rushabhpasad/n8n-etymology-shorts)"

CSV_FIELDS = ["id", "word", "category", "origin_language", "priority", "hook"]


def load_existing_words(path: Path) -> tuple[set[str], int]:
    """Returns (lowercase word set, max id seen)."""
    existing: set[str] = set()
    max_id = 0
    if not path.exists():
        return existing, max_id
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            existing.add(row["word"].lower().strip())
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


def write_row(writer: csv.DictWriter, row: dict, also_file: Path | None) -> None:
    writer.writerow(row)
    if also_file is not None:
        with also_file.open("a", newline="", encoding="utf-8") as f:
            csv.DictWriter(
                f, fieldnames=CSV_FIELDS, quoting=csv.QUOTE_MINIMAL
            ).writerow(row)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--count", type=int, default=10,
                    help="number of candidates to ask the LLM for")
    ap.add_argument("--category", default=None,
                    help="optional category to focus on (e.g. greek_myth)")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"Ollama model name (default: {DEFAULT_MODEL})")
    ap.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL,
                    help=f"Ollama server URL (default: {DEFAULT_OLLAMA_URL})")
    ap.add_argument("--append", action="store_true",
                    help="append accepted rows to words.csv "
                         "(default: stdout review only)")
    ap.add_argument("--skip-wiktionary", action="store_true",
                    help="bypass Wiktionary verification (faster but riskier)")
    args = ap.parse_args()

    existing, max_id = load_existing_words(WORDS_CSV)
    print(
        f"loaded {len(existing)} existing words; max_id={max_id}",
        file=sys.stderr,
    )

    system = load_system_prompt(PROMPT_FILE)
    examples = load_example_rows(WORDS_CSV, n=10)
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
            word = (cand.get("word") or "").strip().lower()
            if not word or not re.fullmatch(r"[a-z][a-z'-]*", word):
                skipped.append((word or "<blank>", "invalid-word-shape"))
                continue
            if word in existing:
                skipped.append((word, "duplicate-of-queued"))
                continue
            if args.skip_wiktionary:
                ok, reason = True, "wiktionary-bypassed"
            else:
                ok, reason = wiktionary_has_etymology(word, client)
                time.sleep(0.3)   # polite pacing
            if not ok:
                skipped.append((word, reason))
                continue
            cand["word"] = word
            accepted.append(cand)
            existing.add(word)  # prevent dup within batch

    print(
        f"accepted {len(accepted)}, skipped {len(skipped)}",
        file=sys.stderr,
    )
    for w, r in skipped:
        print(f"  skip {w!r:>20}  {r}", file=sys.stderr)

    if not accepted:
        return 0

    target = WORDS_CSV if args.append else None
    writer = csv.DictWriter(
        sys.stdout, fieldnames=CSV_FIELDS, quoting=csv.QUOTE_MINIMAL
    )
    next_id = max_id + 1
    for cand in accepted:
        row = {
            "id": next_id,
            "word": cand["word"],
            "category": (cand.get("category") or "uncategorized").strip(),
            "origin_language": (cand.get("origin_language") or "unknown").strip(),
            "priority": cand.get("priority", 20),
            "hook": (cand.get("hook") or "").strip(),
        }
        write_row(writer, row, target)
        next_id += 1

    if args.append:
        print(
            f"\n→ appended {len(accepted)} rows to {WORDS_CSV}",
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
