"""Flashcards with SM-2 spaced repetition (V0.6 tutor).

Cards are created only through the card_propose tool, after the user saw and
approved every card. Studying (/quiz) happens in the CLI without the model, so
card text never goes back into the model's context.

Grades are Anki-style buttons. Again, good and easy are SM-2 with quality
1, 4 and 5; hard follows Anki (interval x 1.2, ease - 0.15), see next_schedule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.memory.db import Database

GRADE_TO_QUALITY = {1: 1, 2: 3, 3: 4, 4: 5}
GRADE_NAMES = {1: "again", 2: "hard", 3: "good", 4: "easy"}
MIN_EASE = 1.3
# Anki's "hard": the interval grows by 20 %, the ease drops by 0.15.
HARD_FACTOR = 1.2
HARD_EASE_PENALTY = 0.15


class CardNotFound(LookupError):
    """No card with that id."""


@dataclass(frozen=True)
class Schedule:
    ease: float
    interval_days: int
    repetitions: int
    due_date: date


def next_schedule(current: Schedule, grade: int, today: date) -> Schedule:
    """SM-2: the interval grows with the *previous* ease, then the ease is updated.

    A failed answer (quality < 3, i.e. "again") restarts the card and makes it
    due today; the first two successes give 1 and 6 days.

    "hard" follows Anki instead: in pure SM-2 it still multiplies the interval by
    the ease (38 days -> 99), which is not what a hard answer should mean. Here
    the interval grows by HARD_FACTOR, at least one day (a new card: 1 day), and
    the ease drops by HARD_EASE_PENALTY.
    """
    if grade not in GRADE_TO_QUALITY:
        raise ValueError("grade must be 1 (again), 2 (hard), 3 (good) or 4 (easy)")
    if grade == 2:
        if current.repetitions == 0:
            interval = 1
        else:
            interval = max(current.interval_days + 1, round(current.interval_days * HARD_FACTOR))
        ease = max(MIN_EASE, current.ease - HARD_EASE_PENALTY)
        return Schedule(ease=round(ease, 4), interval_days=interval, repetitions=current.repetitions + 1,
                        due_date=today + timedelta(days=interval))
    quality = GRADE_TO_QUALITY[grade]
    if quality < 3:
        repetitions, interval = 0, 0
    else:
        repetitions = current.repetitions + 1
        if repetitions == 1:
            interval = 1
        elif repetitions == 2:
            interval = 6
        else:
            interval = max(1, round(current.interval_days * current.ease))
    miss = 5 - quality
    ease = max(MIN_EASE, current.ease + 0.1 - miss * (0.08 + miss * 0.02))
    return Schedule(ease=round(ease, 4), interval_days=interval, repetitions=repetitions,
                    due_date=today + timedelta(days=interval))


@dataclass(frozen=True)
class NewCard:
    front: str
    back: str
    source: str | None = None


@dataclass(frozen=True)
class Card:
    id: int
    course_id: int | None
    front: str
    back: str
    source: str | None
    ease: float
    interval_days: int
    repetitions: int
    due_date: str
    created_at: str
    course: str | None = None

    @property
    def schedule(self) -> Schedule:
        return Schedule(self.ease, self.interval_days, self.repetitions, date.fromisoformat(self.due_date))


@dataclass(frozen=True)
class CardStats:
    total: int
    due: int
    reviewed_today: int
    by_course: list[tuple[str | None, int, int]]  # (course code, total, due)


_SELECT = "SELECT k.*, c.code AS course FROM cards k LEFT JOIN courses c ON c.id = k.course_id"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CardStore:
    def __init__(self, db: Database | Path | str) -> None:
        self.db = db if isinstance(db, Database) else Database(db)

    def add_many(self, cards: list[NewCard], course_id: int | None = None,
                 today: date | None = None) -> tuple[list[int], int]:
        """Save cards due today. Returns (new ids, skipped duplicates): a card whose
        question already exists in the same course (ignoring case) is skipped."""
        today = today or date.today()
        created: list[int] = []
        skipped = 0
        with self.db.connect() as conn:
            seen = {
                row[0]
                for row in conn.execute(
                    "SELECT lower(front) FROM cards WHERE course_id IS ?", (course_id,)
                )
            }
            for card in cards:
                key = card.front.lower()
                if key in seen:
                    skipped += 1
                    continue
                seen.add(key)
                cursor = conn.execute(
                    "INSERT INTO cards (course_id, front, back, source, due_date, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (course_id, card.front, card.back, card.source, today.isoformat(), _now()),
                )
                created.append(int(cursor.lastrowid))
        return created, skipped

    def due(self, course_id: int | None = None, limit: int = 20, today: date | None = None) -> list[Card]:
        today = today or date.today()
        course_filter = "AND k.course_id = ?" if course_id is not None else ""
        params = (today.isoformat(), course_id, limit) if course_id is not None else (today.isoformat(), limit)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"{_SELECT} WHERE k.due_date <= ? {course_filter} ORDER BY k.due_date, k.id LIMIT ?",
                params,
            ).fetchall()
        return [Card(**dict(row)) for row in rows]

    def due_counts(self, start: date, days: int) -> dict[date, int]:
        """Cards due on each day from start; start also counts everything overdue."""
        end = start + timedelta(days=days - 1)
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT max(due_date, ?) AS day, COUNT(*) FROM cards WHERE due_date <= ? GROUP BY day",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        return {date.fromisoformat(row[0]): int(row[1]) for row in rows}

    def next_due_date(self, course_id: int | None = None) -> str | None:
        course_filter = "WHERE course_id = ?" if course_id is not None else ""
        with self.db.connect() as conn:
            row = conn.execute(
                f"SELECT MIN(due_date) FROM cards {course_filter}",
                (course_id,) if course_id is not None else (),
            ).fetchone()
        return row[0]

    def review(self, card_id: int, grade: int, today: date | None = None) -> Card:
        """Record one answer and reschedule the card, in one transaction."""
        today = today or date.today()
        with self.db.connect() as conn:
            card = self._get(conn, card_id)
            new = next_schedule(card.schedule, grade, today)
            conn.execute(
                "UPDATE cards SET ease = ?, interval_days = ?, repetitions = ?, due_date = ? WHERE id = ?",
                (new.ease, new.interval_days, new.repetitions, new.due_date.isoformat(), card_id),
            )
            conn.execute(
                "INSERT INTO reviews (card_id, grade, reviewed_on, reviewed_at) VALUES (?, ?, ?, ?)",
                (card_id, grade, today.isoformat(), _now()),
            )
            return self._get(conn, card_id)

    def stats(self, course_id: int | None = None, today: date | None = None) -> CardStats:
        today_s = (today or date.today()).isoformat()
        course_filter = "WHERE k.course_id = ?" if course_id is not None else ""
        params: tuple = (course_id,) if course_id is not None else ()
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT c.code, COUNT(*) AS total, SUM(k.due_date <= ?) AS due
                  FROM cards k LEFT JOIN courses c ON c.id = k.course_id
                  {course_filter}
                 GROUP BY k.course_id ORDER BY c.code
                """,
                (today_s, *params),
            ).fetchall()
            reviewed = conn.execute(
                f"""
                SELECT COUNT(*) FROM reviews r JOIN cards k ON k.id = r.card_id
                 WHERE r.reviewed_on = ? {'AND k.course_id = ?' if course_id is not None else ''}
                """,
                (today_s, *params),
            ).fetchone()[0]
        by_course = [(row[0], int(row[1]), int(row[2] or 0)) for row in rows]
        return CardStats(
            total=sum(total for _, total, _ in by_course),
            due=sum(due for _, _, due in by_course),
            reviewed_today=int(reviewed),
            by_course=by_course,
        )

    def list(self, course_id: int | None = None, limit: int = 100) -> list[Card]:
        course_filter = "WHERE k.course_id = ?" if course_id is not None else ""
        params = (course_id, limit) if course_id is not None else (limit,)
        with self.db.connect() as conn:
            rows = conn.execute(f"{_SELECT} {course_filter} ORDER BY k.due_date, k.id LIMIT ?", params).fetchall()
        return [Card(**dict(row)) for row in rows]

    def get(self, card_id: int) -> Card:
        with self.db.connect() as conn:
            return self._get(conn, card_id)

    def forget(self, card_id: int) -> None:
        """Delete a card and its review history permanently."""
        with self.db.connect() as conn:
            self._get(conn, card_id)
            conn.execute("DELETE FROM cards WHERE id = ?", (card_id,))

    @staticmethod
    def _get(conn, card_id: int | None) -> Card:
        row = conn.execute(f"{_SELECT} WHERE k.id = ?", (card_id,)).fetchone()
        if row is None:
            raise CardNotFound(f"Card {card_id} not found.")
        return Card(**dict(row))
