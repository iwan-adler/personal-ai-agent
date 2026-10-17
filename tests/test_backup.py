"""Backups (V0.9, D7): consistent copies, retention, verification, safe restore."""

import sqlite3
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

import app.backup as backup
from app.audit import AuditLog
from app.config import PROJECT_ROOT, Settings
from app.main import announce_auto_backup
from app.memory.db import MIGRATIONS, Database
from app.memory.tasks import TaskStore

NOW = datetime(2026, 10, 17, 12, 0, 0)


@pytest.fixture
def data(tmp_path) -> Path:
    data_dir = tmp_path / "data"
    TaskStore(Database(data_dir / "agent.db")).add("Submit DBMS")
    AuditLog(data_dir / "audit.jsonl").write("run_start", run_id="r1")
    return data_dir


def _tasks(data_dir):
    return [t.title for t in TaskStore(data_dir / "agent.db").list("all")]


def test_backup_holds_a_consistent_database_and_the_audit_log(data, tmp_path):
    reader = sqlite3.connect(data / "agent.db")  # Koda may be running: an open connection
    reader.execute("SELECT * FROM tasks").fetchall()
    path = backup.create_backup(data, tmp_path / "backups", NOW)
    reader.close()
    assert path.name == "koda-backup-20261017-120000.zip"
    with zipfile.ZipFile(path) as archive:
        assert sorted(archive.namelist()) == ["agent.db", "audit.jsonl"]
    assert backup.verify_backup(path) == []


def test_only_the_newest_backups_are_kept(data, tmp_path):
    for hour in range(backup.KEEP + 3):
        backup.create_backup(data, tmp_path / "b", NOW + timedelta(hours=hour))
    kept = backup.list_backups(tmp_path / "b")
    assert len(kept) == backup.KEEP
    assert kept[0].name == f"koda-backup-{(NOW + timedelta(hours=backup.KEEP + 2)).strftime('%Y%m%d-%H%M%S')}.zip"


def test_automatic_backup_at_most_once_a_day(data, tmp_path):
    folder = tmp_path / "b"
    assert backup.maybe_auto_backup(data, folder, NOW) is not None
    assert backup.maybe_auto_backup(data, folder, NOW + timedelta(hours=23)) is None
    assert backup.maybe_auto_backup(data, folder, NOW + timedelta(hours=24)) is not None
    assert backup.maybe_auto_backup(tmp_path / "empty", folder, NOW) is None  # nothing to back up yet


# --- Verify and restore ---------------------------------------------------------------------------


def _zip(path, members):
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return path


def test_unsafe_backups_are_refused(data, tmp_path):
    good_db = (data / "agent.db").read_bytes()
    not_a_zip = tmp_path / "x.zip"
    not_a_zip.write_bytes(b"nope")
    cases = {
        "not a zip": not_a_zip,
        "no database": _zip(tmp_path / "a.zip", {"audit.jsonl": "{}"}),
        "unexpected": _zip(tmp_path / "b.zip", {"agent.db": good_db, "../../outside.txt": "pwned"}),
        "corrupt db": _zip(tmp_path / "c.zip", {"agent.db": b"SQLite format 3\x00" + b"\xff" * 4000}),
    }
    for label, path in cases.items():
        assert backup.verify_backup(path), label


def test_a_backup_from_a_newer_version_is_refused(data, tmp_path):
    newer = tmp_path / "newer.db"
    conn = sqlite3.connect(newer)
    conn.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
    conn.close()
    problems = backup.verify_backup(_zip(tmp_path / "n.zip", {"agent.db": newer.read_bytes()}))
    assert any("newer version" in p for p in problems)


def test_zip_slip_cannot_write_outside_the_data_folder(data, tmp_path):
    """Security: a crafted backup never gets to write anywhere."""
    evil = _zip(tmp_path / "evil.zip", {"agent.db": (data / "agent.db").read_bytes(),
                                        "../../outside.txt": "pwned"})
    with pytest.raises(backup.BackupError, match="unexpected file"):
        backup.restore_backup(evil, data, tmp_path / "b", confirm=lambda _p: True)
    assert not (tmp_path / "outside.txt").exists() and not (data.parent.parent / "outside.txt").exists()


def test_restore_asks_and_backs_up_the_current_state_first(data, tmp_path):
    folder = tmp_path / "b"
    saved = backup.create_backup(data, folder, NOW)
    TaskStore(data / "agent.db").add("Added after the backup")
    assert backup.restore_backup(saved, data, folder, confirm=lambda _p: False) == "Kept your current data."
    assert _tasks(data) == ["Submit DBMS", "Added after the backup"]
    message = backup.restore_backup(saved, data, folder, confirm=lambda _p: True)
    assert message.startswith(f"Restored {saved.name}. Your previous state is in koda-backup-")
    assert _tasks(data) == ["Submit DBMS"]
    newest = backup.list_backups(folder)[0]  # the safety backup holds the later task
    assert newest != saved
    scratch = tmp_path / "check"
    scratch.mkdir()
    with zipfile.ZipFile(newest) as archive:
        (scratch / "agent.db").write_bytes(archive.read("agent.db"))
    assert _tasks(scratch) == ["Submit DBMS", "Added after the backup"]


def test_no_database_no_backup(tmp_path):
    with pytest.raises(backup.BackupError, match="no database"):
        backup.create_backup(tmp_path / "missing", tmp_path / "b", NOW)


# --- Settings, start-up, command line --------------------------------------------------------------


def test_backup_dir_setting(tmp_path):
    ws = tmp_path / "ws"
    (ws / "inner").mkdir(parents=True)
    with pytest.raises(ValidationError, match="inside WORKSPACE_DIR"):
        Settings(openai_api_key="k", openai_model="m", workspace_dir=ws, backup_dir=ws / "inner")
    with pytest.raises(ValidationError, match="absolute"):
        Settings(openai_api_key="k", openai_model="m", backup_dir="relative/b")
    assert Settings(openai_api_key="k", openai_model="m", backup_dir="").backup_dir is None


def test_startup_backup_is_announced_and_never_fatal(data, tmp_path, capsys, monkeypatch):
    settings = Settings(openai_api_key="k", openai_model="m")
    announce_auto_backup(settings, data)
    assert "Backup saved:" in capsys.readouterr().out

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("app.main.maybe_auto_backup", broken)
    announce_auto_backup(settings, data)  # must not raise
    assert "Automatic backup failed: disk full" in capsys.readouterr().err


def test_command_line(data, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("app.main.DATA_DIR", data)
    monkeypatch.setattr(backup, "backup_dir_from_settings", lambda data_dir: tmp_path / "b")
    assert backup.main([]) == 0 and "Backup saved:" in capsys.readouterr().out
    assert backup.main(["--list"]) == 0 and "koda-backup-" in capsys.readouterr().out
    newest = backup.list_backups(tmp_path / "b")[0]
    assert backup.main(["--verify", str(newest)]) == 0 and "Backup is fine." in capsys.readouterr().out
    bad = _zip(tmp_path / "bad.zip", {"audit.jsonl": "{}"})
    assert backup.main(["--verify", str(bad)]) == 1


# --- Start scripts -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("script", "module"),
    [("koda.cmd", "app.main"), ("koda-web.cmd", "app.web_ui"), ("koda.sh", "app.main"), ("koda-web.sh", "app.web_ui")],
)
def test_start_scripts_use_the_project_venv(script, module):
    text = (PROJECT_ROOT / "scripts" / script).read_text(encoding="utf-8")
    assert f"-m {module}" in text
    assert (".venv\\Scripts\\python.exe" in text) if script.endswith(".cmd") else (".venv/bin/python" in text)
    assert "README.md" in text  # tells you what to do if the project is not set up
