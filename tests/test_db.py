"""Schema versions: upgrade old databases, keep data, refuse newer ones."""

import sqlite3

import pytest

import app.memory.db as db_module
from app.memory.db import MIGRATIONS, Database, SchemaTooNew
from app.memory.tasks import TaskStore


def _tables(db: Database) -> set[str]:
    with db.connect() as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_new_database_gets_the_latest_schema(tmp_path):
    db = Database(tmp_path / "agent.db")
    assert db.schema_version() == len(MIGRATIONS)
    assert "tasks" in _tables(db)


def test_database_from_before_versioning_keeps_its_tasks(tmp_path):
    path = tmp_path / "agent.db"
    conn = sqlite3.connect(path)  # what the first V0.4 slice left behind: user_version 0
    conn.executescript(
        "CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,"
        " due_date TEXT, status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL,"
        " completed_at TEXT);"
        "INSERT INTO tasks (title, created_at) VALUES ('Old task', '2026-10-01T00:00:00+00:00');"
    )
    conn.close()
    store = TaskStore(path)
    assert store.db.schema_version() == len(MIGRATIONS)
    assert [t.title for t in store.list()] == ["Old task"]


def test_reopening_is_harmless(tmp_path):
    TaskStore(tmp_path / "agent.db").add("Keep me")
    store = TaskStore(tmp_path / "agent.db")
    assert [t.title for t in store.list()] == ["Keep me"]


def test_newer_schema_is_refused(tmp_path):
    path = tmp_path / "agent.db"
    Database(path)
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
    conn.close()
    with pytest.raises(SchemaTooNew):
        Database(path)


def test_failed_migration_leaves_the_previous_version(tmp_path, monkeypatch):
    path = tmp_path / "agent.db"
    Database(path)
    broken = [*MIGRATIONS, "CREATE TABLE half_done (x INTEGER); THIS IS NOT SQL;"]
    monkeypatch.setattr(db_module, "MIGRATIONS", broken)
    with pytest.raises(sqlite3.Error):
        Database(path)
    monkeypatch.setattr(db_module, "MIGRATIONS", MIGRATIONS)
    db = Database(path)
    assert db.schema_version() == len(MIGRATIONS)
    assert "half_done" not in _tables(db)
