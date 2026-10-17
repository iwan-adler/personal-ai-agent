"""Long-term facts the user told the agent ("I study CS, 3rd semester").

Saved only through the fact_save tool, which always asks first: a fact outlives
the session, so a planted one would act in every later session. Searched with
SQLite FTS5 keyword search (D4). Deleted only by the user's /forget-fact
command, never by a model tool.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.memory.db import Database

_WORD = re.compile(r"\w+", re.UNICODE)
MAX_QUERY_WORDS = 12


class FactNotFound(LookupError):
    """No fact with that id."""


@dataclass(frozen=True)
class Fact:
    id: int
    text: str
    topic: str | None
    created_at: str
    course_id: int | None = None
    course: str | None = None  # the course code, for display


_COLUMNS = "f.*, c.code AS course"
_COURSE_JOIN = "LEFT JOIN courses c ON c.id = f.course_id"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fts_query(text: str) -> str | None:
    """Plain words, each quoted, joined with OR; None if there are no words.

    FTS5 has its own query syntax (quotes, OR/NOT/NEAR, *, column:filters), and
    raw text from the model would raise SQL errors or act as operators. Quoting
    every word makes the search a plain keyword search; bm25 ranks the matches.
    """
    words = _WORD.findall(text)[:MAX_QUERY_WORDS]
    if not words:
        return None
    return " OR ".join(f'"{word}"' for word in words)


class FactStore:
    def __init__(self, db: Database | Path | str) -> None:
        self.db = db if isinstance(db, Database) else Database(db)

    def add(self, text: str, topic: str | None = None, course_id: int | None = None) -> Fact:
        with self.db.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO facts (text, topic, created_at, course_id) VALUES (?, ?, ?, ?)",
                (text, topic, _now(), course_id),
            )
            return self._get(conn, cursor.lastrowid)

    def search(self, query: str, limit: int = 5, course_id: int | None = None) -> list[Fact]:
        match = fts_query(query)
        if match is None:
            return []
        course_filter = "AND f.course_id = ?" if course_id is not None else ""
        params = (match, course_id, limit) if course_id is not None else (match, limit)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT {_COLUMNS} FROM facts_fts
                  JOIN facts f ON f.id = facts_fts.rowid
                  {_COURSE_JOIN}
                 WHERE facts_fts MATCH ? {course_filter}
                 ORDER BY bm25(facts_fts), f.id DESC
                 LIMIT ?
                """,
                params,
            ).fetchall()
        return [Fact(**dict(row)) for row in rows]

    def list(self, topic: str | None = None, limit: int = 20) -> list[Fact]:
        """Newest first, optionally only one topic."""
        where, params = ("WHERE f.topic = ?", (topic,)) if topic else ("", ())
        with self.db.connect() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM facts f {_COURSE_JOIN} {where} ORDER BY f.id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [Fact(**dict(row)) for row in rows]

    def get(self, fact_id: int) -> Fact:
        with self.db.connect() as conn:
            return self._get(conn, fact_id)

    def forget(self, fact_id: int) -> None:
        """Delete a fact permanently, from the table and the search index."""
        with self.db.connect() as conn:
            self._get(conn, fact_id)
            conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,))

    @staticmethod
    def _get(conn, fact_id: int | None) -> Fact:
        row = conn.execute(f"SELECT {_COLUMNS} FROM facts f {_COURSE_JOIN} WHERE f.id = ?", (fact_id,)).fetchone()
        if row is None:
            raise FactNotFound(f"Fact {fact_id} not found.")
        return Fact(**dict(row))
