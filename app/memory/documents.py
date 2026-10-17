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
CATEGORIES: tuple[str, ...] = ("notes", "assignment", "timetable", "classwork", "other")


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
    course_id: int | None = None
    category: str = "notes"
    course: str | None = None  # the course code, for display

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]


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
             chunks: list[Chunk], course_id: int | None = None) -> ImportStatus:
        """Insert or replace one document and all its chunks, atomically.

        A changed document keeps its course unless a new course_id is given; an
        unchanged one only gets its course updated if one is given.
        """
        with self.db.connect() as conn:
            row = conn.execute("SELECT id, sha256, course_id FROM documents WHERE path = ?", (path,)).fetchone()
            if row is not None and row["sha256"] == sha256:
                if course_id is not None and course_id != row["course_id"]:
                    conn.execute("UPDATE documents SET course_id = ? WHERE id = ?", (course_id, row["id"]))
                return "unchanged"
            if row is not None and course_id is None:
                course_id = row["course_id"]
            if row is not None:
                # Updated in place, so the id (and the category and study-session
                # links that point at it) stay. Chunks are deleted one by one so
                # their FTS delete trigger runs for every row.
                document_id = row["id"]
                conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
                conn.execute(
                    "UPDATE documents SET kind = ?, sha256 = ?, size_bytes = ?, pages = ?, imported_at = ?,"
                    " course_id = ? WHERE id = ?",
                    (kind, sha256, size_bytes, pages, _now(), course_id, document_id),
                )
            else:
                document_id = conn.execute(
                    "INSERT INTO documents (path, kind, sha256, size_bytes, pages, imported_at, course_id)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (path, kind, sha256, size_bytes, pages, _now(), course_id),
                ).lastrowid
            conn.executemany(
                "INSERT INTO chunks (document_id, seq, page, line, text) VALUES (?, ?, ?, ?, ?)",
                [(document_id, seq, c.page, c.line, c.text) for seq, c in enumerate(chunks)],
            )
            return "updated" if row is not None else "imported"

    def search(self, query: str, limit: int = 5, course_id: int | None = None) -> list[Hit]:
        match = fts_query(query)
        if match is None:
            return []
        course_filter = "AND d.course_id = ?" if course_id is not None else ""
        params = (match, course_id, limit) if course_id is not None else (match, limit)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT d.id AS document_id, d.path, c.page, c.line, c.text
                  FROM chunks_fts
                  JOIN chunks c ON c.id = chunks_fts.rowid
                  JOIN documents d ON d.id = c.document_id
                 WHERE chunks_fts MATCH ? {course_filter}
                 ORDER BY bm25(chunks_fts), c.id
                 LIMIT ?
                """,
                params,
            ).fetchall()
        return [Hit(**dict(row)) for row in rows]

    def list(self) -> list[Document]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT d.*, COUNT(ch.id) AS chunk_count, co.code AS course
                  FROM documents d
                  LEFT JOIN chunks ch ON ch.document_id = d.id
                  LEFT JOIN courses co ON co.id = d.course_id
                 GROUP BY d.id ORDER BY d.path
                """
            ).fetchall()
        return [Document(**dict(row)) for row in rows]

    def by_path(self, path: str) -> Document:
        for document in self.list():
            if document.path == path:
                return document
        raise DocumentNotFound(f"Document {path} not found.")

    def set_category(self, document_id: int, category: str) -> None:
        if category not in CATEGORIES:
            raise ValueError("Unknown category.")
        with self.db.connect() as conn:
            if conn.execute("UPDATE documents SET category = ? WHERE id = ?", (category, document_id)).rowcount == 0:
                raise DocumentNotFound(f"Document {document_id} not found.")

    def set_course(self, document_id: int, course_id: int | None) -> None:
        with self.db.connect() as conn:
            if conn.execute("UPDATE documents SET course_id = ? WHERE id = ?", (course_id, document_id)).rowcount == 0:
                raise DocumentNotFound(f"Document {document_id} not found.")

    def chunks(self, document_id: int, limit: int = 10_000) -> list[Hit]:
        """The document's chunks in reading order, as hits with citations."""
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT d.id AS document_id, d.path, c.page, c.line, c.text FROM chunks c"
                " JOIN documents d ON d.id = c.document_id WHERE d.id = ? ORDER BY c.seq LIMIT ?",
                (document_id, limit),
            ).fetchall()
        return [Hit(**dict(row)) for row in rows]

    def search_in(self, query: str, document_ids: list[int], limit: int = 6) -> list[Hit]:
        """Keyword search limited to some documents (a study session's files)."""
        match = fts_query(query)
        if match is None or not document_ids:
            return []
        marks = ",".join("?" for _ in document_ids)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT d.id AS document_id, d.path, c.page, c.line, c.text
                  FROM chunks_fts
                  JOIN chunks c ON c.id = chunks_fts.rowid
                  JOIN documents d ON d.id = c.document_id
                 WHERE chunks_fts MATCH ? AND d.id IN ({marks})
                 ORDER BY bm25(chunks_fts), c.id
                 LIMIT ?
                """,
                (match, *document_ids, limit),
            ).fetchall()
        return [Hit(**dict(row)) for row in rows]

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
