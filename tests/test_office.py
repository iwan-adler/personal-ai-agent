"""Word and PowerPoint import: text comes out, booby-trapped files do not get in."""

import zipfile

import pytest

from app.memory.documents import DocumentStore
from app.tools.docs import DocImportInput, _doc_import, extract_office
from app.tools.workspace import WorkspaceError

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'


def make_docx(path, paragraphs, extra=None):
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {W}><w:body>{body}</w:body></w:document>')
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return path


def make_pptx(path, slides):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for number, lines in enumerate(slides, start=1):
            paras = "".join(f"<a:p><a:r><a:t>{line}</a:t></a:r></a:p>" for line in lines)
            z.writestr(f"ppt/slides/slide{number}.xml", f"<p:sld {A} xmlns:p='x'><a:txBody>{paras}</a:txBody></p:sld>")
    return path


def test_docx_paragraphs(tmp_path):
    path = make_docx(tmp_path / "notes.docx", ["Normalization", "1NF removes repeating groups."])
    assert extract_office(path, "docx") == ["Normalization\n1NF removes repeating groups."]


def test_pptx_one_page_per_slide_in_order(tmp_path):
    slides = [[f"Slide {n}", "point"] for n in range(1, 12)]  # slide10 must not sort before slide2
    pages = extract_office(make_pptx(tmp_path / "deck.pptx", slides), "pptx")
    assert len(pages) == 11 and pages[0] == "Slide 1\npoint" and pages[9].startswith("Slide 10")


def test_import_with_citations(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    make_pptx(library / "joins.pptx", [["Intro"], ["INNER JOIN keeps matching rows"]])
    make_docx(library / "lab.docx", ["Exercise 1", "Write a LEFT JOIN query"])
    store = DocumentStore(tmp_path / "agent.db")
    out = _doc_import(tmp_path, store, DocImportInput(path="."))
    assert out.imported == 2 and not out.problems
    assert store.search("INNER")[0].citation == "joins.pptx, p. 2"
    assert store.search("LEFT")[0].citation == "lab.docx, line 1"


@pytest.mark.parametrize("payload", [
    '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><w:document %s>&b;</w:document>' % W,
    '<!DOCTYPE x SYSTEM "file:///C:/Windows/win.ini"><w:document %s/>' % W,
])
def test_xml_with_entities_is_refused(tmp_path, payload):
    """Security: no entity expansion (billion laughs) and no external entities."""
    path = tmp_path / "evil.docx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", payload)
    with pytest.raises(WorkspaceError, match="unreadable"):
        extract_office(path, "docx")


def test_zip_bomb_is_refused(tmp_path):
    """Security: a small file that unpacks to a huge one is stopped before reading."""
    path = tmp_path / "bomb.docx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", "<w:document %s>" % W + " " * 30_000_000 + "</w:document>")
    assert path.stat().st_size < 1_000_000
    with pytest.raises(WorkspaceError, match="too large"):
        extract_office(path, "docx")


@pytest.mark.parametrize("content", [b"not a zip at all", b"PK\x03\x04broken"])
def test_broken_files_are_reported(tmp_path, content):
    path = tmp_path / "broken.pptx"
    path.write_bytes(content)
    with pytest.raises(WorkspaceError, match="unreadable"):
        extract_office(path, "pptx")
