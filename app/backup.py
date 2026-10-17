"""Backups of the agent's data (V0.9, D7: stay local).

python -m app.backup              create a backup now
python -m app.backup --list       list backups, newest first
python -m app.backup --verify F   check a backup without restoring it
python -m app.backup --restore F  restore (asks first; backs up the current state before)

A backup is one zip with agent.db and audit.jsonl. The database is copied with
SQLite's backup API, so the copy is consistent even while Koda is running. The
CLI and the web UI also make one automatically at start, at most once a day.
Only the newest KEEP backups are kept.

Restoring reads the zip only by its known member names (never extractall), so a
crafted zip cannot write elsewhere ("zip slip"), and refuses a backup whose
database fails SQLite's integrity check or comes from a newer schema.

Backups hold your messages, facts and tasks unencrypted, like data/ itself.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
import zipfile
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from app.memory.db import MIGRATIONS

PREFIX = "koda-backup-"
KEEP = 10
AUTO_INTERVAL = timedelta(hours=24)
DB_NAME = "agent.db"
AUDIT_NAME = "audit.jsonl"
MEMBERS = (DB_NAME, AUDIT_NAME)
_STAMP = "%Y%m%d-%H%M%S"


class BackupError(RuntimeError):
    """A backup could not be made, or is not safe to restore."""


def _stamp_of(path: Path) -> datetime | None:
    try:
        return datetime.strptime(path.stem.removeprefix(PREFIX)[:15], _STAMP)
    except ValueError:
        return None


def list_backups(backup_dir: Path) -> list[Path]:
    """Backups, newest first."""
    if not backup_dir.is_dir():
        return []
    found = [p for p in backup_dir.glob(f"{PREFIX}*.zip") if _stamp_of(p) is not None]
    return sorted(found, key=lambda p: p.name, reverse=True)


def create_backup(data_dir: Path, backup_dir: Path, now: datetime | None = None) -> Path:
    db_path = data_dir / DB_NAME
    if not db_path.exists():
        raise BackupError("There is no database to back up yet.")
    now = now or datetime.now()
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"{PREFIX}{now.strftime(_STAMP)}.zip"
    with tempfile.TemporaryDirectory(dir=backup_dir) as tmp:
        snapshot = Path(tmp) / DB_NAME
        source = sqlite3.connect(db_path)
        dest = sqlite3.connect(snapshot)
        try:
            source.backup(dest)  # consistent copy, even with other connections open
        finally:
            dest.close()
            source.close()
        partial = Path(tmp) / "partial.zip"
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(snapshot, DB_NAME)
            audit = data_dir / AUDIT_NAME
            if audit.exists():
                archive.write(audit, AUDIT_NAME)
        os.replace(partial, target)  # never a half-written backup under the final name
    for old in list_backups(backup_dir)[KEEP:]:
        old.unlink()
    return target


def verify_backup(path: Path) -> list[str]:
    """Problems that make a backup unsafe to restore; empty means it is fine."""
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile):
        return ["not a readable zip file"]
    with archive:
        names = set(archive.namelist())
        problems = [f"unexpected file {name!r}" for name in sorted(names - set(MEMBERS))]
        if DB_NAME not in names:
            return problems + ["no database in the backup"]
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / DB_NAME
            copy.write_bytes(archive.read(DB_NAME))
            try:
                conn = sqlite3.connect(copy)
                try:
                    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
                    version = conn.execute("PRAGMA user_version").fetchone()[0]
                finally:
                    conn.close()
            except sqlite3.DatabaseError:
                return problems + ["the database in the backup cannot be read"]
    if integrity != "ok":
        problems.append("the database in the backup fails its integrity check")
    if version > len(MIGRATIONS):
        problems.append(f"the backup is from a newer version (schema {version})")
    return problems


def restore_backup(path: Path, data_dir: Path, backup_dir: Path, confirm: Callable[[str], bool]) -> str:
    problems = verify_backup(path)
    if problems:
        raise BackupError("Refusing to restore: " + "; ".join(problems) + ".")
    if not confirm(f"Replace your current data with {path.name}? Stop Koda first. "
                   "Your current state is backed up before anything is replaced."):
        return "Kept your current data."
    safety = create_backup(data_dir, backup_dir) if (data_dir / DB_NAME).exists() else None
    with zipfile.ZipFile(path) as archive:
        for name in MEMBERS:  # known names only: no paths from the zip are ever used
            if name not in archive.namelist():
                continue
            partial = data_dir / f".{name}.restoring"
            partial.write_bytes(archive.read(name))
            os.replace(partial, data_dir / name)
    kept = f" Your previous state is in {safety.name}." if safety else ""
    return f"Restored {path.name}.{kept}"


def maybe_auto_backup(data_dir: Path, backup_dir: Path, now: datetime | None = None) -> Path | None:
    """At start: back up if there is data and the newest backup is a day old or more."""
    now = now or datetime.now()
    if not (data_dir / DB_NAME).exists():
        return None
    newest = next(iter(list_backups(backup_dir)), None)
    if newest is not None and now - (_stamp_of(newest) or datetime.min) < AUTO_INTERVAL:
        return None
    return create_backup(data_dir, backup_dir, now)


def _confirm(prompt: str) -> bool:
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def main(argv: list[str] | None = None) -> int:
    from app.main import DATA_DIR

    parser = argparse.ArgumentParser(prog="python -m app.backup", description="Back up or restore Koda's data.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--list", action="store_true", help="list backups, newest first")
    group.add_argument("--verify", metavar="FILE", help="check a backup without restoring it")
    group.add_argument("--restore", metavar="FILE", help="restore a backup (asks first)")
    args = parser.parse_args(argv)
    backup_dir = backup_dir_from_settings(DATA_DIR)
    try:
        if args.list:
            backups = list_backups(backup_dir)
            print("\n".join(f"  {p.name}  ({p.stat().st_size / 1_000_000:.1f} MB)" for p in backups)
                  or f"No backups in {backup_dir}.")
        elif args.verify:
            problems = verify_backup(Path(args.verify))
            print("Backup is fine." if not problems else "Problems: " + "; ".join(problems))
            return 1 if problems else 0
        elif args.restore:
            print(restore_backup(Path(args.restore), DATA_DIR, backup_dir, _confirm))
        else:
            print(f"Backup saved: {create_backup(DATA_DIR, backup_dir)}")
    except (BackupError, OSError, sqlite3.Error) as exc:
        print(f"Backup error: {exc}", file=sys.stderr)
        return 1
    return 0


def backup_dir_from_settings(data_dir: Path) -> Path:
    """BACKUP_DIR if set and valid, else data/backups. Never fails: a broken
    setting must not stop backups; it is reported by load_settings instead."""
    try:
        from app.config import load_settings

        configured = load_settings().backup_dir
    except Exception:  # noqa: BLE001 — fall back to the default location
        configured = None
    return configured or data_dir / "backups"


if __name__ == "__main__":
    raise SystemExit(main())
