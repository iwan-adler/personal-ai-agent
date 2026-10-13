"""The only way tools may start a process (enforced by tests/test_process.py).

Three rules, each closing a hole a subprocess tool would otherwise open:
- Allowlisted environment: the child gets only the variables below, so secrets
  in our environment (API keys, tokens) never reach it, including ones we do
  not know the name of. A denylist could only stop names we thought of.
- Arguments as a list, never shell=True: model text stays one literal argument
  and cannot become shell syntax such as ";", "&&" or "|".
- A real timeout: subprocess.run kills the child when it expires, unlike the
  thread in run_with_timeout, which can only stop waiting. Limitation: on
  Windows only the direct child is killed, not processes it started itself.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.tools.base import ToolTimeout

# What common programs (python, git) need to start on Windows and POSIX.
# HOME/USERPROFILE reveal the user name, but git needs them for its config.
ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "LANG",
        "LC_ALL",
        "TZ",
    }
)
MAX_OUTPUT_CHARS = 8_192


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def safe_env() -> dict[str, str]:
    """A copy of os.environ with only the allowlisted variables."""
    return {name: value for name, value in os.environ.items() if name.upper() in ENV_ALLOWLIST}


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + "…[truncated]"


def run_command(
    args: Sequence[str],
    *,
    timeout_seconds: float,
    cwd: str | Path | None = None,
) -> CommandResult:
    """Run a program without a shell, with a scrubbed environment and a hard timeout.

    Raises TypeError/ValueError for unsafe or empty arguments and ToolTimeout
    (after the child was killed) when the timeout expires.
    """
    # A str is a Sequence too; passing one would mean "parse this as a command line".
    if isinstance(args, (str, bytes)) or not isinstance(args, Sequence):
        raise TypeError("args must be a list of strings, not a command-line string.")
    if not args:
        raise ValueError("args must name a program to run.")
    if not all(isinstance(arg, str) for arg in args):
        raise TypeError("every argument must be a str.")
    if not timeout_seconds > 0:
        raise ValueError("timeout_seconds must be > 0.")

    try:
        completed = subprocess.run(
            list(args),
            shell=False,
            env=safe_env(),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolTimeout(f"Command timed out after {timeout_seconds:g}s and was stopped.") from exc
    return CommandResult(
        returncode=completed.returncode,
        stdout=_truncate(completed.stdout),
        stderr=_truncate(completed.stderr),
    )
