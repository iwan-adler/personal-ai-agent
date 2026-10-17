"""python -m app.doctor: a local health check (V0.9 monitoring).

Read-only. Each check reports OK, WARN or FAIL with a short reason; the exit code
is 1 if anything FAILs. It never reads .env's contents (only asks git whether
the file is ignored and untracked), never prints settings values, and never
contacts the network or the model provider.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Literal

from app.config import PROJECT_ROOT, ConfigError, Settings, load_settings
from app.memory.db import MIGRATIONS, Database, SchemaTooNew
from app.tools.process import CommandResult, run_command

Status = Literal["OK", "WARN", "FAIL"]
MIN_FREE_BYTES = 1_000_000_000
# Plain ASCII: on Windows, output redirected to a file or pipe uses a legacy code page
# that cannot encode symbols like a check mark, and a health check must not crash there.
SYMBOLS = {"OK": "[ok]  ", "WARN": "[warn]", "FAIL": "[FAIL]"}

Runner = Callable[[list[str]], CommandResult]


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str


def _git(args: list[str]) -> CommandResult:
    return run_command(["git", *args], timeout_seconds=10, cwd=PROJECT_ROOT)


def check_settings(load: Callable[[], Settings] = load_settings) -> tuple[Check, Settings | None]:
    try:
        settings = load()
    except ConfigError as exc:
        return Check("settings", "FAIL", str(exc)), None
    enabled = [name for name, on in (
        ("files", settings.workspace_dir is not None),
        ("web", bool(settings.web_allowlist)),
        ("feeds", bool(settings.feeds)),
        ("github read", bool(settings.github_repos)),
        ("github write", bool(settings.github_write_repos)),
    ) if on]
    detail = f"loaded; autonomy level {int(settings.autonomy_level)}; optional tools: {', '.join(enabled) or 'none'}"
    return Check("settings", "OK", detail), settings


def check_database(path: Path) -> Check:
    if not path.exists():
        return Check("database", "WARN", "no database yet; it is created on first start")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            broken_links = conn.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        return Check("database", "FAIL", f"cannot be read ({type(exc).__name__}); restore it from a backup")
    if integrity != "ok":
        return Check("database", "FAIL", "integrity check failed; restore it from a backup")
    if version > len(MIGRATIONS):
        return Check("database", "FAIL", f"schema {version} is newer than this code ({len(MIGRATIONS)})")
    if broken_links:
        return Check("database", "WARN", f"{len(broken_links)} rows point at missing rows")
    if version < len(MIGRATIONS):
        return Check("database", "WARN", f"schema {version}; it is upgraded to {len(MIGRATIONS)} on next start")
    return Check("database", "OK", f"integrity ok, schema {version}")


def check_audit_log(path: Path) -> Check:
    if not path.exists():
        return Check("audit log", "WARN", "no audit log yet; it is created on first start")
    damaged = 0
    events = 0
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                json.loads(line)
                events += 1
            except ValueError:
                damaged += 1
    size_mb = path.stat().st_size / 1_000_000
    if damaged:
        return Check("audit log", "WARN", f"{events} events, {damaged} damaged line(s) (e.g. after a crash), {size_mb:.1f} MB")
    return Check("audit log", "OK", f"{events} events, {size_mb:.1f} MB")


def check_disk(path: Path) -> Check:
    free = shutil.disk_usage(path if path.exists() else path.parent).free
    if free < MIN_FREE_BYTES:
        return Check("disk", "WARN", f"only {free / 1_000_000_000:.2f} GB free")
    return Check("disk", "OK", f"{free / 1_000_000_000:.1f} GB free")


def check_env_file(git: Runner = _git) -> Check:
    """Never reads .env; only asks git whether it is ignored and untracked."""
    try:
        tracked = git(["ls-files", "--error-unmatch", ".env"]).returncode == 0
        ignored = git(["check-ignore", "-q", ".env"]).returncode == 0
    except (OSError, ValueError):
        return Check(".env", "WARN", "git is not available, so .env protection was not checked")
    if tracked:
        return Check(".env", "FAIL", ".env is tracked by git; remove it from the repo and rotate its keys")
    if not ignored:
        return Check(".env", "FAIL", ".env is not in .gitignore")
    return Check(".env", "OK", "ignored by git and not tracked")


def check_dependencies(requirements: Path = PROJECT_ROOT / "requirements.txt",
                       version_of: Callable[[str], str] = metadata.version) -> Check:
    drift: list[str] = []
    for line in requirements.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or "==" not in line:
            continue
        name, pinned = (part.strip() for part in line.split("==", 1))
        try:
            installed = version_of(name)
        except metadata.PackageNotFoundError:
            drift.append(f"{name} missing")
            continue
        if installed != pinned:
            drift.append(f"{name} {installed} (pinned {pinned})")
    if drift:
        return Check("dependencies", "WARN", "differs from requirements.txt: " + "; ".join(drift))
    return Check("dependencies", "OK", "installed versions match requirements.txt")


def run_checks(data_dir: Path, load: Callable[[], Settings] = load_settings, git: Runner = _git) -> list[Check]:
    settings_check, _ = check_settings(load)
    return [
        settings_check,
        check_database(data_dir / "agent.db"),
        check_audit_log(data_dir / "audit.jsonl"),
        check_disk(data_dir),
        check_env_file(git),
        check_dependencies(),
    ]


def main() -> int:
    from app.main import DATA_DIR

    checks = run_checks(DATA_DIR)
    for check in checks:
        print(f"{SYMBOLS[check.status]} {check.name:<13} {check.detail}")
    failed = [c for c in checks if c.status == "FAIL"]
    print(f"\n{len(failed)} problem(s)." if failed else "\nAll checks passed (warnings, if any, are listed above).")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
