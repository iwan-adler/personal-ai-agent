"""The document library: imported files, their chunks, and keyword search (D4).

A document is identified by its path relative to the workspace's library/
folder. Re-importing an unchanged file (same sha256) does nothing; a changed
one replaces all its chunks in one transaction. Every search hit carries a
ready-made citation ("lecture3.pdf, p. 12" or "notes.md, line 40"), so the
model never has to invent one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from app.memory.db import Database
from app.memory.facts import fts_query

ImportStatus = Literal["imported", "updated", "unchanged"]


class DocumentNotFound(LookupError):
    """No document with that id."""


@dataclass(frozen=True)
class Chunk:
    page: int | None
    line: int | None
    text: str


@dataclass(frozen=True)
class Document:
    id: int
    path: str
    kind: str
    sha256: str
    size_bytes: int
    pages: int | None
    imported_at: str
    chunk_count: int


@dataclass(frozen=True)
class Hit:
    document_id: int
    path: str
    page: int | None
    line: int | None
    text: str

    @property
    def citation(self) -> str:
        if self.page is not None:
            return f"{self.path}, p. {self.page}"
        if self.line is not None:
            return f"{self.path}, line {self.line}"
        return self.path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DocumentStore:
    def __init__(self, db: Database | Path | str) -> None:
        self.db = db if isinstance(db, Database) else Database(db)

    def sha256_of(self, path: str) -> str | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT sha256 FROM documents WHERE path = ?", (path,)).fetchone()
        return row["sha256"] if row else None

    def save(self, path: str, kind: str, sha256: str, size_bytes: int, pages: int | None,
             chunks: list[Chunk]) -> ImportStatus:
        """Insert or replace one document and all its chunks, atomically."""
        with self.db.connect() as conn:
            row = conn.execute("SELECT id, sha256 FROM documents WHERE path = ?", (path,)).fetchone()
            if row is not None and row["sha256"] == sha256:
                return "unchanged"
            if row is not None:
                # Chunks first, so their FTS delete trigger runs for every row.
                conn.execute("DELETE FROM chunks WHERE document_id = ?", (row["id"],))
                conn.execute("DELETE FROM documents WHERE id = ?", (row["id"],))
            cursor = conn.execute(
                "INSERT INTO documents (path, kind, sha256, size_bytes, pages, imported_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (path, kind, sha256, size_bytes, pages, _now()),
            )
            conn.executemany(
                "INSERT INTO chunks (document_id, seq, page, line, text) VALUES (?, ?, ?, ?, ?)",
                [(cursor.lastrowid, seq, c.page, c.line, c.text) for seq, c in enumerate(chunks)],
            )
            return "updated" if row is not None else "imported"

    def search(self, query: str, limit: int = 5) -> list[Hit]:
        match = fts_query(query)
        if match is None:
            return []
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT d.id AS document_id, d.path, c.page, c.line, c.text
                  FROM chunks_fts
                  JOIN chunks c ON c.id = chunks_fts.rowid
                  JOIN documents d ON d.id = c.document_id
                 WHERE chunks_fts MATCH ?
                 ORDER BY bm25(chunks_fts), c.id
                 LIMIT ?
                """,
                (match, limit),
            ).fetchall()
        return [Hit(**dict(row)) for row in rows]

    def list(self) -> list[Document]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT d.*, COUNT(c.id) AS chunk_count
                  FROM documents d LEFT JOIN chunks c ON c.document_id = d.id
                 GROUP BY d.id ORDER BY d.path
                """
            ).fetchall()
        return [Document(**dict(row)) for row in rows]

    def get(self, document_id: int) -> Document:
        for document in self.list():
            if document.id == document_id:
                return document
        raise DocumentNotFound(f"Document {document_id} not found.")

    def forget(self, document_id: int) -> None:
        """Delete a document, its chunks and their search entries permanently."""
        with self.db.connect() as conn:
            if conn.execute("SELECT 1 FROM documents WHERE id = ?", (document_id,)).fetchone() is None:
                raise DocumentNotFound(f"Document {document_id} not found.")
            conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
            conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
