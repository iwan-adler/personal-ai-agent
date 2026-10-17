"""Courses: the structure that tasks, facts and documents can be linked to.

The V0.6 "knowledge graph" is foreign keys in agent.db, as the memory plan says:
a graph engine only if joins ever fail. A course is identified by a short code
("DBMS"), unique regardless of case. Exams are tasks linked to their course,
so a deadline lives in one place only. Deleting a course (only the user's
/forget-course command can) clears the links; the linked items stay.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.memory.db import Database

CODE_PATTERN = r"^[A-Za-z0-9_-]{1,20}$"
_CODE = re.compile(CODE_PATTERN)


class CourseNotFound(LookupError):
    """No course with that code."""


class CourseExists(ValueError):
    """A course with that code (in any case) already exists."""


@dataclass(frozen=True)
class Course:
    id: int
    code: str
    name: str
    semester: str | None
    created_at: str


@dataclass(frozen=True)
class CourseStats:
    course: Course
    open_tasks: int
    documents: int
    facts: int
    next_due_date: str | None
    next_due_title: str | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def check_code(code: str) -> str:
    if not _CODE.fullmatch(code):
        raise ValueError("Course codes are 1-20 letters, digits, '-' or '_'.")
    return code


class CourseStore:
    def __init__(self, db: Database | Path | str) -> None:
        self.db = db if isinstance(db, Database) else Database(db)

    def add(self, code: str, name: str, semester: str | None = None) -> Course:
        check_code(code)
        with self.db.connect() as conn:
            if self._find(conn, code) is not None:
                raise CourseExists(f"A course with code {code} already exists.")
            cursor = conn.execute(
                "INSERT INTO courses (code, name, semester, created_at) VALUES (?, ?, ?, ?)",
                (code, name, semester, _now()),
            )
            return self._by_id(conn, cursor.lastrowid)

    def get(self, code: str) -> Course:
        with self.db.connect() as conn:
            course = self._find(conn, code)
        if course is None:
            raise CourseNotFound(f"Unknown course '{code}'; see course_list.")
        return course

    def update(self, code: str, name: str | None = None, semester: str | None = None) -> Course:
        with self.db.connect() as conn:
            course = self._find(conn, code)
            if course is None:
                raise CourseNotFound(f"Unknown course '{code}'; see course_list.")
            conn.execute(
                "UPDATE courses SET name = ?, semester = ? WHERE id = ?",
                (name if name is not None else course.name,
                 semester if semester is not None else course.semester, course.id),
            )
            return self._by_id(conn, course.id)

    def list(self) -> list[CourseStats]:
        """Every course with counts and its next open deadline."""
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT c.*,
                  (SELECT COUNT(*) FROM tasks t WHERE t.course_id = c.id AND t.status = 'open') AS open_tasks,
                  (SELECT COUNT(*) FROM documents d WHERE d.course_id = c.id) AS documents,
                  (SELECT COUNT(*) FROM facts f WHERE f.course_id = c.id) AS facts,
                  (SELECT t.due_date FROM tasks t WHERE t.course_id = c.id AND t.status = 'open'
                     AND t.due_date IS NOT NULL ORDER BY t.due_date, t.id LIMIT 1) AS next_due_date,
                  (SELECT t.title FROM tasks t WHERE t.course_id = c.id AND t.status = 'open'
                     AND t.due_date IS NOT NULL ORDER BY t.due_date, t.id LIMIT 1) AS next_due_title
                  FROM courses c ORDER BY c.code COLLATE NOCASE
                """
            ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            course = Course(**{k: data.pop(k) for k in ("id", "code", "name", "semester", "created_at")})
            result.append(CourseStats(course=course, **data))
        return result

    def forget(self, code: str) -> Course:
        """Delete a course; tasks, facts and documents stay, without the link."""
        with self.db.connect() as conn:
            course = self._find(conn, code)
            if course is None:
                raise CourseNotFound(f"Unknown course '{code}'; see course_list.")
            conn.execute("DELETE FROM courses WHERE id = ?", (course.id,))
            return course

    @staticmethod
    def _find(conn, code: str) -> Course | None:
        row = conn.execute("SELECT * FROM courses WHERE code = ? COLLATE NOCASE", (code,)).fetchone()
        return Course(**dict(row)) if row else None

    @staticmethod
    def _by_id(conn, course_id: int | None) -> Course:
        return Course(**dict(conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()))
