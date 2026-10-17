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
    # 5: courses (V0.6 "knowledge graph": foreign keys, not a graph engine).
    # Tasks, facts and documents may point at a course; deleting a course only
    # clears those links. Exams are tasks linked to their course.
    """
    CREATE TABLE courses (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        code       TEXT NOT NULL UNIQUE COLLATE NOCASE,
        name       TEXT NOT NULL,
        semester   TEXT,
        created_at TEXT NOT NULL
    );
    ALTER TABLE tasks ADD COLUMN course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL;
    ALTER TABLE facts ADD COLUMN course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL;
    ALTER TABLE documents ADD COLUMN course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL;
    CREATE INDEX tasks_by_course ON tasks (course_id);
    CREATE INDEX facts_by_course ON facts (course_id);
    CREATE INDEX documents_by_course ON documents (course_id);
    """,
    # 6: flashcards with SM-2 scheduling, and a review log (V0.6 tutor).
    """
    CREATE TABLE cards (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        course_id     INTEGER REFERENCES courses(id) ON DELETE SET NULL,
        front         TEXT NOT NULL,
        back          TEXT NOT NULL,
        source        TEXT,
        ease          REAL NOT NULL DEFAULT 2.5,
        interval_days INTEGER NOT NULL DEFAULT 0,
        repetitions   INTEGER NOT NULL DEFAULT 0,
        due_date      TEXT NOT NULL,           -- YYYY-MM-DD, local date
        created_at    TEXT NOT NULL
    );
    CREATE INDEX cards_by_due ON cards (due_date, id);
    CREATE INDEX cards_by_course ON cards (course_id);
    CREATE TABLE reviews (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        card_id     INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
        grade       INTEGER NOT NULL CHECK (grade BETWEEN 1 AND 4),
        reviewed_on TEXT NOT NULL,             -- YYYY-MM-DD, local date
        reviewed_at TEXT NOT NULL
    );
    CREATE INDEX reviews_by_day ON reviews (reviewed_on);
    """,
    # 7: remaining effort per task, for the planner (V0.7).
    """
    ALTER TABLE tasks ADD COLUMN estimate_hours REAL
        CHECK (estimate_hours IS NULL OR (estimate_hours > 0 AND estimate_hours <= 200));
    """,
    # 8: opportunity scout (V0.7). A local cache of matching feed entries; the
    # link is unique, so re-scanning never stores an entry twice.
    """
    CREATE TABLE opportunities (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        link       TEXT NOT NULL UNIQUE,
        title      TEXT NOT NULL,
        summary    TEXT NOT NULL,
        feed_url   TEXT NOT NULL,
        published  TEXT,
        matched    TEXT NOT NULL,           -- comma-separated keywords that matched
        status     TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'saved', 'dismissed')),
        found_at   TEXT NOT NULL,
        task_id    INTEGER REFERENCES tasks(id) ON DELETE SET NULL
    );
    CREATE INDEX opportunities_by_status ON opportunities (status, id);
    """,
    # 9: Study Mode (a tutor session with progress) and small UI preferences.
    # A study session only holds what was said in it: the tutor has no tools.
    """
    CREATE TABLE study_sessions (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        subject        TEXT NOT NULL,
        topic          TEXT NOT NULL,
        difficulty     TEXT NOT NULL CHECK (difficulty IN ('beginner', 'intermediate', 'advanced')),
        goal           TEXT NOT NULL DEFAULT '',
        mode           TEXT NOT NULL CHECK (mode IN ('learn', 'revise', 'practice', 'quiz')),
        course_id      INTEGER REFERENCES courses(id) ON DELETE SET NULL,
        started_at     TEXT NOT NULL,
        last_active_at TEXT NOT NULL,
        active_seconds INTEGER NOT NULL DEFAULT 0 CHECK (active_seconds >= 0),
        correct        INTEGER NOT NULL DEFAULT 0,
        partial        INTEGER NOT NULL DEFAULT 0,
        incorrect      INTEGER NOT NULL DEFAULT 0,
        open_question  TEXT                -- the question waiting for an answer, if any
    );
    CREATE TABLE study_messages (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL REFERENCES study_sessions(id) ON DELETE CASCADE,
        role       TEXT NOT NULL CHECK (role IN ('tutor', 'student')),
        content    TEXT NOT NULL,
        verdict    TEXT CHECK (verdict IS NULL OR verdict IN ('correct', 'partial', 'incorrect')),
        created_at TEXT NOT NULL
    );
    CREATE INDEX study_messages_by_session ON study_messages (session_id, id);
    CREATE INDEX study_sessions_by_activity ON study_sessions (last_active_at);
    CREATE TABLE preferences (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    # 10: your materials. A category for each document (what the file is, for the
    # Materials page), the weekly class timetable, and which files a study session
    # teaches from. Timetable rows are only ever saved after you confirm them.
    """
    ALTER TABLE documents ADD COLUMN category TEXT NOT NULL DEFAULT 'notes'
        CHECK (category IN ('notes', 'assignment', 'timetable', 'classwork', 'other'));
    CREATE TABLE timetable (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        weekday     INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),   -- 0 = Monday
        start_time  TEXT NOT NULL CHECK (start_time GLOB '[0-2][0-9]:[0-5][0-9]'),
        end_time    TEXT NOT NULL CHECK (end_time GLOB '[0-2][0-9]:[0-5][0-9]' AND end_time > start_time),
        title       TEXT NOT NULL,
        room        TEXT NOT NULL DEFAULT '',
        course_id   INTEGER REFERENCES courses(id) ON DELETE SET NULL
    );
    CREATE INDEX timetable_by_day ON timetable (weekday, start_time);
    CREATE TABLE study_session_documents (
        session_id  INTEGER NOT NULL REFERENCES study_sessions(id) ON DELETE CASCADE,
        document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        PRIMARY KEY (session_id, document_id)
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
