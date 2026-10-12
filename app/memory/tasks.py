"""Persistent task list (first V0.4 slice): SQLite via the standard library.

Why a table and not notes in RAM: deadlines must survive a restart, and
"what is due this week?" is a sorted query, not a text search.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    due_date     TEXT,                 -- YYYY-MM-DD or NULL
    status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
    created_at   TEXT NOT NULL,
    completed_at TEXT
);
"""

StatusFilter = Literal["open", "done", "all"]


class TaskNotFound(LookupError):
    """No task with that id."""


class TaskAlreadyDone(ValueError):
    """The task was completed earlier."""


@dataclass(frozen=True)
class Task:
    id: int
    title: str
    due_date: str | None
    status: str
    created_at: str
    completed_at: str | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TaskStore:
    """Opens a short-lived connection per call.

    Tools run in worker threads (run_with_timeout), and a sqlite3 connection
    must not be shared across threads, so nothing is kept open between calls.
    """

    def __init__(self, path: Path | str, timeout_seconds: float = 5.0) -> None:
        self.path = Path(path)
        # How long to wait for a lock held by another process before failing.
        self.timeout_seconds = timeout_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=self.timeout_seconds)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def add(self, title: str, due_date: date | None = None) -> Task:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO tasks (title, due_date, created_at) VALUES (?, ?, ?)",
                (title, due_date.isoformat() if due_date else None, _now()),
            )
            return self._get(conn, cursor.lastrowid)

    def list(self, status: StatusFilter = "open", limit: int = 20) -> list[Task]:
        """Earliest deadline first; tasks without a deadline last."""
        where = "" if status == "all" else "WHERE status = ?"
        params: tuple = () if status == "all" else (status,)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM tasks {where} "
                "ORDER BY due_date IS NULL, due_date, id LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [Task(**dict(row)) for row in rows]

    def complete(self, task_id: int) -> Task:
        with self._connect() as conn:
            task = self._get(conn, task_id)
            if task.status == "done":
                raise TaskAlreadyDone(f"Task {task_id} is already done.")
            conn.execute(
                "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
                (_now(), task_id),
            )
            return self._get(conn, task_id)

    @staticmethod
    def _get(conn: sqlite3.Connection, task_id: int | None) -> Task:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFound(f"Task {task_id} not found.")
        return Task(**dict(row))
