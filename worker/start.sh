#!/usr/bin/env bash
# Book Bash — Station 4 worker launcher.
# Runs inside the book-restorer venv so restore.py's OpenCV/PyMuPDF/OpenAI
# stack is importable, and installs the worker's own deps on top.
set -euo pipefail

WORKER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOOK_BASH_DIR="$(dirname "$WORKER_DIR")"
RESTORER_DIR="${RESTORER_DIR:-$HOME/book-restorer}"
VENV="$RESTORER_DIR/.venv"

if [[ ! -f "$VENV/bin/activate" ]]; then
  echo "ERROR: book-restorer venv not found at $VENV" >&2
  exit 1
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

# Credentials: Supabase from book-bash, OPENAI_API_KEY from book-restorer.
set -a
[[ -f "$BOOK_BASH_DIR/.env" ]] && source "$BOOK_BASH_DIR/.env"
[[ -f "$RESTORER_DIR/.env" ]] && source "$RESTORER_DIR/.env"
set +a

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  echo "ERROR: OPENAI_API_KEY not found in $RESTORER_DIR/.env" >&2
  exit 1
fi

# Idempotent — pip no-ops when everything is already satisfied.
echo "Installing worker dependencies..."
pip install --quiet --disable-pip-version-check -r "$WORKER_DIR/requirements.txt"

export RESTORER_DIR
echo "Starting Book Bash worker (logs -> $WORKER_DIR/worker.log)"
exec python "$WORKER_DIR/worker.py"
