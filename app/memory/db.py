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
    # 2: sessions. Only what the user typed and the agent's final replies are
    # stored, never tool calls or tool results (see app/memory/sessions.py).
    """
    CREATE TABLE sessions (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at     TEXT NOT NULL,
        last_active_at TEXT NOT NULL
    );
    CREATE TABLE messages (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
        content    TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX messages_by_session ON messages (session_id, id);
    """,
    # 3: long-term facts, with FTS5 keyword search (D4). Facts are never
    # updated, only added or deleted, so two triggers keep the index in sync.
    """
    CREATE TABLE facts (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        text       TEXT NOT NULL,
        topic      TEXT,
        created_at TEXT NOT NULL
    );
    CREATE VIRTUAL TABLE facts_fts USING fts5(
        text, topic, content='facts', content_rowid='id'
    );
    CREATE TRIGGER facts_after_insert AFTER INSERT ON facts BEGIN
        INSERT INTO facts_fts (rowid, text, topic) VALUES (new.id, new.text, new.topic);
    END;
    CREATE TRIGGER facts_after_delete AFTER DELETE ON facts BEGIN
        INSERT INTO facts_fts (facts_fts, rowid, text, topic)
        VALUES ('delete', old.id, old.text, old.topic);
    END;
    """,
    # 4: document library (V0.6). A document is a file under the workspace's
    # library/ folder; its chunks carry a page (PDF) or start line (text) for
    # citations. FTS5 over chunk text, kept in sync by triggers like facts.
    """
    CREATE TABLE documents (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        path        TEXT NOT NULL UNIQUE,   -- relative to library/, '/' separators
        kind        TEXT NOT NULL CHECK (kind IN ('pdf', 'text')),
        sha256      TEXT NOT NULL,
        size_bytes  INTEGER NOT NULL,
        pages       INTEGER,                -- PDFs only
        imported_at TEXT NOT NULL
    );
    CREATE TABLE chunks (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        seq         INTEGER NOT NULL,
        page        INTEGER,
        line        INTEGER,
        text        TEXT NOT NULL
    );
    CREATE INDEX chunks_by_document ON chunks (document_id, seq);
    CREATE VIRTUAL TABLE chunks_fts USING fts5(text, content='chunks', content_rowid='id');
    CREATE TRIGGER chunks_after_insert AFTER INSERT ON chunks BEGIN
        INSERT INTO chunks_fts (rowid, text) VALUES (new.id, new.text);
    END;
    CREATE TRIGGER chunks_after_delete AFTER DELETE ON chunks BEGIN
        INSERT INTO chunks_fts (chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
    END;
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
