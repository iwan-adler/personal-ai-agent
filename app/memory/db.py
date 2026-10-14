"""The agent's SQLite database (data/agent.db): connections and schema versions.

One short-lived connection per call: tools run in worker threads
(run_with_timeout), and a sqlite3 connection must not be shared across threads,
so nothing is kept open between calls.

Schema changes are numbered migrations recorded in PRAGMA user_version. A
database created by an older version is upgraded step by step on open; one
created by a newer version is refused rather than misread.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# Append only: never edit or reorder a migration that has shipped.
MIGRATIONS: list[str] = [
    # 1: tasks (first V0.4 slice). IF NOT EXISTS: databases from before
    # versioning already have this table at user_version 0.
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        title        TEXT NOT NULL,
        due_date     TEXT,                 -- YYYY-MM-DD or NULL
        status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
        created_at   TEXT NOT NULL,
        completed_at TEXT
    );
    """,
]


class SchemaTooNew(RuntimeError):
    """The database was written by a newer version of the agent."""


class Database:
    def __init__(self, path: Path | str, timeout_seconds: float = 5.0) -> None:
        self.path = Path(path)
        # How long to wait for a lock held by another process before failing.
        self.timeout_seconds = timeout_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=self.timeout_seconds)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def schema_version(self) -> int:
        with self.connect() as conn:
            return conn.execute("PRAGMA user_version").fetchone()[0]

    def _migrate(self) -> None:
        current = self.schema_version()
        if current > len(MIGRATIONS):
            raise SchemaTooNew(
                f"{self.path} has schema version {current}; this code knows up to "
                f"{len(MIGRATIONS)}. Update the agent instead of opening it with old code."
            )
        for version, sql in enumerate(MIGRATIONS[current:], start=current + 1):
            with self.connect() as conn:
                # One transaction per step: a failed migration leaves the
                # previous version intact instead of a half-upgraded schema.
                conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
