#!/bin/bash
# run_scan_rename.sh
# Activates the local virtual environment and runs scan_rename.py.
#
# Usage:
#   bash run_scan_rename.sh                  # process this directory
#   bash run_scan_rename.sh --dry-run        # preview without making changes
#   bash run_scan_rename.sh --dir /some/path # process a different directory
#
# Prerequisites (first run only):
#   1. python3 -m venv .venv
#   2. .venv/bin/pip install openai PyMuPDF Pillow
#
# API key is read from macOS Keychain (see README for setup), or from the
# environment variable OPENAI_API_KEY if already exported.

set -euo pipefail

echo "── $(date '+%Y-%m-%d %H:%M:%S')  run_scan_rename started ──"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python3"

if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "Virtual environment not found. Run these commands first:"
    echo "  cd \"$SCRIPT_DIR\""
    echo "  python3 -m venv .venv"
    echo "  .venv/bin/pip install openai PyMuPDF Pillow"
    exit 1
fi

# Fall back to Keychain when the variable isn't already in the environment.
if [[ -z "${OPENAI_API_KEY:-}" ]]; then
    OPENAI_API_KEY=$(security find-generic-password -a "$USER" -s "openai-api-key" -w 2>/dev/null || true)
export OPENAI_API_KEY
fi

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
    echo "Error: OPENAI_API_KEY not found in environment or Keychain."
    echo "Store it once with:"
    echo "  security add-generic-password -a \"\$USER\" -s \"openai-api-key\" -w"
    exit 1
fi

exec "$VENV_PYTHON" "$SCRIPT_DIR/scan_rename.py" "$@"
