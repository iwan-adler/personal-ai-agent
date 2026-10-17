"""Koda as a desktop app: the web UI in its own Edge window (Windows).

Edge's app mode shows one window without tabs or an address bar. It runs with
its own profile in data/app-window/, so your normal browser's extensions,
which can read every page, never see Koda. While that window is open, Edge
holds the profile's lockfile; when it is released, the server stops too.
(Watching the started process is not enough: Edge may restart itself, for
example when it creates the profile.)

The sign-in link (with the token) is passed on Edge's command line, which
other programs of the same Windows user can read; they could read data/
directly anyway, and the token is new on every start.
"""

from __future__ import annotations

import socket
import time
from pathlib import Path

from app.tools.process import start_detached

EDGE_PATHS = (
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
)


def find_edge() -> Path | None:
    return next((path for path in EDGE_PATHS if path.is_file()), None)


def window_args(url: str, profile_dir: Path) -> list[str]:
    return [
        f"--app={url}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--window-size=1100,820",
    ]


def port_free(host: str, port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def wait_for_port(host: str, port: int, timeout_seconds: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def edge_running(profile_dir: Path) -> bool:
    """True while an Edge process holds this profile's lockfile."""
    try:
        with open(profile_dir / "lockfile", "r+b"):
            return False
    except FileNotFoundError:
        return False
    except OSError:  # locked by Edge
        return True


def wait_until_closed(profile_dir: Path, start_timeout_seconds: float = 30.0, poll_seconds: float = 1.0) -> None:
    deadline = time.monotonic() + start_timeout_seconds
    while not edge_running(profile_dir):
        if time.monotonic() > deadline:
            return
        time.sleep(0.2)
    while edge_running(profile_dir):
        time.sleep(poll_seconds)


def open_window(edge: Path, url: str, profile_dir: Path, starter=start_detached):
    profile_dir.mkdir(parents=True, exist_ok=True)
    return starter(edge, window_args(url, profile_dir))
