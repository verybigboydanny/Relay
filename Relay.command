#!/bin/bash
# Opens the Relay app. The first run creates a virtual environment and installs the dependencies.
cd "$(dirname "$0")" || exit 1

# Files downloaded from the internet carry a quarantine flag that makes macOS refuse to run them.
xattr -dr com.apple.quarantine . 2>/dev/null

pause() { read -n 1 -s -r -p "Press any key to close this window."; echo; }

if [ ! -x .venv/bin/python ]; then
  PY=""
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
      PY="$c"; break
    fi
  done
  if [ -z "$PY" ]; then
    echo "Relay needs Python 3.11 or newer."
    echo "Install it from https://www.python.org/downloads/ (or run: brew install python), then open this file again."
    pause; exit 1
  fi
  echo "First run: setting up Relay. This takes a minute."
  "$PY" -m venv .venv \
    && .venv/bin/python -m pip install --quiet --upgrade pip \
    && .venv/bin/python -m pip install --quiet -r requirements.txt \
    || { echo "Setup failed. Scroll up for the error."; rm -rf .venv; pause; exit 1; }
fi

[ -f .env ] || cp .env.example .env
exec .venv/bin/python dashboard.py
