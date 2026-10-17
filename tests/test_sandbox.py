"""sandbox_run: a hardened throwaway container on a copy of the workspace.

Most tests use a fake runner that records the docker command. The tests at the
end run a real container; they are skipped unless Docker works and
KODA_SANDBOX_TEST_IMAGE names a digest-pinned image that is already pulled.
"""

import json
import os
import shutil
import subprocess
import sys

import pytest
from pydantic import ValidationError

import app.tools.sandbox as sandbox
from app.agent.loop import AgentLoop, RunStatus
from app.audit import AuditLog
from app.config import AutonomyLevel, Settings
from app.llm.client import FakeLLMClient
from app.main import build_registry
from app.safety.permissions import DEFAULT_GRANTS, SafetyLayer
from app.tools.base import RiskLevel, ToolTimeout
from app.tools.builtin import build_default_registry
from app.tools.process import CommandResult
from app.tools.sandbox import SandboxError, SandboxInput, copy_workspace, register_sandbox_tools

IMAGE = "python:3.13-slim@sha256:" + "a" * 64


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (root / ".env").write_text("OPENAI_API_KEY=sk-secret\n", encoding="utf-8")
    (root / "deploy.pem").write_text("key", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("x", encoding="utf-8")
    return root


class FakeRunner:
    def __init__(self, result=CommandResult(0, "ok\n", ""), timeout=False):
        self.result, self.timeout = result, timeout
        self.calls = []
        self.copied: list[str] = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if args[:2] == ["docker", "run"]:
            source = next(a for a in args if a.startswith("type=bind,source=")).split("source=")[1].split(",")[0]
            self.copied = sorted(str(p.relative_to(source)).replace("\\", "/")
                                 for p in __import__("pathlib").Path(source).rglob("*") if p.is_file())
            if self.timeout:
                raise ToolTimeout("timed out")
        return self.result


# --- The docker command --------------------------------------------------------------------------


def test_the_container_is_locked_down(ws):
    runner = FakeRunner()
    out = sandbox._sandbox_run(ws.resolve(), IMAGE, runner, SandboxInput(command="python src/main.py"))
    assert out.exit_code == 0 and out.stdout == "ok\n"
    args, kwargs = runner.calls[0]
    joined = " ".join(args)
    for flag in ("--rm", "--pull never", "--network none", "--read-only", "--cap-drop ALL",
                 "--security-opt no-new-privileges", "--user 65534:65534", "--memory 512m",
                 "--memory-swap 512m", "--cpus 1", "--pids-limit 128", "--workdir /work"):
        assert flag in joined, flag
    assert args[-4:] == [IMAGE, "sh", "-c", "python src/main.py"]
    mounts = [a for a in args if a.startswith("type=bind")]
    assert len(mounts) == 1 and str(ws) not in mounts[0]  # only the throwaway copy is mounted
    assert "--privileged" not in args and "-v" not in args and "--volume" not in args
    assert kwargs["timeout_seconds"] == sandbox.COMMAND_TIMEOUT_SECONDS


def test_secrets_and_git_are_not_copied(ws):
    """Security: the container never sees .env, keys or .git."""
    runner = FakeRunner()
    sandbox._sandbox_run(ws.resolve(), IMAGE, runner, SandboxInput(command="ls -R"))
    assert runner.copied == ["src/main.py"]


def test_the_copy_is_deleted_and_real_files_are_untouched(ws):
    runner = FakeRunner()
    sandbox._sandbox_run(ws.resolve(), IMAGE, runner, SandboxInput(command="rm -rf /work/*"))
    mount = next(a for a in runner.calls[0][0] if a.startswith("type=bind")).split("source=")[1].split(",")[0]
    assert not os.path.exists(mount)
    assert (ws / "src" / "main.py").read_text(encoding="utf-8") == "print('hi')\n"


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows feature")
def test_links_leading_outside_are_not_copied(tmp_path, ws):
    import _winapi

    secret_dir = tmp_path / "private"
    secret_dir.mkdir()
    (secret_dir / "diary.txt").write_text("private", encoding="utf-8")
    _winapi.CreateJunction(str(secret_dir), str(ws / "escape"))
    target = tmp_path / "copy"
    target.mkdir()
    copy_workspace(ws.resolve(), target)
    assert not (target / "escape").exists()


def test_hard_links_are_not_copied(tmp_path, ws):
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    os.link(outside, ws / "src" / "linked.txt")
    target = tmp_path / "copy"
    target.mkdir()
    copy_workspace(ws.resolve(), target)
    assert sorted(p.name for p in target.rglob("*") if p.is_file()) == ["main.py"]


def test_a_huge_workspace_is_refused(ws, tmp_path):
    target = tmp_path / "copy"
    target.mkdir()
    with pytest.raises(SandboxError, match="larger than"):
        copy_workspace(ws.resolve(), target, max_bytes=5)


def test_timeout_kills_the_container_itself(ws):
    """Security: stopping the docker CLI would leave the container running."""
    runner = FakeRunner(timeout=True)
    with pytest.raises(ToolTimeout, match="container was stopped"):
        sandbox._sandbox_run(ws.resolve(), IMAGE, runner, SandboxInput(command="sleep 1000"))
    name = runner.calls[0][0][runner.calls[0][0].index("--name") + 1]
    assert runner.calls[1][0] == ["docker", "kill", name]


@pytest.mark.parametrize(
    ("stderr", "message"),
    [("Unable to find image 'x' locally\nError: No such image", "pull it once"),
     ("Cannot connect to the Docker daemon at npipe:////./pipe/docker_engine", "not running")],
)
def test_docker_problems_are_explained(ws, stderr, message):
    runner = FakeRunner(result=CommandResult(125, "", stderr))
    with pytest.raises(SandboxError, match=message):
        sandbox._sandbox_run(ws.resolve(), IMAGE, runner, SandboxInput(command="true"))


def test_missing_docker(ws):
    def no_docker(args, **kwargs):
        raise FileNotFoundError("docker")

    with pytest.raises(SandboxError, match="not installed"):
        sandbox._sandbox_run(ws.resolve(), IMAGE, no_docker, SandboxInput(command="true"))


# --- Settings and the safety layer ------------------------------------------------------------------


@pytest.mark.parametrize("image", ["python:3.13-slim", "python@sha256:abc", "python:latest@sha256:" + "Z" * 64,
                                   "--privileged@sha256:" + "a" * 64])
def test_the_image_must_be_pinned_by_digest(image):
    with pytest.raises(ValidationError):
        Settings(openai_api_key="k", openai_model="m", sandbox_image=image)


def test_the_sandbox_exists_only_with_a_workspace_and_an_image(ws):
    base = Settings(openai_api_key="k", openai_model="m")
    assert build_registry(base.model_copy(update={"workspace_dir": ws})).get("sandbox_run") is None
    tool = build_registry(base.model_copy(update={"workspace_dir": ws.resolve(), "sandbox_image": IMAGE})).get("sandbox_run")
    assert tool.risk == RiskLevel.HIGH_RISK and tool.reads_outside_content and tool.permission == "sandbox.run"
    assert "sandbox.run" in DEFAULT_GRANTS


@pytest.mark.parametrize("level", [AutonomyLevel.ASK_PERMISSION, AutonomyLevel.AUTO_SAFE, AutonomyLevel.AUTO_BOUNDED])
def test_every_command_asks_and_shows_what_will_run(tmp_path, ws, level):
    """Security: HIGH_RISK, never grantable; the prompt shows the image and command."""
    runner = FakeRunner()
    registry = build_default_registry()
    register_sandbox_tools(registry, ws, IMAGE, runner=runner)
    prompts = []
    call = json.dumps({"action": "tool_call", "tool": "sandbox_run", "args": {"command": "pytest -q"}, "summary": "Run."})
    agent = AgentLoop(
        settings=Settings(openai_api_key="k", openai_model="m", autonomy_level=level),
        llm=FakeLLMClient(replies=[call, json.dumps({"action": "finish", "text": "ok", "summary": "ok"})]),
        registry=registry,
        safety=SafetyLayer(registry, allowed_permissions=DEFAULT_GRANTS, autonomy=level),
        confirm=lambda p: prompts.append(p) or False,
        audit=AuditLog(tmp_path / "audit.jsonl"),
    )
    assert agent.run("run the tests").status == RunStatus.BLOCKED
    assert runner.calls == []  # declined: docker never ran
    assert f"image:   {IMAGE}" in prompts[0] and "command: pytest -q" in prompts[0]
    assert "no network" in prompts[0] and "COPY of your workspace" in prompts[0]
    assert "without asking for the rest of this request" not in prompts[0]


# --- Real Docker (skipped until Docker and a pulled, pinned image are available) ---------------------


def _docker_ready() -> str | None:
    image = os.environ.get("KODA_SANDBOX_TEST_IMAGE")
    if not image or shutil.which("docker") is None:
        return None
    try:
        subprocess.run(["docker", "image", "inspect", image], capture_output=True, check=True, timeout=20)
    except (subprocess.SubprocessError, OSError):
        return None
    return image


real_docker = pytest.mark.skipif(_docker_ready() is None,
                                 reason="needs Docker and KODA_SANDBOX_TEST_IMAGE (a pulled, digest-pinned image)")


@real_docker
def test_real_container_has_no_network_and_a_read_only_system(ws):
    image = _docker_ready()
    out = sandbox._sandbox_run(ws.resolve(), image, sandbox.run_command, SandboxInput(
        command="cat src/main.py; id -u; touch /etc/x 2>&1 || echo ro-ok; "
                "(ls -A /work; cat /work/.env 2>&1) ; "
                "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 53), 3)\" 2>&1 || echo net-blocked"))
    assert "print('hi')" in out.stdout and "65534" in out.stdout
    assert "ro-ok" in out.stdout and "net-blocked" in out.stdout
    assert ".env" not in out.stdout.split("ro-ok", 1)[1].split("net-blocked")[0].replace("cat: /work/.env", "")


@real_docker
def test_real_container_cannot_change_the_workspace(ws):
    image = _docker_ready()
    sandbox._sandbox_run(ws.resolve(), image, sandbox.run_command, SandboxInput(command="echo changed > src/main.py"))
    assert (ws / "src" / "main.py").read_text(encoding="utf-8") == "print('hi')\n"
