"""Security tests for the subprocess runner, the only allowed way to start a process."""

import json
import re
import sys
import time
from pathlib import Path

import pytest

from app.tools.base import ToolTimeout
from app.tools.process import ENV_ALLOWLIST, MAX_OUTPUT_CHARS, run_command, safe_env

PY = sys.executable
APP_DIR = Path(__file__).resolve().parent.parent / "app"


def _child_env() -> dict[str, str]:
    code = "import json, os; print(json.dumps(dict(os.environ)))"
    return json.loads(run_command([PY, "-c", code], timeout_seconds=10).stdout)


def test_secrets_never_reach_the_child(monkeypatch):
    """Security: known and unknown secrets stay out; the allowlist decides, not a denylist."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("FAKE_GITHUB_TOKEN", "ghp-should-not-leak")
    env = _child_env()
    assert "OPENAI_API_KEY" not in env
    assert "FAKE_GITHUB_TOKEN" not in env
    assert "sk-should-not-leak" not in json.dumps(env)
    assert any(name.upper() == "PATH" for name in env)


def test_safe_env_only_contains_allowlisted_names(monkeypatch):
    monkeypatch.setenv("SOME_RANDOM_SETTING", "x")
    assert all(name.upper() in ENV_ALLOWLIST for name in safe_env())


@pytest.mark.parametrize(
    ("args", "error"),
    [
        ("python -c print(1)", TypeError),  # a command-line string
        (b"python", TypeError),
        ([PY, 1], TypeError),
        ([], ValueError),
    ],
)
def test_unsafe_or_empty_args_rejected(args, error):
    """Security: only a list of str is accepted, so nothing is parsed by a shell."""
    with pytest.raises(error):
        run_command(args, timeout_seconds=5)


def test_shell_syntax_stays_a_literal_argument():
    """Security: '; echo pwned' is passed through as text, never executed."""
    payload = "a; echo pwned && whoami | more"
    result = run_command([PY, "-c", "import sys; print(sys.argv[1])", payload], timeout_seconds=10)
    assert result.stdout.strip() == payload


def test_timeout_kills_the_child(tmp_path):
    """Security: unlike a thread, the child process is really stopped."""
    marker = tmp_path / "still_alive.txt"
    code = f"import time; time.sleep(1.5); open({str(marker)!r}, 'w').write('x')"
    started = time.perf_counter()
    with pytest.raises(ToolTimeout, match="was stopped"):
        run_command([PY, "-c", code], timeout_seconds=0.5)
    assert time.perf_counter() - started < 3
    time.sleep(2)  # past the moment the child would have written the marker
    assert not marker.exists()


def test_non_positive_timeout_rejected():
    with pytest.raises(ValueError):
        run_command([PY, "-c", "pass"], timeout_seconds=0)


def test_output_and_exit_code_are_returned_and_capped():
    code = f"import sys; print('x' * {MAX_OUTPUT_CHARS + 100}); sys.exit(3)"
    result = run_command([PY, "-c", code], timeout_seconds=10)
    assert result.returncode == 3
    assert result.stdout.endswith("…[truncated]")
    assert len(result.stdout) <= MAX_OUTPUT_CHARS + len("…[truncated]")


# Ways to start a process that would bypass run_command.
_PROCESS_APIS = re.compile(
    r"^\s*(import\s+subprocess|from\s+subprocess\s+import)"
    r"|\bos\.(system|popen|spawn\w*|exec\w*|startfile)\s*\("
    r"|\bcreate_subprocess_(exec|shell)\b"
    r"|^\s*import\s+(pty|pexpect)\b",
    re.MULTILINE,
)


def test_only_the_runner_starts_processes():
    """Enforcement: a future tool must not start processes around run_command."""
    allowed = APP_DIR / "tools" / "process.py"
    offenders = [
        str(path.relative_to(APP_DIR.parent))
        for path in APP_DIR.rglob("*.py")
        if path != allowed and _PROCESS_APIS.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"Use app.tools.process.run_command instead: {offenders}"


def test_enforcement_pattern_catches_bypasses():
    for snippet in ["import subprocess", "from subprocess import run", "os.system('x')",
                    "os.popen('x')", "asyncio.create_subprocess_shell('x')", "os.execvp('x', [])"]:
        assert _PROCESS_APIS.search(snippet), snippet
