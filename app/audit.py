"""Append-only audit trail: one JSON line per agent event, kept on disk.

The run trace (AgentRun.steps) disappears when the CLI exits; this file answers
"what did the agent do, and why was it allowed?" afterwards.

Privacy: user messages, raw tool arguments and successful tool output are NOT
written. Arguments appear only as the same short hash the trace uses; denial
and error reasons are kept (truncated) because they explain the decision.

Crashes: a write cut short leaves a partial last line. The next write seals it
with a newline first, so new events are never glued onto it, and read() skips
damaged lines (reporting only their line numbers).
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DETAIL_MAX_CHARS = 200

logger = logging.getLogger("paios.audit")


def truncate(text: str | None, limit: int = DETAIL_MAX_CHARS) -> str | None:
    if text is None or len(text) <= limit:
        return text
    return text[:limit] + "…"


class AuditLog:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Tool threads and the main thread may write; keep lines whole.
        self._lock = threading.Lock()

    def write(self, event: str, **fields: Any) -> None:
        """Raises OSError if the file can't be written; the caller decides what that means."""
        record = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
        line = json.dumps(record, default=str, ensure_ascii=False) + "\n"
        # Binary append mode so the last byte can be checked; writes still go to the end.
        with self._lock, self.path.open("a+b") as handle:
            handle.seek(0, 2)
            if handle.tell() > 0:
                handle.seek(-1, 2)
                if handle.read(1) != b"\n":
                    # A crash cut the previous line short. Seal it, or this event
                    # would be glued onto it and lost as well.
                    line = "\n" + line
            handle.write(line.encode("utf-8"))

    def read(self, *, strict: bool = False) -> list[dict[str, Any]]:
        """All events, oldest first.

        A damaged line (e.g. half-written before a crash) is skipped with a
        warning naming its line number, never its content. strict=True raises
        ValueError instead, for anyone who needs to know the file is intact.
        """
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        damaged: list[int] = []
        with self.path.open(encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    if strict:
                        raise ValueError(f"Audit log line {number} is damaged.") from None
                    damaged.append(number)
        if damaged:
            logger.warning("audit_damaged_lines", extra={"event": {"lines": damaged}})
        return records
