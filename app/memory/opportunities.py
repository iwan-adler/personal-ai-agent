"""Opportunities found by the scout: a local cache of matching feed entries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from app.memory.db import Database

Status = Literal["new", "saved", "dismissed"]
StatusFilter = Literal["new", "saved", "dismissed", "all"]


class OpportunityNotFound(LookupError):
    """No opportunity with that id."""


@dataclass(frozen=True)
class Opportunity:
    id: int
    link: str
    title: str
    summary: str
    feed_url: str
    published: str | None
    matched: str
    status: str
    found_at: str
    task_id: int | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class OpportunityStore:
    def __init__(self, db: Database | Path | str) -> None:
        self.db = db if isinstance(db, Database) else Database(db)

    def add_if_new(self, link: str, title: str, summary: str, feed_url: str,
                   published: str | None, matched: list[str]) -> int | None:
        """Store one entry unless its link is known already (in any status).
        Returns the new id, or None for a known link."""
        with self.db.connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO opportunities (link, title, summary, feed_url, published, matched, found_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (link, title, summary, feed_url, published, ",".join(matched), _now()),
            )
            return int(cursor.lastrowid) if cursor.rowcount == 1 else None

    def list(self, status: StatusFilter = "new", limit: int = 20) -> list[Opportunity]:
        where, params = ("", ()) if status == "all" else ("WHERE status = ?", (status,))
        with self.db.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM opportunities {where} ORDER BY id DESC LIMIT ?", (*params, limit)
            ).fetchall()
        return [Opportunity(**dict(row)) for row in rows]

    def get(self, opportunity_id: int) -> Opportunity:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM opportunities WHERE id = ?", (opportunity_id,)).fetchone()
        if row is None:
            raise OpportunityNotFound(f"Opportunity {opportunity_id} not found.")
        return Opportunity(**dict(row))

    def set_status(self, opportunity_id: int, status: Status, task_id: int | None = None) -> Opportunity:
        self.get(opportunity_id)
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE opportunities SET status = ?, task_id = COALESCE(?, task_id) WHERE id = ?",
                (status, task_id, opportunity_id),
            )
        return self.get(opportunity_id)
