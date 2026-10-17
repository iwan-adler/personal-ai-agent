"""Your weekly class timetable (migration 10). Rows are added only after you confirm them."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.extract import normalize_time
from app.memory.db import Database

DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MAX_CLASSES = 200


class ClassNotFound(LookupError):
    """No timetable entry with that id."""


@dataclass(frozen=True)
class ClassSlot:
    id: int
    weekday: int
    start_time: str
    end_time: str
    title: str
    room: str
    course_id: int | None
    course: str | None = None

    @property
    def day_name(self) -> str:
        return DAY_NAMES[self.weekday]

    @property
    def hours(self) -> float:
        sh, sm = map(int, self.start_time.split(":"))
        eh, em = map(int, self.end_time.split(":"))
        return ((eh * 60 + em) - (sh * 60 + sm)) / 60


_SELECT = "SELECT t.*, c.code AS course FROM timetable t LEFT JOIN courses c ON c.id = t.course_id"


def check_slot(weekday: int, start: str, end: str, title: str, room: str) -> tuple[int, str, str, str, str]:
    """Normalized values, or ValueError with a message for you."""
    if weekday not in range(7):
        raise ValueError("Pick a day.")
    start, end = normalize_time(start), normalize_time(end)
    if end <= start:
        raise ValueError("A class must end after it starts.")
    title, room = " ".join(title.split()), " ".join(room.split())
    if not title or len(title) > 120 or len(room) > 60:
        raise ValueError("A class needs a name (up to 120 characters); rooms up to 60.")
    return weekday, start, end, title, room


class TimetableStore:
    def __init__(self, db: Database | Path | str) -> None:
        self.db = db if isinstance(db, Database) else Database(db)

    def add(self, weekday: int, start: str, end: str, title: str, room: str = "",
            course_id: int | None = None) -> ClassSlot:
        weekday, start, end, title, room = check_slot(weekday, start, end, title, room)
        with self.db.connect() as conn:
            if conn.execute("SELECT COUNT(*) FROM timetable").fetchone()[0] >= MAX_CLASSES:
                raise ValueError(f"The timetable is limited to {MAX_CLASSES} classes.")
            cursor = conn.execute(
                "INSERT INTO timetable (weekday, start_time, end_time, title, room, course_id) VALUES (?, ?, ?, ?, ?, ?)",
                (weekday, start, end, title, room, course_id),
            )
            return self._get(conn, cursor.lastrowid)

    def update(self, slot_id: int, weekday: int, start: str, end: str, title: str, room: str,
               course_id: int | None) -> ClassSlot:
        weekday, start, end, title, room = check_slot(weekday, start, end, title, room)
        with self.db.connect() as conn:
            self._get(conn, slot_id)
            conn.execute(
                "UPDATE timetable SET weekday = ?, start_time = ?, end_time = ?, title = ?, room = ?, course_id = ?"
                " WHERE id = ?",
                (weekday, start, end, title, room, course_id, slot_id),
            )
            return self._get(conn, slot_id)

    def delete(self, slot_id: int) -> None:
        with self.db.connect() as conn:
            self._get(conn, slot_id)
            conn.execute("DELETE FROM timetable WHERE id = ?", (slot_id,))

    def clear(self) -> int:
        with self.db.connect() as conn:
            return conn.execute("DELETE FROM timetable").rowcount

    def week(self) -> list[ClassSlot]:
        with self.db.connect() as conn:
            rows = conn.execute(f"{_SELECT} ORDER BY t.weekday, t.start_time, t.id").fetchall()
        return [ClassSlot(**dict(row)) for row in rows]

    def day(self, weekday: int) -> list[ClassSlot]:
        return [slot for slot in self.week() if slot.weekday == weekday]

    @staticmethod
    def _get(conn: sqlite3.Connection, slot_id: int | None) -> ClassSlot:
        row = conn.execute(f"{_SELECT} WHERE t.id = ?", (slot_id,)).fetchone()
        if row is None:
            raise ClassNotFound(f"Class {slot_id} not found.")
        return ClassSlot(**dict(row))
