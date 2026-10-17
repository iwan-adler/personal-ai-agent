"""Fetching web pages under D6: allowlisted HTTPS hosts only, never the local network.

Every hop (the first request and each redirect) goes through the same checks:
1. URL: https, port 443, no user:password, a host name (not an IP address)
   that is on the allowlist or a subdomain of an entry, compared in its
   normalised form (lowercase, IDNA/punycode), so look-alikes do not match.
2. DNS: every address the name resolves to must be public. Loopback, private,
   link-local (including the cloud metadata address), shared, reserved and
   multicast ranges are refused, also as IPv4-mapped IPv6.
3. Connection: made to exactly the address that was checked, while TLS still
   verifies the certificate against the host name. This closes DNS
   rebinding, where a name points somewhere harmless during the check and to
   127.0.0.1 a moment later. System proxies are therefore not used.

No cookies are kept or sent, compression is not accepted (no zip bombs), and
bodies are capped. Error messages never include page content.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass

MAX_BYTES = 2_000_000
MAX_REDIRECTS = 5
SOCKET_TIMEOUT_SECONDS = 10.0
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
PAGE_TYPES = frozenset({"text/html", "text/plain", "application/xhtml+xml"})
FEED_TYPES = frozenset({"application/rss+xml", "application/atom+xml", "application/xml", "text/xml"})
USER_AGENT = "koda-personal-agent"

# host -> resolved addresses. Replaced in tests.
Resolver = Callable[[str], list[str]]
# (host, pinned ip, path with query, timeout) -> (status, headers, body). Replaced in tests.
Transport = Callable[[str, str, str, float], tuple[int, Mapping[str, str], bytes]]


class WebError(RuntimeError):
    """A URL was refused, or the page could not be fetched."""


@dataclass(frozen=True)
class Page:
    url: str
    content_type: str
    charset: str
    body: bytes

    def text(self) -> str:
        try:
            return self.body.decode(self.charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


def normalize_host(host: str) -> str:
    """Lowercase, no trailing dot, international names as punycode."""
    host = host.strip().rstrip(".").lower()
    if not host:
        raise WebError("The URL has no host name.")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        raise WebError("The host name is not valid.") from None


def is_allowed_host(host: str, allowlist: tuple[str, ...]) -> bool:
    return any(host == entry or host.endswith("." + entry) for entry in allowlist)


def check_url(url: str, allowlist: tuple[str, ...]) -> urllib.parse.SplitResult:
    """Static checks; returns the URL split, with the normalised host."""
    try:
        parts = urllib.parse.urlsplit(url.strip())
        port = parts.port
    except ValueError:
        raise WebError("That is not a valid URL.") from None
    if parts.scheme.lower() != "https":
        raise WebError("Only https:// URLs can be fetched.")
    if parts.username is not None or parts.password is not None:
        raise WebError("URLs with a user name or password are not allowed.")
    if port not in (None, 443):
        raise WebError("Only the standard HTTPS port (443) is allowed.")
    raw_host = parts.hostname or ""
    try:
        ipaddress.ip_address(raw_host.strip("[]"))
        raise WebError("IP addresses are not allowed; use a host name on the allowlist.")
    except ValueError:
        pass
    host = normalize_host(raw_host)
    if not is_allowed_host(host, allowlist):
        raise WebError("That host is not on WEB_ALLOWLIST.")
    return parts._replace(netloc=host, scheme="https")


def is_public_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def system_resolver(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise WebError("The host name could not be resolved.") from None
    return list(dict.fromkeys(info[4][0] for info in infos))


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to a fixed, already-checked IP; TLS still verifies the host name."""

    def __init__(self, host: str, ip: str, timeout: float, context: ssl.SSLContext | None = None) -> None:
        super().__init__(host, 443, timeout=timeout, context=context or ssl.create_default_context())
        self._pinned_ip = ip

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_ip, 443), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def pinned_transport(host: str, ip: str, path: str, timeout: float) -> tuple[int, Mapping[str, str], bytes]:
    connection = PinnedHTTPSConnection(host, ip, timeout)
    try:
        connection.request("GET", path, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html, application/xhtml+xml, text/plain;q=0.9, "
                      "application/rss+xml, application/atom+xml, application/xml;q=0.8",
            "Accept-Encoding": "identity",
        })
        response = connection.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read(MAX_BYTES + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise WebError(f"Could not fetch the page ({type(exc).__name__}).") from None
    finally:
        connection.close()


def fetch(
    url: str,
    allowlist: tuple[str, ...],
    *,
    allowed_types: frozenset[str] = PAGE_TYPES,
    resolver: Resolver = system_resolver,
    transport: Transport = pinned_transport,
    max_bytes: int = MAX_BYTES,
) -> Page:
    for _hop in range(MAX_REDIRECTS + 1):
        parts = check_url(url, allowlist)
        addresses = resolver(parts.hostname or "")
        if not addresses:
            raise WebError("The host name could not be resolved.")
        if not all(is_public_address(address) for address in addresses):
            raise WebError("The host resolves to a private or local address; refused.")
        path = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
        status, headers, body = transport(parts.hostname or "", addresses[0], path, SOCKET_TIMEOUT_SECONDS)
        headers = {k.lower(): v for k, v in headers.items()}
        if status in REDIRECT_STATUSES:
            location = headers.get("location")
            if not location:
                raise WebError("The server sent a redirect without a target.")
            url = urllib.parse.urljoin(urllib.parse.urlunsplit(parts), location)
            continue
        if status >= 400:
            raise WebError(f"The server returned {status}.")
        if len(body) > max_bytes:
            raise WebError(f"The page is larger than {max_bytes // 1_000_000} MB.")
        media_type, _, params = headers.get("content-type", "").partition(";")
        media_type = media_type.strip().lower()
        if media_type not in allowed_types:
            raise WebError(f"Not a supported text type ({media_type or 'unknown'}).")
        charset = "utf-8"
        for param in params.split(";"):
            key, _, value = param.strip().partition("=")
            if key.lower() == "charset" and value:
                charset = value.strip("\"' ").lower()
        return Page(url=urllib.parse.urlunsplit(parts), content_type=media_type, charset=charset, body=body)
    raise WebError(f"More than {MAX_REDIRECTS} redirects; refused.")
