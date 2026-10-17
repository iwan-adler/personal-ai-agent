"""The Markdown renderer for model replies: formatting works, and no input becomes HTML."""

from html.parser import HTMLParser

import pytest

from app.web_ui.markdown import render


def test_common_formatting():
    html = str(render("## Joins\nA **join** combines *rows*; use `SELECT`.\n\n- one\n- two\n  - nested\n\n1. first\n2. second"))
    assert "<h4>Joins</h4>" in html
    assert "<strong>join</strong>" in html and "<em>rows</em>" in html and "<code>SELECT</code>" in html
    assert "<ul><li>one</li><li>two<ul><li>nested</li></ul></li></ul>" in html
    assert "<ol><li>first</li><li>second</li></ol>" in html


def test_code_blocks_tables_quotes_and_rules():
    html = str(render("```python\nif a < b:\n    print('**x**')\n```\n| A | B |\n|---|---|\n| 1 | **2** |\n> note\n\n---"))
    assert '<span class="code-lang">python</span>' in html
    assert "if a &lt; b:\n    print(&#39;**x**&#39;)" in html  # code is never formatted
    assert "<th>A</th>" in html and "<td><strong>2</strong></td>" in html
    assert "<blockquote><p>note</p></blockquote>" in html and "<hr>" in html


def test_links_only_for_https():
    html = str(render("[docs](https://example.org/a?b=1&c=2) and https://example.org/x."))
    assert '<a href="https://example.org/a?b=1&amp;c=2" rel="noopener noreferrer nofollow">docs</a>' in html
    assert '<a href="https://example.org/x" rel="noopener noreferrer nofollow">https://example.org/x</a>.' in html


@pytest.mark.parametrize("attack", [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "[click](javascript:alert(1))",
    "[click](http://evil.example)",
    '[x](https://ok.example" onmouseover="alert(1))',
    "`<b>`</b>",
    "**<i>bold</i>**",
    "| <script>x</script> | b |\n|---|---|\n| <img src=x> | c |",
    "```\n</code></pre><script>alert(1)</script>\n```",
    "> <iframe src=x>",
    "\x00<script>0</script>\x00",
])
def test_nothing_from_the_model_becomes_html(attack):
    """Security: everything is escaped before our own tags are added."""
    parser = _Elements()
    parser.feed(str(render(attack)))
    for tag, attrs in parser.found:
        assert tag in ALLOWED_TAGS, tag
        for name, value in attrs:
            assert (tag, name) in ALLOWED_ATTRS, (tag, name)
            if name == "href":
                assert value.startswith("https://") and '"' not in value, value


ALLOWED_TAGS = {"p", "br", "strong", "em", "code", "pre", "div", "span", "ul", "ol", "li", "h3", "h4",
                "h5", "h6", "blockquote", "hr", "table", "thead", "tbody", "tr", "th", "td", "a"}
ALLOWED_ATTRS = {("a", "href"), ("a", "rel"), ("div", "class"), ("span", "class")}


class _Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.found = []

    def handle_starttag(self, tag, attrs):
        self.found.append((tag, attrs))


def test_empty_and_plain_text():
    assert str(render("")) == "" and str(render(None)) == ""
    assert str(render("snake_case_name and 2*3*4")) == "<p>snake_case_name and 2*3*4</p>"
