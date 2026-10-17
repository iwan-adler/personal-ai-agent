"""python -m app.web_ui: the local web UI on 127.0.0.1 only (D5 rule 1)."""

from __future__ import annotations

import sqlite3
import sys

from app.config import ConfigError, load_settings
from app.logging_setup import setup_logging
from app.main import DATA_DIR, announce_auto_backup, build_agent
from app.web_ui.security import Guard
from app.web_ui.server import create_app

HOST = "127.0.0.1"  # never 0.0.0.0: the UI must not be reachable from the network


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1
    logger = setup_logging(settings.log_level)
    try:
        agent, state = build_agent(settings, logger, defer_approvals=True)
    except (OSError, sqlite3.Error) as exc:
        print(f"Storage error: could not open {DATA_DIR}: {exc}", file=sys.stderr)
        return 1
    announce_auto_backup(settings)
    import uvicorn

    guard = Guard(port=settings.web_ui_port)
    app = create_app(agent, state, guard)
    print("Koda web UI. Open this link (it signs you in; keep it private):")
    print(f"  http://{HOST}:{guard.port}/?token={guard.token}")
    print("Stop with Ctrl+C.")
    # No access log: the first request's URL carries the token.
    uvicorn.run(app, host=HOST, port=guard.port, access_log=False, server_header=False,
                proxy_headers=False, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
