#!/bin/sh
# Start the Koda web interface (macOS / Linux, 127.0.0.1 only). Open the link it prints.
cd "$(dirname "$0")/.." || exit 1
if [ ! -x .venv/bin/python ]; then
  echo 'Koda is not set up yet: see "Getting started" in README.md.' >&2
  exit 1
fi
exec .venv/bin/python -m app.web_ui "$@"
