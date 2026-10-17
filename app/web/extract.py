"""Turning fetched pages into plain text, and parsing feeds safely.

HTML: Python's html.parser, which never runs scripts or loads anything; script,
style and similar elements are dropped, block elements become line breaks.

XML (feeds, slice 3): the standard parser on this machine (expat 2.8) already
refuses entity-expansion attacks ("billion laughs") and does not resolve
external entities. As a second line of defence, any document with a DOCTYPE
or ENTITY declaration is refused before parsing (RSS and Atom never need one),
and so is anything that is not ASCII-compatible (UTF-16 could hide those
declarations from the byte check).
"""

from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser

from app.web.fetcher import WebError, check_url

SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "iframe", "object", "head"}
BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "header",
              "footer", "main", "nav", "aside", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6"}
MAX_LINKS = 50


@dataclass
class ExtractedPage:
    title: str
    text: str
    links: list[str] = field(default_factory=list)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.hrefs: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        elif tag in SKIP_TAGS:
            self._skip_depth += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)
        elif not self._skip_depth:
            self.parts.append(data)


def html_to_text(html: str, base_url: str, allowlist: tuple[str, ...]) -> ExtractedPage:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in "".join(parser.parts).split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    links: list[str] = []
    for href in parser.hrefs:
        absolute, _ = urllib.parse.urldefrag(urllib.parse.urljoin(base_url, href))
        try:
            check_url(absolute, allowlist)  # only links the agent could actually follow
        except WebError:
            continue
        if absolute not in links:
            links.append(absolute)
        if len(links) == MAX_LINKS:
            break
    return ExtractedPage(title=" ".join("".join(parser.title_parts).split()), text=text, links=links)


def parse_xml_safely(data: bytes) -> ET.Element:
    """Parse untrusted XML (feeds). Raises WebError for anything suspicious."""
    if b"\x00" in data[:4096]:
        raise WebError("Only UTF-8 or ASCII feeds are supported.")
    lowered = data.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise WebError("Feeds with DOCTYPE or ENTITY declarations are refused.")
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        raise WebError("The feed is not valid XML.") from None
