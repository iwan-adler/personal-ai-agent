"""python -m app.doctor: read-only health checks, never reading .env or the network."""

import sqlite3
from importlib import metadata

import pytest

import app.doctor as doctor
from app.audit import AuditLog
from app.config import ConfigError, Settings
from app.memory.db import MIGRATIONS, Database
from app.tools.process import CommandResult


def _settings():
    return Settings(openai_api_key="sk-test", openai_model="m")


def _git(tracked=False, ignored=True):
    def run(args):
        if args[0] == "ls-files":
            return CommandResult(0 if tracked else 1, "", "")
        return CommandResult(0 if ignored else 1, "", "")
    return run


def _healthy(tmp_path):
    Database(tmp_path / "agent.db")
    AuditLog(tmp_path / "audit.jsonl").write("run_start", run_id="r")
    return tmp_path


def test_a_healthy_setup_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "check_dependencies", lambda: doctor.Check("dependencies", "OK", "match"))
    checks = doctor.run_checks(_healthy(tmp_path), load=_settings, git=_git())
    assert [c.status for c in checks] == ["OK"] * 6
    assert checks[1].detail == f"integrity ok, schema {len(MIGRATIONS)}"


def test_settings_errors_fail_without_echoing_values():
    def broken():
        raise ConfigError("MAX_STEPS must be an integer.")
    check, settings = doctor.check_settings(broken)
    assert (check.status, check.detail, settings) == ("FAIL", "MAX_STEPS must be an integer.", None)
    ok, _ = doctor.check_settings(_settings)
    assert "sk-test" not in ok.detail and "optional tools: none" in ok.detail


def test_corrupted_database_fails(tmp_path):
    (tmp_path / "agent.db").write_bytes(b"SQLite format 3\x00" + b"\xff" * 4000)
    assert doctor.check_database(tmp_path / "agent.db").status == "FAIL"


def test_old_and_newer_schemas(tmp_path):
    path = tmp_path / "agent.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 1")
    conn.close()
    assert doctor.check_database(path).status == "WARN"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 5}")
    conn.close()
    assert doctor.check_database(path).status == "FAIL"


def test_the_check_never_changes_the_database(tmp_path):
    path = tmp_path / "agent.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 1")
    conn.close()
    doctor.check_database(path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1  # no migration happened
    conn.close()


def test_missing_files_are_warnings(tmp_path):
    assert doctor.check_database(tmp_path / "agent.db").status == "WARN"
    assert doctor.check_audit_log(tmp_path / "audit.jsonl").status == "WARN"


def test_damaged_audit_lines_are_counted(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.write("a")
    with log.path.open("a", encoding="utf-8") as handle:
        handle.write('{"cut off')
    check = doctor.check_audit_log(log.path)
    assert check.status == "WARN" and "1 events, 1 damaged line(s)" in check.detail


@pytest.mark.parametrize(("tracked", "ignored", "status"), [(False, True, "OK"), (True, True, "FAIL"), (False, False, "FAIL")])
def test_env_file_protection(tracked, ignored, status):
    assert doctor.check_env_file(_git(tracked, ignored)).status == status


def test_env_check_without_git():
    def no_git(args):
        raise OSError("git not found")
    assert doctor.check_env_file(no_git).status == "WARN"


def test_dependency_drift(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("fastapi==0.142.2\npypdf==6.19.0\nmissing-pkg==1.0\n# comment\n", encoding="utf-8")
    versions = {"fastapi": "0.142.2", "pypdf": "6.21.0"}

    def version_of(name):
        if name not in versions:
            raise metadata.PackageNotFoundError(name)
        return versions[name]

    check = doctor.check_dependencies(req, version_of)
    assert check.status == "WARN"
    assert "pypdf 6.21.0 (pinned 6.19.0)" in check.detail and "missing-pkg missing" in check.detail


def test_output_is_plain_ascii(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "run_checks", lambda data_dir: [doctor.Check("x", "FAIL", "broken"),
                                                                 doctor.Check("y", "OK", "fine")])
    assert doctor.main() == 1
    out = capsys.readouterr().out
    out.encode("ascii")  # would raise for symbols a legacy Windows code page cannot print
    assert "[FAIL] x" in out and "1 problem(s)." in out
