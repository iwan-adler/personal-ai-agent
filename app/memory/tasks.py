"""Persistent task list (first V0.4 slice): SQLite via the standard library.

Why a table and not notes in RAM: deadlines must survive a restart, and
"what is due this week?" is a sorted query, not a text search.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

from app.memory.db import Database

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
    course_id: int | None = None
    course: str | None = None  # the course code, for display


# Tasks with their course code; every read goes through this.
_SELECT = "SELECT t.*, c.code AS course FROM tasks t LEFT JOIN courses c ON c.id = t.course_id"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TaskStore:
    """Tasks in the agent database. The table is created by migration 1 in db.py."""

    def __init__(self, db: Database | Path | str) -> None:
        self.db = db if isinstance(db, Database) else Database(db)

    @property
    def path(self) -> Path:
        return self.db.path

    def add(self, title: str, due_date: date | None = None, course_id: int | None = None) -> Task:
        with self.db.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO tasks (title, due_date, created_at, course_id) VALUES (?, ?, ?, ?)",
                (title, due_date.isoformat() if due_date else None, _now(), course_id),
            )
            return self._get(conn, cursor.lastrowid)

    def list(self, status: StatusFilter = "open", limit: int = 20,
             course_id: int | None = None) -> list[Task]:
        """Earliest deadline first; tasks without a deadline last. Optionally one course."""
        conditions, params = [], []
        if status != "all":
            conditions.append("t.status = ?")
            params.append(status)
        if course_id is not None:
            conditions.append("t.course_id = ?")
            params.append(course_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.db.connect() as conn:
            rows = conn.execute(
                f"{_SELECT} {where} ORDER BY t.due_date IS NULL, t.due_date, t.id LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [Task(**dict(row)) for row in rows]

    def complete(self, task_id: int) -> Task:
        with self.db.connect() as conn:
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
        row = conn.execute(f"{_SELECT} WHERE t.id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFound(f"Task {task_id} not found.")
        return Task(**dict(row))
