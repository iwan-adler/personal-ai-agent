"""Append-only audit trail: one JSON line per agent event, kept on disk.

The run trace (AgentRun.steps) disappears when the CLI exits; this file answers
"what did the agent do, and why was it allowed?" afterwards.

Privacy: user messages, raw tool arguments and successful tool output are NOT
written. Arguments appear only as the same short hash the trace uses; denial
and error reasons are kept (truncated) because they explain the decision.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DETAIL_MAX_CHARS = 200


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
        line = json.dumps(record, default=str, ensure_ascii=False)
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
