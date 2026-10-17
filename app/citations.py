"""The citations rule (V0.7): a cited source must have been read in this request.

Language models invent plausible sources. Tools that read citable material
(doc_search, web_fetch) report what they read; after the final reply, every
citation in square brackets is compared with that list, and citations that
were not read are flagged for the user.

A bracketed part counts as a citation when it looks like one: a URL, a page
("p. 12") or line ("line 40") reference, or a .pdf/.md/.txt file name, so
ordinary brackets like "[optional]" are ignored. Several citations in one
bracket can be separated by ";". Markdown links ("[text](url)") are not
checked. This verifies that a source was *read*, not that it *says* what the
answer claims.
"""

from __future__ import annotations

import re
import urllib.parse

_BRACKET = re.compile(r"\[([^\[\]\n]{3,300})\](?!\()")
_LOOKS_LIKE_CITATION = re.compile(r"https?://|\bp\.\s*\d+|\bline\s+\d+|\.(?:pdf|md|txt)\b", re.IGNORECASE)


def normalize(citation: str) -> str:
    text = " ".join(citation.split()).strip()
    if re.match(r"https?://", text, re.IGNORECASE):
        parts = urllib.parse.urlsplit(text)
        path = parts.path.rstrip("/") or ""
        return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))
    text = text.lower()
    text = re.sub(r"\bp\.\s*", "p. ", text)
    return re.sub(r"\bline\s+", "line ", text)


def citations_in(text: str) -> list[str]:
    found: list[str] = []
    for match in _BRACKET.finditer(text):
        for part in match.group(1).split(";"):
            part = part.strip()
            if _LOOKS_LIKE_CITATION.search(part) and part not in found:
                found.append(part)
    return found


def unverified(text: str, sources: set[str]) -> list[str]:
    """Citations in text that do not match any source read in this request."""
    known = {normalize(source) for source in sources}
    return [citation for citation in citations_in(text) if normalize(citation) not in known]


def annotate(text: str, missing: list[str]) -> str:
    if not missing:
        return text
    listed = "; ".join(f"[{c}]" for c in missing)
    return f"{text}\n\n⚠ Unverified source(s), not read in this request: {listed}"
