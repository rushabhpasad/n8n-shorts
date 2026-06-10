#!/usr/bin/env bash
# Start shorts-api in dev (auto-reload) on stl.
# Assumes uv is on PATH (brew install).

set -euo pipefail

cd "$(dirname "$0")/.."

# Ensure uv resolves the right Python
export UV_PYTHON="${UV_PYTHON:-3.12}"

# uv-managed venv lives in ./api/.venv via the pyproject in ./api
cd api

uv sync

exec uv run uvicorn main:app \
    --host "${HOST:-0.0.0.0}" \
    --port "${PORT:-7860}" \
    --reload
