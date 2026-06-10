#!/usr/bin/env bash
# One-time migration helper for the multi-channel refactor.
#
# Brings a Wordstrata install (single-channel layout) up to the multi-channel
# layout where every artifact lives under a channel-scoped path:
#
#   secrets/youtube_oauth.json      → secrets/youtube_oauth.wordstrata.json
#   secrets/youtube_token.json      → secrets/youtube_token.wordstrata.json
#   output/scripts/word_XXXX.json   → output/wordstrata/scripts/word_XXXX.json
#   output/audio/word_XXXX.wav      → output/wordstrata/audio/word_XXXX.wav
#   output/images/word_XXXX_N.png   → output/wordstrata/images/word_XXXX_N.png
#   output/videos/word_XXXX.mp4     → output/wordstrata/videos/word_XXXX.mp4
#
# state.db migrates itself on the next uvicorn startup (see db.py:apply_migrations).
#
# Idempotent — safe to re-run.

set -euo pipefail

ROOT="${HOME}/etymology-shorts"
SECRETS="${ROOT}/secrets"
OUT="${ROOT}/output"

echo "Migrating ${ROOT} to multi-channel layout..."
echo

if [ ! -d "${ROOT}" ]; then
  echo "ERROR: ${ROOT} doesn't exist. Nothing to migrate."
  exit 1
fi

# 1. Secrets — rename if the channel-scoped form doesn't already exist.
if [ -f "${SECRETS}/youtube_oauth.json" ] && [ ! -f "${SECRETS}/youtube_oauth.wordstrata.json" ]; then
  mv "${SECRETS}/youtube_oauth.json" "${SECRETS}/youtube_oauth.wordstrata.json"
  echo "  ✓ moved youtube_oauth.json → youtube_oauth.wordstrata.json"
elif [ -f "${SECRETS}/youtube_oauth.wordstrata.json" ]; then
  echo "  · youtube_oauth.wordstrata.json already exists; skipping"
fi

if [ -f "${SECRETS}/youtube_token.json" ] && [ ! -f "${SECRETS}/youtube_token.wordstrata.json" ]; then
  mv "${SECRETS}/youtube_token.json" "${SECRETS}/youtube_token.wordstrata.json"
  echo "  ✓ moved youtube_token.json → youtube_token.wordstrata.json"
elif [ -f "${SECRETS}/youtube_token.wordstrata.json" ]; then
  echo "  · youtube_token.wordstrata.json already exists; skipping"
fi

# 2. Output directories.
mkdir -p "${OUT}/wordstrata"
for sub in scripts audio images videos; do
  src="${OUT}/${sub}"
  dst="${OUT}/wordstrata/${sub}"
  if [ -d "${src}" ] && [ ! -d "${dst}" ]; then
    mv "${src}" "${dst}"
    echo "  ✓ moved output/${sub} → output/wordstrata/${sub}"
  elif [ -d "${dst}" ]; then
    echo "  · output/wordstrata/${sub} already exists; skipping"
  fi
done

echo
echo "Migration complete."
echo
echo "Next steps:"
echo "  1. Restart shorts-api (uvicorn) — state.db will migrate itself on startup."
echo "  2. For each new channel, create a Google Cloud OAuth client and run:"
echo "       uv run scripts/yt_init.py --channel the-mythscape"
echo "       uv run scripts/yt_init.py --channel open-verdicts"
echo "       uv run scripts/yt_init.py --channel bright-beasts"
echo "  3. Initialize state for each new channel:"
echo "       curl -X POST http://localhost:7860/the-mythscape/state/init"
echo "       curl -X POST http://localhost:7860/open-verdicts/state/init"
echo "       curl -X POST http://localhost:7860/bright-beasts/state/init"
echo "  4. Generate per-channel n8n workflows:"
echo "       python3 n8n/generate.py"
echo "     Then import each n8n/workflows/<slug>.json into n8n (and deactivate"
echo "     the old 'Etymology Shorts — Daily' workflow)."
