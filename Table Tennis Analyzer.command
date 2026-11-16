#!/bin/bash
# Double-click this file to set up (first run only) and launch the app.
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  PY=""
  for p in /usr/local/bin/python3 /opt/homebrew/bin/python3 /usr/bin/python3; do
    if [ -x "$p" ] && "$p" -c "import venv" >/dev/null 2>&1; then PY="$p"; break; fi
  done
  if [ -z "$PY" ]; then
    echo "Python 3 is needed. Install it from https://www.python.org/downloads/ and run this again."
    read -r -p "Press Return to close."
    exit 1
  fi
  echo "First run: setting up (takes a few minutes)..."
  "$PY" -m venv .venv || exit 1
  .venv/bin/python -m pip install --upgrade pip -q
  .venv/bin/python -m pip install -r requirements.txt || { read -r -p "Setup failed. Press Return."; exit 1; }
fi
exec .venv/bin/python app.py
