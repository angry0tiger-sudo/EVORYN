#!/usr/bin/env bash
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

PYTHON_BIN=""
for candidate in python3.12 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && \
        "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)' >/dev/null 2>&1; then
        PYTHON_BIN="$candidate"
        break
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    echo "ERROR: Python 3.12.x is required. No files were removed." >&2
    exit 2
fi

if [ ! -x ".venv/bin/python" ]; then
    "$PYTHON_BIN" -m venv .venv
fi

if [ ! -f ".env" ]; then
    cp .env.example .env
    chmod 600 .env 2>/dev/null || true
    echo "Created .env from .env.example; no credentials were added."
else
    echo "Existing .env preserved; it was not overwritten."
fi

mkdir -p data
chmod 700 data 2>/dev/null || true
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m compileall -q src tests scripts
.venv/bin/python -m pytest -q

cat <<'EOF'

EVORYN local environment setup and offline verification completed.
No exchange request was made and no trading process was started.
Read README.md before configuring any credential.
EOF
