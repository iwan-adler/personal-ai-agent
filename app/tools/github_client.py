"""A minimal read-only GitHub REST client on the standard library.

Two rules keep the token where it belongs:
- The host is fixed to https://api.github.com and paths are built only from
  validated parts, so the model cannot point a request anywhere else.
- Redirects are refused, and the token is an "unredirected" header. urllib
  would otherwise follow a redirect and forward Authorization to the new host.
  A renamed repository therefore fails with a clear message instead.

Error messages carry the HTTP status and GitHub's own message (truncated), with
the token redacted; never response headers or the request.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import SecretStr

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
MAX_RESPONSE_BYTES = 1_000_000
MESSAGE_MAX_CHARS = 200

# (request, timeout seconds) -> (status, headers, body). Replaced in tests.
Transport = Callable[[urllib.request.Request, float], tuple[int, Mapping[str, str], bytes]]


class GitHubError(RuntimeError):
    """GitHub could not be reached or answered with an error."""


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise GitHubError(
            f"GitHub answered with a redirect ({code}). If the repository was renamed, "
            "update GITHUB_REPOS."
        )


def urllib_transport(request: urllib.request.Request, timeout: float) -> tuple[int, Mapping[str, str], bytes]:
    opener = urllib.request.build_opener(_RefuseRedirects)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.URLError as exc:
        raise GitHubError(f"Could not reach GitHub: {exc.reason}") from None
    except TimeoutError:
        raise GitHubError("GitHub did not answer in time.") from None


class GitHubClient:
    def __init__(self, token: SecretStr | None, transport: Transport = urllib_transport) -> None:
        self._token = token
        self._transport = transport

    def get(self, repo: str, path: str = "", params: Mapping[str, Any] | None = None,
            *, timeout: float) -> Any:
        """GET /repos/<repo><path>. repo must already be validated "owner/name"."""
        owner, name = repo.split("/")
        url = (
            f"{API_ROOT}/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}"
            f"{path}"
        )
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "koda-personal-agent",
            },
        )
        if self._token is not None:
            request.add_unredirected_header("Authorization", f"Bearer {self._token.get_secret_value()}")
        try:
            status, headers, body = self._transport(request, timeout)
        except GitHubError as exc:
            raise GitHubError(self._redact(str(exc))) from None
        if len(body) > MAX_RESPONSE_BYTES:
            raise GitHubError("GitHub's answer is larger than 1 MB.")
        if status >= 400:
            raise GitHubError(self._redact(self._error_message(status, headers, body)))
        try:
            return json.loads(body)
        except ValueError:
            raise GitHubError("GitHub's answer was not valid JSON.") from None

    def _error_message(self, status: int, headers: Mapping[str, str], body: bytes) -> str:
        remaining = {k.lower(): v for k, v in headers.items()}.get("x-ratelimit-remaining")
        if status in (403, 429) and remaining == "0":
            hint = "" if self._token is not None else " Setting GITHUB_TOKEN raises the limit."
            return f"GitHub rate limit reached; try again later.{hint}"
        try:
            message = str(json.loads(body).get("message", ""))
        except (ValueError, AttributeError):
            message = ""
        if len(message) > MESSAGE_MAX_CHARS:
            message = message[:MESSAGE_MAX_CHARS] + "…"
        return f"GitHub returned {status}" + (f": {message}" if message else ".")

    def _redact(self, text: str) -> str:
        if self._token is None:
            return text
        secret = self._token.get_secret_value()
        return text.replace(secret, "***") if secret else text
