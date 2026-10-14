"""Persistent chat sessions, so a conversation can be resumed after a restart.

Only what the user typed and the agent's final replies are stored. Tool calls,
tool results and repair attempts are not: an instruction planted in an old tool
result must not come back in a later session ("memory poisoning"), and what the
tools found lives in their own tables anyway.

Privacy: messages are stored unencrypted in data/agent.db (gitignored). The
audit log never gets their content, only session ids.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from app.conversation import Conversation
from app.memory.db import Database

PREVIEW_CHARS = 60


class SessionNotFound(LookupError):
    """No session with that id."""


@dataclass(frozen=True)
class SessionInfo:
    id: int
    started_at: str
    last_active_at: str
    message_count: int
    preview: str  # start of the first user message, for the CLI listing


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SessionStore:
    def __init__(self, db: Database | Path | str) -> None:
        self.db = db if isinstance(db, Database) else Database(db)

    def start(self) -> int:
        now = _now()
        with self.db.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO sessions (started_at, last_active_at) VALUES (?, ?)", (now, now)
            )
            return int(cursor.lastrowid)

    def record_turn(self, session_id: int, user_text: str, reply: str) -> None:
        """Store one exchange: what the user typed and the final reply shown."""
        now = _now()
        with self.db.connect() as conn:
            self._require(conn, session_id)
            conn.executemany(
                "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                [(session_id, "user", user_text, now), (session_id, "assistant", reply, now)],
            )
            conn.execute("UPDATE sessions SET last_active_at = ? WHERE id = ?", (now, session_id))

    def messages(self, session_id: int) -> list[tuple[Literal["user", "assistant"], str]]:
        with self.db.connect() as conn:
            self._require(conn, session_id)
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id", (session_id,)
            ).fetchall()
        return [(row["role"], row["content"]) for row in rows]

    def restore(self, session_id: int) -> Conversation:
        """A fresh Conversation (default system prompt) holding the stored exchanges."""
        conv = Conversation()
        for role, content in self.messages(session_id):
            if role == "user":
                conv.add_user(content)
            else:
                conv.add_assistant(content)
        return conv

    def recent(self, limit: int = 10) -> list[SessionInfo]:
        """Sessions with at least one message, most recently active first."""
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT s.id, s.started_at, s.last_active_at,
                       COUNT(m.id) AS message_count,
                       (SELECT content FROM messages
                         WHERE session_id = s.id AND role = 'user' ORDER BY id LIMIT 1) AS first
                  FROM sessions s JOIN messages m ON m.session_id = s.id
                 GROUP BY s.id
                 ORDER BY s.last_active_at DESC, s.id DESC
                 LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            SessionInfo(
                id=row["id"],
                started_at=row["started_at"],
                last_active_at=row["last_active_at"],
                message_count=row["message_count"],
                preview=_preview(row["first"] or ""),
            )
            for row in rows
        ]

    def forget(self, session_id: int) -> None:
        """Delete a session and all its messages permanently."""
        with self.db.connect() as conn:
            self._require(conn, session_id)
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    @staticmethod
    def _require(conn, session_id: int) -> None:
        if conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone() is None:
            raise SessionNotFound(f"Session {session_id} not found.")


def _preview(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + "…"
