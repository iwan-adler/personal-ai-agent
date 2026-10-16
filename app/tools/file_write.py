"""File write tools, confined to WORKSPACE_DIR like the read tools.

Risk choices (risk is fixed per tool, hence two tools):
- file_create makes a new file only -> CONFIRMATION, permission files.write.
- file_overwrite replaces an existing file -> HIGH_RISK: always asks, never
  grantable. It requires expected_sha256 from file_read, so the model can only
  overwrite a file it has read, and only if nobody changed it since.
- Both show a preview in the prompt (content, or a diff) and neither returns
  file content, so their output is not outside content.
- No delete, append or mkdir tool. Script and executable types that Windows runs
  on double-click are never written, so an injection cannot plant one.

Writes are atomic: the content goes to a temporary file in the same folder,
which is then linked (create: fails if the name appeared meanwhile) or swapped
in with os.replace (overwrite). Before an overwrite, the old version is copied
to a backup folder outside the workspace; only the newest BACKUPS_KEPT remain.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.tools.base import RiskLevel, Tool, ToolRegistry
from app.tools.workspace import TEMP_PREFIX, WorkspaceError, check_relative_path, resolve_in_workspace

MAX_CONTENT_BYTES = 200_000
PREVIEW_CONTENT_LINES = 60
PREVIEW_DIFF_LINES = 200
BACKUPS_KEPT = 20

# Types Windows runs (or opens as a shortcut) on double-click.
BLOCKED_WRITE_SUFFIXES = (
    ".exe", ".com", ".bat", ".cmd", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf",
    ".wsh", ".hta", ".scr", ".lnk", ".url", ".msi", ".reg", ".cpl", ".pif", ".dll",
)


class _WriteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="File relative to the workspace; its folder must exist.")
    content: str = Field(description="The complete new file content (UTF-8 text).")

    @field_validator("path")
    @classmethod
    def _writable_path(cls, value: str) -> str:
        # During the safety review: a bad target is denied before any prompt.
        try:
            relative = check_relative_path(value)
        except WorkspaceError as exc:
            raise ValueError(str(exc)) from None
        if relative == ".":
            raise ValueError("Path must name a file.")
        if relative.lower().endswith(BLOCKED_WRITE_SUFFIXES):
            raise ValueError("That file type runs on double-click; the agent may not write it.")
        return relative

    @field_validator("content")
    @classmethod
    def _content_size(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_CONTENT_BYTES:
            raise ValueError(f"Content is larger than {MAX_CONTENT_BYTES // 1000} KB.")
        return value


class FileCreateInput(_WriteInput):
    pass


class FileOverwriteInput(_WriteInput):
    expected_sha256: str = Field(
        pattern=r"^[0-9a-f]{64}$",
        description="sha256 from file_read of the current version.",
    )


class FileWriteOutput(BaseModel):
    path: str
    bytes_written: int
    sha256: str
    backed_up: bool


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _split(relative: str) -> tuple[str, str]:
    folder, _, name = relative.rpartition("/")
    return folder or ".", name


def _write_temp(folder: Path, data: bytes) -> Path:
    temp = folder / f"{TEMP_PREFIX}{uuid.uuid4().hex}"
    with open(temp, "xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return temp


def _create(root: Path, args: FileCreateInput) -> FileWriteOutput:
    folder_rel, name = _split(args.path)
    folder = resolve_in_workspace(root, folder_rel)
    if not folder.is_dir():
        raise WorkspaceError("The folder does not exist.")
    target = folder / name
    if os.path.lexists(target):
        raise WorkspaceError("A file with that name already exists; use file_overwrite.")
    data = args.content.encode("utf-8")
    temp = _write_temp(folder, data)
    try:
        # A hard link fails if the name appeared since the check above, so this
        # can never replace an existing file.
        os.link(temp, target)
    except FileExistsError:
        raise WorkspaceError("A file with that name appeared meanwhile; nothing was written.") from None
    finally:
        os.unlink(temp)
    return FileWriteOutput(path=args.path, bytes_written=len(data), sha256=_sha256(data), backed_up=False)


def _backup(backup_dir: Path, name: str, data: bytes) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    safe_name = re.sub(r"[^\w.-]", "_", name)[:80]
    (backup_dir / f"{stamp}-{safe_name}").write_bytes(data)
    backups = sorted(p for p in backup_dir.iterdir() if p.is_file())
    for old in backups[:-BACKUPS_KEPT]:
        old.unlink()


def _overwrite(root: Path, backup_dir: Path, args: FileOverwriteInput) -> FileWriteOutput:
    target = resolve_in_workspace(root, args.path)  # exists, inside, single hard link
    if not target.is_file():
        raise WorkspaceError("Not a file.")
    old = target.read_bytes()
    if _sha256(old) != args.expected_sha256:
        raise WorkspaceError("File changed since it was read (or was never read); read it again.")
    # No overwrite without an undo: if the backup fails, nothing is replaced.
    _backup(backup_dir, target.name, old)
    data = args.content.encode("utf-8")
    temp = _write_temp(target.parent, data)
    try:
        os.replace(temp, target)
    except BaseException:
        os.unlink(temp)
        raise
    return FileWriteOutput(path=args.path, bytes_written=len(data), sha256=_sha256(data), backed_up=True)


def _preview_create(args: FileCreateInput) -> str:
    lines = args.content.splitlines()
    shown = lines[:PREVIEW_CONTENT_LINES]
    more = len(lines) - len(shown)
    body = "\n".join(f"  | {line}" for line in shown)
    tail = f"\n  … {more} more lines" if more > 0 else ""
    size = len(args.content.encode("utf-8"))
    return f"Create new file {args.path} ({len(lines)} lines, {size} bytes):\n{body}{tail}"


def _preview_overwrite(root: Path, args: FileOverwriteInput) -> str:
    target = resolve_in_workspace(root, args.path)
    old = target.read_bytes()
    note = ""
    if _sha256(old) != args.expected_sha256:
        note = "Note: the file changed since it was read, so this write will be refused.\n"
    diff = list(
        difflib.unified_diff(
            old.decode("utf-8", errors="replace").splitlines(),
            args.content.splitlines(),
            fromfile=f"{args.path} (current)",
            tofile=f"{args.path} (new)",
            lineterm="",
        )
    )
    if not diff:
        return f"{note}Overwrite {args.path}: no changes."
    shown = diff[:PREVIEW_DIFF_LINES]
    more = len(diff) - len(shown)
    tail = f"\n… {more} more diff lines" if more > 0 else ""
    return f"{note}Overwrite {args.path} (old version is backed up):\n" + "\n".join(shown) + tail


def register_file_write_tools(registry: ToolRegistry, workspace: Path, backup_dir: Path) -> None:
    root = workspace.resolve(strict=True)
    registry.register(
        Tool(
            name="file_create",
            description=(
                "Create a NEW text file in the user's workspace (fails if it exists). "
                "The folder must already exist. Script and executable types are refused."
            ),
            input_model=FileCreateInput,
            output_model=FileWriteOutput,
            permission="files.write",
            risk=RiskLevel.CONFIRMATION,
            handler=lambda ctx, args: _create(root, args),
            preview=lambda ctx, args: _preview_create(args),
        )
    )
    registry.register(
        Tool(
            name="file_overwrite",
            description=(
                "Replace the whole content of an EXISTING text file in the user's workspace. "
                "Call file_read first and pass its sha256 as expected_sha256."
            ),
            input_model=FileOverwriteInput,
            output_model=FileWriteOutput,
            permission="files.write",
            risk=RiskLevel.HIGH_RISK,
            handler=lambda ctx, args: _overwrite(root, backup_dir, args),
            preview=lambda ctx, args: _preview_overwrite(root, args),
        )
    )
