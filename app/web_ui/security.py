"""The five web UI rules from D5, as checks every request goes through.

1. Listen on 127.0.0.1 only (enforced in __main__; never 0.0.0.0).
2. Every request needs the session token (an HttpOnly, SameSite=Strict cookie,
   set once from the link printed in the terminal), and the Host header must
   be this server. Any website open in the browser can send requests to
   localhost, and DNS rebinding can make a foreign name point at 127.0.0.1;
   the Host check refuses both.
   Every POST also needs a same-origin Origin header and the page's CSRF value.
3. An approval counts only with its id plus its one-time value (AgentLoop.resume).
4. Model and tool text is rendered escaped (Jinja2 autoescape), and a strict
   Content-Security-Policy allows only our own static scripts and styles.
5. The audit log, safety layer and grants are unchanged: the UI only answers.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field

COOKIE = "koda_session"


@dataclass(frozen=True)
class Guard:
    port: int
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    csrf: str = field(default_factory=lambda: secrets.token_urlsafe(32))

    @property
    def hosts(self) -> frozenset[str]:
        return frozenset({f"127.0.0.1:{self.port}", f"localhost:{self.port}"})

    @property
    def origins(self) -> frozenset[str]:
        return frozenset(f"http://{host}" for host in self.hosts)

    def host_ok(self, host: str | None) -> bool:
        return host is not None and host.lower() in self.hosts

    def token_ok(self, value: str | None) -> bool:
        return value is not None and secrets.compare_digest(value.encode(), self.token.encode())

    def csrf_ok(self, value: str | None) -> bool:
        return value is not None and secrets.compare_digest(value.encode(), self.csrf.encode())

    def origin_ok(self, origin: str | None, fetch_site: str | None) -> bool:
        """POSTs must come from this page. Browsers always send Origin on POST."""
        if origin is None or origin.lower() not in self.origins:
            return False
        return fetch_site in (None, "same-origin")


SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
        "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}
