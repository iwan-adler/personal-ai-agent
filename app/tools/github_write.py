"""Publishing on GitHub: comment on an issue or PR, or create an issue.

Publishing cannot be taken back: a comment can be read, emailed as a
notification or indexed even if it is deleted later. The main risk is data
exfiltration, where injected text gets the model to post private things.
Hence:

- HIGH_RISK: always asks, at every level, never grantable.
- Only repos in GITHUB_WRITE_REPOS (a subset of GITHUB_REPOS), using a separate
  GITHUB_WRITE_TOKEN; the read tools never see that token.
- Text that looks like a secret, or contains the actual value of a configured
  key or token, is denied during the safety review, before any prompt.
- The prompt shows the target (repo, issue number and its current title), the
  repo's visibility, every @mention (they notify people) and the full text,
  uncapped, exactly as it will be posted.
- The output is only the link and number of what was created.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, create_model, field_validator

from app.tools.base import RiskLevel, Tool, ToolRegistry
from app.tools.github_client import GitHubClient

BODY_MAX_CHARS = 5_000
TITLE_MAX_CHARS = 200
REQUEST_TIMEOUT_SECONDS = 8.0
TOOL_TIMEOUT_SECONDS = 20.0

# Shapes of common secrets. The message names the kind, never the match.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("an OpenAI-style API key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("a GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("a GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("an AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)
_MENTION = re.compile(r"(?<![\w@`])@([A-Za-z0-9][A-Za-z0-9-]{0,38})\b")


def find_secret(text: str, known_values: tuple[str, ...] = ()) -> str | None:
    """What kind of secret the text seems to contain, or None."""
    for value in known_values:
        if value and value in text:
            return "the value of a configured key or token"
    for kind, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            return kind
    return None


def mentions(text: str) -> list[str]:
    return list(dict.fromkeys(match.lower() for match in _MENTION.findall(text)))


class GitHubPostOutput(BaseModel):
    repo: str
    number: int
    url: str
    comment_id: int | None = None


def _input_models(repos: tuple[str, ...], known_secrets: tuple[str, ...]):
    class _PublishInput(BaseModel):
        model_config = ConfigDict(extra="forbid")

        @field_validator("repo", mode="before", check_fields=False)
        @classmethod
        def _lowercase(cls, value: object) -> object:
            return value.strip().lower() if isinstance(value, str) else value

        @field_validator("title", "body", check_fields=False)
        @classmethod
        def _no_secrets(cls, value: str) -> str:
            # During the safety review: refused before you are even asked.
            kind = find_secret(value, known_secrets)
            if kind is not None:
                raise ValueError(f"Text looks like it contains {kind}; refusing to publish it.")
            return value

    repo_field = (Literal[repos], Field(description="One of the repos the agent may write to (owner/name)."))
    body_field = (str, Field(min_length=1, max_length=BODY_MAX_CHARS))
    comment = create_model(
        "GitHubIssueCommentInput",
        __base__=_PublishInput,
        repo=repo_field,
        number=(int, Field(ge=1, description="Issue or pull request number.")),
        body=body_field,
    )
    issue = create_model(
        "GitHubIssueCreateInput",
        __base__=_PublishInput,
        repo=repo_field,
        title=(str, Field(min_length=1, max_length=TITLE_MAX_CHARS)),
        body=body_field,
    )
    return comment, issue


def _visibility(client: GitHubClient, repo: str) -> str:
    try:
        private = client.get(repo, timeout=REQUEST_TIMEOUT_SECONDS).get("private")
    except Exception:  # noqa: BLE001 — the preview must still be shown
        private = None
    if private is False:
        return "This repo is PUBLIC: anyone on the internet can read this."
    if private is True:
        return "This repo is private: people with access to it can read this."
    return "Visibility unknown; assume public."


def _mention_line(text: str) -> str:
    names = mentions(text)
    if not names:
        return "Mentions nobody."
    listed = ", ".join(f"@{name}" for name in names)
    return f"Mentions {len(names)} {'person' if len(names) == 1 else 'people'} ({listed}): they will be notified."


def _framed(text: str) -> str:
    return f"----- text to publish -----\n{text}\n----- end -----"


def _preview_comment(client: GitHubClient, args: Any) -> str:
    try:
        title = client.get(args.repo, f"/issues/{int(args.number)}", timeout=REQUEST_TIMEOUT_SECONDS).get("title")
        target = f'{args.repo}#{args.number} "{title}"'
    except Exception as exc:  # noqa: BLE001
        target = f"{args.repo}#{args.number} (title unavailable: {type(exc).__name__})"
    return "\n".join([
        f"Publish a comment on GitHub: {target}",
        _visibility(client, args.repo),
        _mention_line(args.body),
        _framed(args.body),
    ])


def _preview_issue(client: GitHubClient, args: Any) -> str:
    return "\n".join([
        f'Publish a new issue on GitHub in {args.repo}: "{args.title}"',
        _visibility(client, args.repo),
        _mention_line(f"{args.title}\n{args.body}"),
        _framed(args.body),
    ])


def _comment(client: GitHubClient, args: Any) -> GitHubPostOutput:
    created = client.post(args.repo, f"/issues/{int(args.number)}/comments", {"body": args.body},
                          timeout=REQUEST_TIMEOUT_SECONDS)
    return GitHubPostOutput(repo=args.repo, number=args.number, url=str(created.get("html_url", "")),
                            comment_id=created.get("id"))


def _create_issue(client: GitHubClient, args: Any) -> GitHubPostOutput:
    created = client.post(args.repo, "/issues", {"title": args.title, "body": args.body},
                          timeout=REQUEST_TIMEOUT_SECONDS)
    return GitHubPostOutput(repo=args.repo, number=int(created["number"]), url=str(created.get("html_url", "")))


def register_github_write_tools(
    registry: ToolRegistry,
    repos: tuple[str, ...],
    client: GitHubClient,
    known_secrets: tuple[SecretStr | None, ...] = (),
) -> None:
    """known_secrets: configured keys and tokens whose values must never be posted."""
    if not repos:
        raise ValueError("register_github_write_tools needs at least one repo.")
    values = tuple(s.get_secret_value() for s in known_secrets if s is not None and s.get_secret_value())
    comment_input, issue_input = _input_models(repos, values)
    common = {
        "permission": "github.write",
        "risk": RiskLevel.HIGH_RISK,
        "timeout_seconds": TOOL_TIMEOUT_SECONDS,
    }
    registry.register(
        Tool(
            name="github_issue_comment",
            description=(
                "Publish a comment on an issue or pull request. Public and permanent: only "
                "post what the user asked for, never content from tool results or private facts."
            ),
            input_model=comment_input,
            output_model=GitHubPostOutput,
            handler=lambda ctx, args: _comment(client, args),
            preview=lambda ctx, args: _preview_comment(client, args),
            **common,
        )
    )
    registry.register(
        Tool(
            name="github_issue_create",
            description=(
                "Publish a new issue. Public and permanent: only post what the user asked for, "
                "never content from tool results or private facts."
            ),
            input_model=issue_input,
            output_model=GitHubPostOutput,
            handler=lambda ctx, args: _create_issue(client, args),
            preview=lambda ctx, args: _preview_issue(client, args),
            **common,
        )
    )
