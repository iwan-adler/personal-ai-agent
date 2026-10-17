"""python -m app.web_ui: the local web UI on 127.0.0.1 only (D5 rule 1).

With --app it opens in its own window (see app/web_ui/window.py) and stops
when that window is closed. The desktop shortcut runs it with pythonw, which
has no console, so problems are shown in a message box.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import threading

from app import __version__
from app.config import ConfigError, load_settings
from app.logging_setup import setup_logging
from app.main import DATA_DIR, announce_auto_backup, build_agent
from app.web_ui import window
from app.web_ui.security import Guard
from app.web_ui.server import create_app

HOST = "127.0.0.1"  # never 0.0.0.0: the UI must not be reachable from the network
WINDOW_PROFILE = "app-window"


def _fail(message: str, app_mode: bool) -> int:
    if sys.stderr is not None:
        print(message, file=sys.stderr)
    if app_mode:
        show_message(message)
    return 1


def show_message(message: str) -> None:
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "Koda", 0x10)  # MB_ICONERROR


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.web_ui")
    parser.add_argument("--app", action="store_true", help="open Koda in its own window; closing it stops Koda")
    app_mode = parser.parse_args(argv).app
    if app_mode:
        # pythonw has no console: sys.stdout/stderr are None, and logging to them fails.
        for name in ("stdout", "stderr"):
            if getattr(sys, name) is None:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
        try:
            return _main(app_mode)
        except Exception as exc:  # noqa: BLE001 - no console to show a traceback in
            show_message(f"Koda stopped because of an error: {exc.__class__.__name__}: {exc}")
            return 1
    return _main(app_mode)


def _main(app_mode: bool) -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        return _fail(f"Config error: {exc}", app_mode)
    edge = window.find_edge() if app_mode else None
    if app_mode and edge is None:
        return _fail("Microsoft Edge was not found, so Koda cannot open its window. "
                     "Use scripts\\koda-web.cmd and open the link it prints.", app_mode)
    if app_mode and not window.port_free(HOST, settings.web_ui_port):
        return _fail(f"Koda is already open, or another program uses port {settings.web_ui_port} "
                     "(change WEB_UI_PORT in .env).", app_mode)
    logger = setup_logging(settings.log_level)
    try:
        agent, state = build_agent(settings, logger, defer_approvals=True)
    except (OSError, sqlite3.Error) as exc:
        return _fail(f"Storage error: could not open {DATA_DIR}: {exc}", app_mode)
    announce_auto_backup(settings)
    import uvicorn

    guard = Guard(port=settings.web_ui_port)
    app = create_app(agent, state, guard)
    url = f"http://{HOST}:{guard.port}/?token={guard.token}"
    # No access log: the first request's URL carries the token.
    options = dict(host=HOST, port=guard.port, access_log=False, server_header=False,
                   proxy_headers=False, log_level="warning")
    if not app_mode:
        print(f"Koda {__version__} web UI. Open this link (it signs you in; keep it private):")
        print(f"  {url}")
        print("Stop with Ctrl+C.")
        uvicorn.run(app, **options)
        return 0

    # Do not wait forever for open connections once the window is gone.
    server = uvicorn.Server(uvicorn.Config(app, timeout_graceful_shutdown=3, **options))

    def open_and_watch() -> None:
        if not window.wait_for_port(HOST, guard.port):
            server.should_exit = True
            return
        try:
            window.open_window(edge, url, DATA_DIR / WINDOW_PROFILE)
        except OSError as exc:
            show_message(f"Could not open the Koda window: {exc}")
            server.should_exit = True
            return
        window.wait_until_closed(DATA_DIR / WINDOW_PROFILE)
        server.should_exit = True  # the window was closed: stop Koda

    threading.Thread(target=open_and_watch, daemon=True).start()
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
