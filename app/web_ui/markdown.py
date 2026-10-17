"""A small, safe Markdown renderer for model replies (no dependency).

Safety comes from the order of work: every piece of text is HTML-escaped
*first*, and only then do we add the few tags we generate ourselves. Nothing
from the model is ever passed through as HTML, so a reply containing
<script> or an onclick attribute shows up as text. Links are only made for
https:// URLs and always get rel="noopener noreferrer nofollow".

Supported: headings, paragraphs, bold, italic, inline code, fenced code
blocks, bullet and numbered lists (one level of nesting), block quotes,
tables and horizontal rules. Anything else stays plain text.
"""

from __future__ import annotations

import re

from markupsafe import Markup
from markupsafe import escape as _ms_escape


def escape(text: str) -> str:
    """Same escaping as the templates (& < > " ' all escaped)."""
    return str(_ms_escape(text))

_FENCE = re.compile(r"^\s*(```|~~~)\s*([\w+#.-]*)\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^(\s*)(\d{1,9})[.)]\s+(.*)$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")

_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__")
_ITALIC = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])|(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])")
_LINK = re.compile(r"\[([^\]\n]{1,200})\]\((https://[^\s()<>\"']{1,500})\)")
_BARE_URL = re.compile(r"(?<![\"'=>])\bhttps://[^\s<>\"'()]{1,500}[^\s<>\"'().,;:!?]")
_PLACEHOLDER = "\x00{}\x00"


def _inline(text: str) -> str:
    """Escape, then add inline formatting. Code spans are protected from the rest."""
    stash: list[str] = []

    def keep(html: str) -> str:
        stash.append(html)
        return _PLACEHOLDER.format(len(stash) - 1)

    text = text.replace("\x00", "")
    text = _CODE_SPAN.sub(lambda m: keep(f"<code>{escape(m.group(1))}</code>"), text)
    text = _LINK.sub(lambda m: keep(_anchor(m.group(2), _format(escape(m.group(1))))), text)
    text = escape(text)
    text = _BARE_URL.sub(lambda m: keep(_anchor(m.group(0), m.group(0))), text)
    text = _format(text)
    return re.sub("\x00(\\d+)\x00", lambda m: stash[int(m.group(1))], text)


def _format(escaped: str) -> str:
    escaped = _BOLD.sub(lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", escaped)
    return _ITALIC.sub(lambda m: f"<em>{m.group(1) or m.group(2)}</em>", escaped)


def _anchor(url: str, label_html: str) -> str:
    # url came from an https-only pattern; escape again in case it was not escaped yet.
    safe_url = escape(url.replace("&amp;", "&"))
    return f'<a href="{safe_url}" rel="noopener noreferrer nofollow">{label_html}</a>'


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in line.split("|")]


def _render_list(items: list[tuple[int, bool, str]]) -> str:
    """items: (indent, numbered, text). One level of nesting is enough for chat replies."""
    out: list[str] = []
    base = items[0][0]
    tag = "ol" if items[0][1] else "ul"
    out.append(f"<{tag}>")
    i = 0
    while i < len(items):
        indent, _, text = items[i]
        out.append(f"<li>{_inline(text)}")
        j = i + 1
        children = []
        while j < len(items) and items[j][0] > base:
            children.append(items[j])
            j += 1
        if children:
            out.append(_render_list(children))
        out.append("</li>")
        i = j
    out.append(f"</{tag}>")
    return "".join(out)


def render(text: str | None) -> Markup:
    if not text:
        return Markup("")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            out.append("<p>" + "<br>".join(_inline(line) for line in paragraph) + "</p>")
            paragraph.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        fence = _FENCE.match(line)
        if fence:
            flush()
            marker, lang = fence.group(1), fence.group(2)
            body = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(marker):
                body.append(lines[i])
                i += 1
            i += 1  # closing fence (or end of text)
            label = f'<span class="code-lang">{escape(lang)}</span>' if lang else ""
            out.append(f'<div class="code">{label}<pre><code>{escape(chr(10).join(body))}</code></pre></div>')
            continue
        if not line.strip():
            flush()
            i += 1
            continue
        heading = _HEADING.match(line)
        if heading:
            flush()
            level = min(len(heading.group(1)) + 2, 6)  # replies sit inside the page: h3-h6
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            i += 1
            continue
        if _RULE.match(line):
            flush()
            out.append("<hr>")
            i += 1
            continue
        if "|" in line and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            flush()
            header = _cells(line)
            i += 2
            rows = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i]))
                i += 1
            head = "".join(f"<th>{_inline(c)}</th>" for c in header)
            body = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row[:len(header)])
                           + "<td></td>" * max(0, len(header) - len(row)) + "</tr>" for row in rows)
            out.append(f'<div class="table"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>')
            continue
        if _QUOTE.match(line):
            flush()
            quoted = []
            while i < len(lines) and _QUOTE.match(lines[i]):
                quoted.append(_QUOTE.match(lines[i]).group(1))
                i += 1
            out.append(f"<blockquote>{render(chr(10).join(quoted))}</blockquote>")
            continue
        if _BULLET.match(line) or _NUMBERED.match(line):
            flush()
            items: list[tuple[int, bool, str]] = []
            while i < len(lines):
                bullet, numbered = _BULLET.match(lines[i]), _NUMBERED.match(lines[i])
                if items and (bullet or numbered):
                    indent = len((bullet or numbered).group(1).expandtabs(4))
                    if indent <= items[0][0] and bool(numbered) != items[0][1]:
                        break  # a different kind of list starts here
                if bullet:
                    items.append((len(bullet.group(1).expandtabs(4)), False, bullet.group(2)))
                elif numbered:
                    items.append((len(numbered.group(1).expandtabs(4)), True, numbered.group(3)))
                elif lines[i].strip() and lines[i].startswith((" ", "\t")) and items:
                    indent, kind, previous = items[-1]
                    items[-1] = (indent, kind, previous + " " + lines[i].strip())  # continuation line
                else:
                    break
                i += 1
            out.append(_render_list(items))
            continue
        paragraph.append(line)
        i += 1
    flush()
    return Markup("".join(out))
