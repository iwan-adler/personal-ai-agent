"""Confining file tools to one folder (WORKSPACE_DIR).

Two layers, because each catches what the other cannot:

1. check_relative_path (no disk access) runs inside the tool's input model, so
   the safety layer denies a bad path during review, before anything runs or
   any confirmation is asked. It rejects absolute paths, drives, UNC/device
   paths, "..", ":" (alternate data streams), Windows device names, names
   ending in a dot or space (Windows strips them: ".env." opens ".env") and
   off-limits names such as .env or *.pem.
2. resolve_in_workspace (on disk) runs in the handler: it follows symlinks and
   junctions to the real target and requires it to stay inside the workspace,
   re-checks off-limits names on the resolved path, and refuses files with
   several hard links, which can point outside without resolve() noticing.

Error messages never repeat the path: paths are tool arguments, and arguments
stay out of the audit log.

Known gap: between the check and the open, another program on the computer
could swap a file for a link. The agent itself cannot create links.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

MAX_PATH_CHARS = 260

_SEPARATORS = re.compile(r"[\\/]+")
_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{prefix}{digit}" for prefix in ("COM", "LPT") for digit in "123456789¹²³"
}
_BLOCKED_EXACT = {".env", ".git", ".ssh"}
_BLOCKED_PREFIXES = (".env.", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
_BLOCKED_SUFFIXES = (".pem", ".key")


class WorkspaceError(ValueError):
    """A path is not allowed, or leads outside the workspace."""


def is_blocked_name(name: str) -> bool:
    """Off-limits even inside the workspace: secrets, keys and git internals."""
    lowered = name.lower()
    return (
        lowered in _BLOCKED_EXACT
        or lowered.startswith(_BLOCKED_PREFIXES)
        or lowered.endswith(_BLOCKED_SUFFIXES)
    )


def check_relative_path(raw: str) -> str:
    """Lexical checks only. Returns the path with "/" separators and no "." parts."""
    if not raw or len(raw) > MAX_PATH_CHARS:
        raise WorkspaceError(f"Path must be 1-{MAX_PATH_CHARS} characters.")
    if "\x00" in raw:
        raise WorkspaceError("Path contains a null character.")
    if ":" in raw:
        raise WorkspaceError("Paths may not contain ':' (drive letters or alternate data streams).")
    if raw.startswith(("/", "\\")):
        raise WorkspaceError("Use a path relative to the workspace, not an absolute one.")
    parts: list[str] = []
    for part in _SEPARATORS.split(raw):
        if part in ("", "."):
            continue
        if part == "..":
            raise WorkspaceError("Paths may not contain '..'.")
        if part != part.rstrip(". "):
            raise WorkspaceError("Names may not end with a dot or a space.")
        if part.split(".", 1)[0].upper() in _RESERVED:
            raise WorkspaceError("That name is reserved by Windows.")
        if is_blocked_name(part):
            raise WorkspaceError("That file is off-limits.")
        parts.append(part)
    return "/".join(parts) or "."


def resolve_in_workspace(root: Path, raw: str) -> Path:
    """The real path of an existing entry inside root, after the lexical checks."""
    relative = check_relative_path(raw)
    root = root.resolve(strict=True)
    try:
        resolved = (root / relative).resolve(strict=True)
    except FileNotFoundError:
        raise WorkspaceError("No such file or folder in the workspace.") from None
    if not resolved.is_relative_to(root):
        raise WorkspaceError("Path leads outside the workspace.")
    # A junction or 8.3 short name can reach an off-limits name the input did not show.
    if any(is_blocked_name(part) for part in resolved.relative_to(root).parts):
        raise WorkspaceError("That file is off-limits.")
    if resolved.is_file() and os.stat(resolved).st_nlink > 1:
        raise WorkspaceError("Files with several hard links are not allowed.")
    return resolved


def is_visible_entry(root: Path, entry: Path) -> bool:
    """Whether a directory entry may be listed: not off-limits, and a link only if
    its target stays inside the workspace."""
    if is_blocked_name(entry.name):
        return False
    try:
        target = entry.resolve(strict=True)
    except OSError:
        return False
    return target.is_relative_to(root.resolve(strict=True))
