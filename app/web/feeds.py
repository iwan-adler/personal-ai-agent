"""RSS 2.0 and Atom feeds -> plain entries (title, link, summary, published).

The XML comes through parse_xml_safely (no DOCTYPE/ENTITY, ASCII-compatible
only), the bytes through fetch() (allowlist, public IPs, pinned connection).
Summaries are HTML in many feeds, so they go through html_to_text. Only http(s)
links are kept; javascript: and similar schemes are dropped.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass

from app.web.extract import html_to_text, parse_xml_safely
from app.web.fetcher import WebError

MAX_ENTRIES = 200
TITLE_MAX_CHARS = 300
SUMMARY_MAX_CHARS = 1_000
ATOM = "{http://www.w3.org/2005/Atom}"


@dataclass(frozen=True)
class FeedEntry:
    title: str
    link: str
    summary: str
    published: str | None


def _clean(text: str | None, limit: int) -> str:
    plain = html_to_text(text or "", "https://invalid.invalid/", ()).text if text else ""
    plain = " ".join(plain.split())
    return plain if len(plain) <= limit else plain[:limit] + "…"


def _safe_link(link: str | None) -> str | None:
    if not link:
        return None
    link = link.strip()
    if urllib.parse.urlsplit(link).scheme.lower() not in ("http", "https"):
        return None
    return link


def parse_feed(data: bytes) -> list[FeedEntry]:
    root = parse_xml_safely(data)
    entries: list[FeedEntry] = []
    if root.tag == "rss" or root.find("channel") is not None:
        for item in root.iter("item"):
            link = _safe_link(item.findtext("link"))
            if link:
                entries.append(FeedEntry(
                    title=_clean(item.findtext("title"), TITLE_MAX_CHARS) or "(untitled)",
                    link=link,
                    summary=_clean(item.findtext("description"), SUMMARY_MAX_CHARS),
                    published=(item.findtext("pubDate") or "").strip() or None,
                ))
            if len(entries) == MAX_ENTRIES:
                break
    elif root.tag == f"{ATOM}feed":
        for entry in root.iter(f"{ATOM}entry"):
            link = None
            for candidate in entry.findall(f"{ATOM}link"):
                if candidate.get("rel", "alternate") == "alternate":
                    link = _safe_link(candidate.get("href"))
                    break
            if link:
                summary = entry.findtext(f"{ATOM}summary") or entry.findtext(f"{ATOM}content")
                entries.append(FeedEntry(
                    title=_clean(entry.findtext(f"{ATOM}title"), TITLE_MAX_CHARS) or "(untitled)",
                    link=link,
                    summary=_clean(summary, SUMMARY_MAX_CHARS),
                    published=(entry.findtext(f"{ATOM}updated") or entry.findtext(f"{ATOM}published") or "").strip() or None,
                ))
            if len(entries) == MAX_ENTRIES:
                break
    else:
        raise WebError("Not an RSS or Atom feed.")
    return entries
