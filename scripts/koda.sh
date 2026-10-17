#!/bin/sh
# Start the Koda chat (macOS / Linux), using the project's virtual environment.
cd "$(dirname "$0")/.." || exit 1
if [ ! -x .venv/bin/python ]; then
  echo 'Koda is not set up yet: see "Getting started" in README.md.' >&2
  exit 1
fi
exec .venv/bin/python -m app.main "$@"
