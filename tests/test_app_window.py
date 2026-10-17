"""Koda as a desktop app: the web UI in its own Edge window (app/web_ui/window.py)."""

import json
import os
import socket
import sys
import time
from pathlib import Path

import pytest

import app.web_ui.__main__ as web_main
from app.config import Settings
from app.tools.process import start_detached
from app.web_ui import window

ROOT = Path(__file__).resolve().parent.parent


# --- Starting the window -----------------------------------------------------------------------------


def test_the_window_is_isolated_from_your_normal_browser(tmp_path):
    """Security: own profile and no extensions, which could read every page."""
    args = window.window_args("http://127.0.0.1:8765/?token=t", tmp_path / "profile")
    assert args[0] == "--app=http://127.0.0.1:8765/?token=t"
    assert f"--user-data-dir={tmp_path / 'profile'}" in args and "--disable-extensions" in args


def test_open_window_uses_the_fixed_edge_path(tmp_path):
    calls = []
    window.open_window(Path(r"C:\Edge\msedge.exe"), "http://127.0.0.1:1/", tmp_path / "p",
                       starter=lambda program, args: calls.append((program, args)))
    assert calls[0][0] == Path(r"C:\Edge\msedge.exe") and (tmp_path / "p").is_dir()
    assert all(path.is_absolute() for path in window.EDGE_PATHS)


def test_start_detached_scrubs_the_environment(tmp_path, monkeypatch):
    """Security: the window process must not inherit API keys."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    out = tmp_path / "env.json"
    code = f"import json, os; open({str(out)!r}, 'w').write(json.dumps(dict(os.environ)))"
    start_detached(Path(sys.executable), ["-c", code]).wait(timeout=30)
    env = json.loads(out.read_text())
    assert "OPENAI_API_KEY" not in env and "sk-should-not-leak" not in out.read_text()


@pytest.mark.parametrize("program", [Path("python.exe"), Path(r"C:\does\not\exist.exe")])
def test_start_detached_needs_an_existing_absolute_program(program):
    with pytest.raises(ValueError):
        start_detached(program, [])


def test_start_detached_refuses_a_command_line_string():
    with pytest.raises(TypeError):
        start_detached(Path(sys.executable), "-c print(1)")


# --- Knowing when the window was closed --------------------------------------------------------------


def test_edge_running_follows_the_profile_lock(tmp_path):
    assert not window.edge_running(tmp_path)  # no lockfile: not running
    (tmp_path / "lockfile").write_bytes(b"")
    assert not window.edge_running(tmp_path)  # left behind but not held


@pytest.mark.skipif(sys.platform != "win32", reason="Edge's exclusive lock is a Windows behaviour")
def test_a_held_lock_means_running(tmp_path):
    """Edge opens its lockfile with no sharing allowed, like this."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    handle = kernel32.CreateFileW(str(tmp_path / "lockfile"), 0xC0000000, 0, None, 2, 0x80, None)
    assert handle not in (None, wintypes.HANDLE(-1).value)
    try:
        assert window.edge_running(tmp_path)
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(handle))
    assert not window.edge_running(tmp_path)


def test_wait_until_closed_gives_up_if_the_window_never_starts(tmp_path):
    start = time.monotonic()
    window.wait_until_closed(tmp_path, start_timeout_seconds=0.3)
    assert time.monotonic() - start < 3


def test_port_checks():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        assert not window.port_free("127.0.0.1", port)
        assert window.wait_for_port("127.0.0.1", port, timeout_seconds=2)


# --- App mode start-up -------------------------------------------------------------------------------


@pytest.fixture
def app_mode(monkeypatch):
    messages = []
    monkeypatch.setattr(web_main, "load_settings", lambda: Settings(openai_api_key="k", openai_model="m"))
    monkeypatch.setattr(web_main, "show_message", messages.append)
    return messages


def test_without_edge_it_explains_instead_of_failing_silently(app_mode, monkeypatch):
    monkeypatch.setattr(window, "find_edge", lambda: None)
    assert web_main.main(["--app"]) == 1
    assert "Edge was not found" in app_mode[0]


def test_a_second_start_is_refused_while_koda_is_open(app_mode, monkeypatch):
    monkeypatch.setattr(window, "find_edge", lambda: Path(sys.executable))
    monkeypatch.setattr(window, "port_free", lambda host, port: False)
    assert web_main.main(["--app"]) == 1
    assert "already open" in app_mode[0]


def test_unexpected_errors_are_shown_not_swallowed(app_mode, monkeypatch):
    def boom():
        raise RuntimeError("broken")

    monkeypatch.setattr(window, "find_edge", boom)
    assert web_main.main(["--app"]) == 1
    assert "RuntimeError: broken" in app_mode[0]


def test_no_console_does_not_crash(app_mode, monkeypatch):
    """pythonw sets sys.stdout/stderr to None; printing must still work."""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(window, "find_edge", lambda: None)
    assert web_main.main(["--app"]) == 1
    assert sys.stderr is not None and "Edge was not found" in app_mode[0]


# --- The shortcut ------------------------------------------------------------------------------------


def test_the_shortcut_runs_koda_without_a_console_and_has_an_icon():
    script = (ROOT / "scripts" / "install-shortcut.cmd").read_text(encoding="utf-8")
    assert r".venv\Scripts\pythonw.exe" in script and "-m app.web_ui --app" in script
    icon = (ROOT / "scripts" / "koda.ico").read_bytes()
    assert icon[:4] == b"\0\0\1\0" and int.from_bytes(icon[4:6], "little") >= 4
    assert "data/app-window/" in (ROOT / ".gitignore").read_text(encoding="utf-8")
