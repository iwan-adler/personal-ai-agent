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


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…[truncated]"


def run_command(
    args: Sequence[str],
    *,
    timeout_seconds: float,
    cwd: str | Path | None = None,
    max_output_chars: int = MAX_OUTPUT_CHARS,
) -> CommandResult:
    """Run a program without a shell, with a scrubbed environment and a hard timeout.

    max_output_chars caps stdout and stderr each; raise it only for a trusted
    helper whose own output is bounded (e.g. the PDF extractor).

    Raises TypeError/ValueError for unsafe or empty arguments and ToolTimeout
    (after the child was killed) when the timeout expires.
    """
    if max_output_chars < 1:
        raise ValueError("max_output_chars must be >= 1.")
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
        stdout=_truncate(completed.stdout, max_output_chars),
        stderr=_truncate(completed.stderr, max_output_chars),
    )


def start_detached(program: Path, args: Sequence[str]) -> subprocess.Popen:
    """Start a fixed, trusted program without waiting for it (the app window).

    Same rules as run_command (list arguments, no shell, allowlisted
    environment), but no timeout: the caller owns the returned process. Only
    for programs Koda chose itself by absolute path, never for model input.
    """
    if not program.is_absolute() or not program.is_file():
        raise ValueError("program must be an existing file given by absolute path.")
    if isinstance(args, (str, bytes)) or not all(isinstance(arg, str) for arg in args):
        raise TypeError("args must be a list of strings.")
    return subprocess.Popen(
        [str(program), *args],
        shell=False,
        env=safe_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
