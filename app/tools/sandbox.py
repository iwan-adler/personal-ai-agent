"""sandbox_run: run a shell command in a throwaway Linux container (V0.5 slice 6).

The command runs as `sh -c` *inside* the container, so the container is the
security boundary, hardened like this:
- no network (--network none): nothing can be downloaded or sent out;
- read-only root filesystem, a small /tmp in memory, no capabilities,
  no-new-privileges, an unprivileged user, CPU / memory / process limits;
- the only mount is a *copy* of the workspace (without .env, keys, .git or
  links leading outside); it is deleted afterwards, so the sandbox can never
  change your real files;
- the image must be pinned by digest (SANDBOX_IMAGE=name@sha256:...) and is
  never pulled automatically (--pull never);
- on timeout the container itself is killed (docker kill), not only the
  docker CLI process.

HIGH_RISK: it always asks, at every level, and is never grantable. Its output
is written by whatever runs in the container, so it is outside content.
Docker is started through run_command (allowlisted environment, no shell).
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.tools.base import RiskLevel, Tool, ToolRegistry, ToolTimeout
from app.tools.process import CommandResult, run_command
from app.tools.workspace import is_blocked_name

COMMAND_TIMEOUT_SECONDS = 60.0
TOOL_TIMEOUT_SECONDS = 120.0  # copy + container start + command
MAX_COPY_BYTES = 200_000_000
OUTPUT_MAX_CHARS = 16_000
MEMORY = "512m"
CPUS = "1"
PIDS = "128"

Runner = Callable[..., CommandResult]


class SandboxError(RuntimeError):
    """The sandbox could not run the command."""


class SandboxInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1, max_length=2000,
                         description="Shell command, run with sh -c inside the container in /work "
                                     "(a throwaway copy of the workspace, no network).")


class SandboxOutput(BaseModel):
    exit_code: int
    stdout: str
    stderr: str


def copy_workspace(workspace: Path, target: Path, max_bytes: int = MAX_COPY_BYTES) -> int:
    """Copy regular files, skipping off-limits names, links and hard links; returns bytes copied."""
    root = workspace.resolve(strict=True)
    copied = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(is_blocked_name(part) for part in relative.parts):
            continue
        if path.is_symlink() or path.is_junction():
            continue
        if path.is_dir():
            continue
        if not path.is_file() or not path.resolve().is_relative_to(root):
            continue
        info = path.stat()
        if info.st_nlink > 1:  # a hard link can point at a file outside the workspace
            continue
        size = info.st_size
        if copied + size > max_bytes:
            raise SandboxError(f"The workspace is larger than {max_bytes // 1_000_000} MB; the copy was stopped.")
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)  # contents only: no permissions or attributes
        copied += size
    return copied


def docker_args(name: str, image: str, copy_dir: Path, command: str) -> list[str]:
    return [
        "docker", "run", "--rm",
        "--name", name,
        "--pull", "never",
        "--network", "none",
        "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
        "--memory", MEMORY, "--memory-swap", MEMORY,
        "--cpus", CPUS,
        "--pids-limit", PIDS,
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--user", "65534:65534",
        "--mount", f"type=bind,source={copy_dir},target=/work",
        "--workdir", "/work",
        "--env", "HOME=/tmp",
        image,
        "sh", "-c", command,
    ]


def _sandbox_run(workspace: Path, image: str, runner: Runner, args: SandboxInput) -> SandboxOutput:
    name = f"koda-sandbox-{uuid.uuid4().hex[:12]}"
    with tempfile.TemporaryDirectory(prefix="koda-sandbox-") as tmp:
        copy_dir = Path(tmp) / "work"
        copy_dir.mkdir()
        copy_workspace(workspace, copy_dir)
        try:
            result = runner(docker_args(name, image, copy_dir, args.command),
                            timeout_seconds=COMMAND_TIMEOUT_SECONDS, max_output_chars=OUTPUT_MAX_CHARS)
        except ToolTimeout:
            # Killing the docker CLI does not stop the container: kill it by name.
            try:
                runner(["docker", "kill", name], timeout_seconds=15)
            except (ToolTimeout, OSError):
                pass
            raise ToolTimeout(f"The command took longer than {COMMAND_TIMEOUT_SECONDS:g}s; the container was stopped.") from None
        except OSError:
            raise SandboxError("Docker is not installed or not on PATH.") from None
    if result.returncode == 125 or "Cannot connect to the Docker daemon" in result.stderr:
        raise SandboxError(_docker_problem(result.stderr))
    return SandboxOutput(exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr)


def _docker_problem(stderr: str) -> str:
    lowered = stderr.lower()
    if "no such image" in lowered or "pull access denied" in lowered or "not found" in lowered:
        return "The sandbox image is not available locally; pull it once with docker pull."
    if "cannot connect" in lowered or "daemon" in lowered:
        return "Docker is not running; start Docker Desktop."
    return "Docker could not start the container."


def _preview(image: str, args: SandboxInput) -> str:
    return (
        "Run in a throwaway Linux container (no network, read-only system, unprivileged user, "
        f"{CPUS} CPU, {MEMORY} memory, {COMMAND_TIMEOUT_SECONDS:g}s limit) on a COPY of your workspace "
        "(.env, keys and .git left out; your real files are never changed):\n"
        f"  image:   {image}\n"
        f"  command: {args.command}"
    )


def register_sandbox_tools(registry: ToolRegistry, workspace: Path, image: str,
                           *, runner: Runner = run_command) -> None:
    root = workspace.resolve(strict=True)
    registry.register(
        Tool(
            name="sandbox_run",
            description=(
                "Run a shell command in an isolated Linux container on a throwaway copy of the "
                "workspace: no network, no access to the real files. For running code, tests or "
                "tools. Always asks the user first."
            ),
            input_model=SandboxInput,
            output_model=SandboxOutput,
            permission="sandbox.run",
            risk=RiskLevel.HIGH_RISK,
            handler=lambda ctx, args: _sandbox_run(root, image, runner, args),
            preview=lambda ctx, args: _preview(image, args),
            timeout_seconds=TOOL_TIMEOUT_SECONDS,
            max_output_bytes=16_384,
            reads_outside_content=True,
        )
    )
