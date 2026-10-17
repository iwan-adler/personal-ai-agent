"""Your study materials: files you upload in the window, saved and imported.

An upload is written to WORKSPACE_DIR/library/<course code or "general">/ and
then imported with the same code as the doc_import tool (isolated parsers,
page or line citations). Rules, each closing a hole an upload could open:

- the file name is reduced to a plain name (letters, digits, space, . _ - ( ));
  folders, drive letters, "..", device names and off-limits names never get
  through, and the final path is re-checked inside library/;
- only the importable types are accepted, by suffix (no executables);
- sizes are capped while reading, before anything is written;
- an existing file is never overwritten: a new name like "notes (2).pdf" is used;
- the file is written to a temporary name first, so a failed upload leaves nothing.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.memory.documents import CATEGORIES, Document, DocumentStore
from app.tools.docs import LIBRARY, MAX_PDF_BYTES, MAX_TEXT_BYTES, STORED_KIND, SUPPORTED, file_chunks
from app.tools.workspace import WorkspaceError, is_blocked_name, resolve_in_workspace

MAX_NAME_CHARS = 100
GENERAL = "general"
_UNSAFE = re.compile(r"[^A-Za-z0-9 ._()\-]+")
_WINDOWS_DEVICES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


class UploadError(ValueError):
    """The upload was refused; the message is shown to you."""


@dataclass(frozen=True)
class Uploaded:
    document: Document
    status: str  # imported | updated | unchanged


def safe_name(filename: str) -> str:
    """A plain file name from whatever the browser sent, or UploadError."""
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]  # never a folder or drive
    name = _UNSAFE.sub("_", name).strip(" .")
    stem, dot, suffix = name.rpartition(".")
    if not dot or not stem:
        raise UploadError("The file needs a name and a type such as .pdf.")
    if f".{suffix.lower()}" not in SUPPORTED:
        raise UploadError(f"{suffix.lower()} files are not supported. Use .pdf, .docx, .pptx, .txt or .md.")
    stem = stem.strip(" .")[: MAX_NAME_CHARS - len(suffix) - 1] or "file"
    if stem.lower() in _WINDOWS_DEVICES or is_blocked_name(f"{stem}.{suffix}"):
        raise UploadError("That file name is not allowed.")
    return f"{stem}.{suffix.lower()}"


def max_bytes_for(name: str) -> int:
    kind = SUPPORTED[Path(name).suffix.lower()]
    return MAX_TEXT_BYTES if kind == "text" else MAX_PDF_BYTES


def _free_name(folder: Path, name: str, data_sha: str) -> str:
    """The name to use: the same name if that file has the same content, else a numbered one."""
    stem, suffix = name.rsplit(".", 1)
    for n in range(1, 1000):
        candidate = name if n == 1 else f"{stem} ({n}).{suffix}"
        target = folder / candidate
        if not target.exists():
            return candidate
        if hashlib.sha256(target.read_bytes()).hexdigest() == data_sha:
            return candidate  # uploading the same file again: reuse it
    raise UploadError("Too many files with that name.")


def _library(root: Path) -> Path:
    """WORKSPACE_DIR/library, created if missing, and checked to stay inside the workspace."""
    try:
        (root / LIBRARY).mkdir(exist_ok=True)
        return resolve_in_workspace(root, LIBRARY)
    except (OSError, WorkspaceError):
        raise UploadError("The library folder in your workspace is not usable.") from None


def save_upload(root: Path, store: DocumentStore, filename: str, data: bytes, course_code: str | None,
                course_id: int | None, category: str) -> Uploaded:
    if category not in CATEGORIES:
        raise UploadError("Unknown category.")
    name = safe_name(filename)
    if not data:
        raise UploadError(f"{name} is empty.")
    if len(data) > max_bytes_for(name):
        raise UploadError(f"{name} is too large (max {max_bytes_for(name) // 1_000_000} MB).")
    library = _library(root)
    folder_name = course_code if course_code and course_code.lower() not in _WINDOWS_DEVICES else GENERAL
    (library / folder_name).mkdir(exist_ok=True)
    folder = resolve_in_workspace(root, f"{LIBRARY}/{folder_name}")
    if not folder.is_relative_to(library):
        raise UploadError("The library folder is not usable.")
    sha = hashlib.sha256(data).hexdigest()
    final_name = _free_name(folder, name, sha)
    target = folder / final_name
    if not target.resolve().is_relative_to(library):
        raise UploadError("That file name is not allowed.")
    created = not target.exists()
    if created:
        fd, temp = tempfile.mkstemp(dir=folder, prefix=".upload-", suffix=".part")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(temp, target)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise

    rel = f"{folder_name}/{final_name}"
    kind = SUPPORTED[target.suffix.lower()]
    if store.sha256_of(rel) == sha:
        status = "unchanged"
    else:
        try:
            chunks, pages = file_chunks(target, kind, data)
        except WorkspaceError as exc:
            if created:
                target.unlink(missing_ok=True)
            raise UploadError(f"{final_name} could not be read ({exc}).") from None
        if not chunks:
            if created:
                target.unlink(missing_ok=True)
            raise UploadError(f"{final_name} has no text Koda can read (a scanned PDF or a photo?).")
        status = store.save(rel, STORED_KIND[kind], sha, len(data), pages, chunks, course_id)
    document = store.by_path(rel)
    if course_id is not None and document.course_id != course_id:
        store.set_course(document.id, course_id)
    store.set_category(document.id, category)
    return Uploaded(store.by_path(rel), status)


def remove_material(root: Path, store: DocumentStore, document_id: int) -> str:
    """Remove a document from Koda and delete its file from library/. Returns its name."""
    document = store.get(document_id)
    store.forget(document_id)
    try:
        real = resolve_in_workspace(root, f"{LIBRARY}/{document.path}")
        if real.is_relative_to(resolve_in_workspace(root, LIBRARY)) and real.is_file():
            real.unlink()
    except WorkspaceError:
        pass  # the file is gone or not reachable: the library entry is removed anyway
    return document.name
