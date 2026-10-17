"""Study Mode sessions: what was said, the open question and the score (migration 9).

Python keeps the score: the tutor model only *proposes* a verdict for an
answer, and it is counted only when a question was actually open. Time spent
is the time between your actions, at most MAX_GAP_SECONDS each, so a session
left open overnight does not count as hours of study.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from app.memory.db import Database

Difficulty = Literal["beginner", "intermediate", "advanced"]
Mode = Literal["learn", "revise", "practice", "quiz"]
Verdict = Literal["correct", "partial", "incorrect"]
DIFFICULTIES: tuple[str, ...] = ("beginner", "intermediate", "advanced")
MODES: tuple[str, ...] = ("learn", "revise", "practice", "quiz")
MAX_GAP_SECONDS = 5 * 60


class StudyNotFound(LookupError):
    """No study session with that id."""


@dataclass(frozen=True)
class StudySession:
    id: int
    subject: str
    topic: str
    difficulty: str
    goal: str
    mode: str
    course_id: int | None
    started_at: str
    last_active_at: str
    active_seconds: int
    correct: int
    partial: int
    incorrect: int
    open_question: str | None

    @property
    def answered(self) -> int:
        return self.correct + self.partial + self.incorrect

    @property
    def score_percent(self) -> int | None:
        """Correct counts 1, partial counts half."""
        if not self.answered:
            return None
        return round(100 * (self.correct + 0.5 * self.partial) / self.answered)

    @property
    def minutes(self) -> int:
        return round(self.active_seconds / 60)


@dataclass(frozen=True)
class StudyMessage:
    role: Literal["tutor", "student"]
    content: str
    verdict: str | None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


class StudyStore:
    def __init__(self, db: Database | Path | str) -> None:
        self.db = db if isinstance(db, Database) else Database(db)

    def start(self, subject: str, topic: str, difficulty: str, goal: str, mode: str,
              course_id: int | None = None) -> StudySession:
        if difficulty not in DIFFICULTIES or mode not in MODES:
            raise ValueError("Unknown difficulty or mode.")
        now = _stamp(_now())
        with self.db.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO study_sessions (subject, topic, difficulty, goal, mode, course_id, started_at,"
                " last_active_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (subject, topic, difficulty, goal, mode, course_id, now, now),
            )
            return self._get(conn, cursor.lastrowid)

    def get(self, session_id: int) -> StudySession:
        with self.db.connect() as conn:
            return self._get(conn, session_id)

    def recent(self, limit: int = 20) -> list[StudySession]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM study_sessions ORDER BY last_active_at DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [StudySession(**dict(row)) for row in rows]

    def messages(self, session_id: int) -> list[StudyMessage]:
        with self.db.connect() as conn:
            self._get(conn, session_id)
            rows = conn.execute(
                "SELECT role, content, verdict FROM study_messages WHERE session_id = ? ORDER BY id", (session_id,)
            ).fetchall()
        return [StudyMessage(**dict(row)) for row in rows]

    def touch(self, session_id: int, now: datetime | None = None) -> None:
        """Count the time since the last action (capped) as study time."""
        now = now or _now()
        with self.db.connect() as conn:
            session = self._get(conn, session_id)
            gap = (now - datetime.fromisoformat(session.last_active_at)).total_seconds()
            add = int(min(max(gap, 0), MAX_GAP_SECONDS))
            conn.execute(
                "UPDATE study_sessions SET active_seconds = active_seconds + ?, last_active_at = ? WHERE id = ?",
                (add, _stamp(now), session_id),
            )

    def add_student(self, session_id: int, text: str, verdict: str | None = None) -> None:
        self._add(session_id, "student", text, verdict)

    def add_tutor(self, session_id: int, text: str, open_question: str | None) -> None:
        """Store what the tutor said; open_question is what now waits for an answer."""
        self._add(session_id, "tutor", text, None)
        with self.db.connect() as conn:
            conn.execute("UPDATE study_sessions SET open_question = ? WHERE id = ?", (open_question, session_id))

    def record_verdict(self, session_id: int, verdict: str) -> None:
        if verdict not in ("correct", "partial", "incorrect"):
            raise ValueError("Unknown verdict.")
        with self.db.connect() as conn:
            self._get(conn, session_id)
            # verdict is one of three fixed column names, checked above.
            conn.execute(f"UPDATE study_sessions SET {verdict} = {verdict} + 1 WHERE id = ?", (session_id,))
            last = conn.execute(
                "SELECT id FROM study_messages WHERE session_id = ? AND role = 'student' ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if last is not None:
                conn.execute("UPDATE study_messages SET verdict = ? WHERE id = ?", (verdict, last["id"]))

    def set_mode(self, session_id: int, mode: str) -> None:
        if mode not in MODES:
            raise ValueError("Unknown mode.")
        with self.db.connect() as conn:
            self._get(conn, session_id)
            conn.execute("UPDATE study_sessions SET mode = ? WHERE id = ?", (mode, session_id))

    def delete(self, session_id: int) -> None:
        with self.db.connect() as conn:
            self._get(conn, session_id)
            conn.execute("DELETE FROM study_sessions WHERE id = ?", (session_id,))

    def seconds_since(self, since: datetime) -> int:
        """Study time in sessions active since a moment (e.g. today, this week)."""
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(active_seconds), 0) AS s FROM study_sessions WHERE last_active_at >= ?",
                (_stamp(since),),
            ).fetchone()
        return int(row["s"])

    def seconds_by_subject(self, since: datetime | None = None) -> dict[str, int]:
        since = since or _now() - timedelta(days=3650)
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT subject, SUM(active_seconds) AS s FROM study_sessions WHERE last_active_at >= ?"
                " GROUP BY subject ORDER BY s DESC",
                (_stamp(since),),
            ).fetchall()
        return {row["subject"]: int(row["s"]) for row in rows}

    def _add(self, session_id: int, role: str, text: str, verdict: str | None) -> None:
        with self.db.connect() as conn:
            self._get(conn, session_id)
            conn.execute(
                "INSERT INTO study_messages (session_id, role, content, verdict, created_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, role, text, verdict, _stamp(_now())),
            )

    @staticmethod
    def _get(conn: sqlite3.Connection, session_id: int | None) -> StudySession:
        row = conn.execute("SELECT * FROM study_sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise StudyNotFound(f"Study session {session_id} not found.")
        return StudySession(**dict(row))


class Preferences:
    """Small key-value settings chosen in the app (e.g. your name), not secrets."""

    def __init__(self, db: Database | Path | str) -> None:
        self.db = db if isinstance(db, Database) else Database(db)

    def get(self, key: str, default: str | None = None) -> str | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT value FROM preferences WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str | None) -> None:
        with self.db.connect() as conn:
            if value is None or value == "":
                conn.execute("DELETE FROM preferences WHERE key = ?", (key,))
            else:
                conn.execute(
                    "INSERT INTO preferences (key, value) VALUES (?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
