#!/usr/bin/env bash
# Sync dev source from local to stl. Pulls nothing; one-way push.
# Excludes generated artifacts and secrets.

set -euo pipefail

LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE_HOST="stl"
REMOTE_DIR="~/etymology-shorts"

rsync -avh --delete \
  --exclude '.DS_Store' \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude 'output/' \
  --exclude 'state.db' \
  --exclude 'state.db-*' \
  --exclude '.env' \
  --exclude 'secrets/' \
  "$LOCAL_DIR/" "$REMOTE_HOST:$REMOTE_DIR/"

echo "synced → $REMOTE_HOST:$REMOTE_DIR"
