"""Extract the text of a Word (.docx) or PowerPoint (.pptx) file. Runs as a separate process.

Like pdf_extract.py, this parses untrusted files, so it is started through
run_command: isolated mode (-I), an allowlisted environment without keys and a
timeout that kills it. It only uses the standard library and prints one JSON
object:

    {"pages": ["text of slide 1", ...]}   for .pptx (one entry per slide)
    {"pages": ["whole document text"]}    for .docx (paragraphs on separate lines)
    {"error": "too_large" | "too_much_text" | "unreadable"}

Office files are ZIP archives of XML. Guards:
- zip bombs: the archive's declared sizes are checked before anything is read,
  and reading stops at the limit anyway;
- XML tricks: any DOCTYPE or ENTITY declaration is refused, so no entity
  expansion ("billion laughs") and no external entities;
- only the members needed for text are read; nothing is ever extracted to disk.

Usage: python -I office_extract.py <path> <docx|pptx> <max_chars>
"""

import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

MAX_MEMBERS = 5_000
MAX_MEMBER_BYTES = 20_000_000
MAX_TOTAL_BYTES = 100_000_000
MAX_SLIDES = 1_000
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_SLIDE = re.compile(r"^ppt/slides/slide(\d{1,5})\.xml$")


class TooLarge(Exception):
    pass


def _read(archive: zipfile.ZipFile, name: str) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > MAX_MEMBER_BYTES:
        raise TooLarge
    with archive.open(info) as handle:
        data = handle.read(MAX_MEMBER_BYTES + 1)
    if len(data) > MAX_MEMBER_BYTES:
        raise TooLarge
    return data


def _xml(data: bytes) -> ET.Element:
    head = data[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in data.upper():
        raise ValueError("DTD not allowed")
    return ET.fromstring(data)


def _docx(archive: zipfile.ZipFile) -> list[str]:
    root = _xml(_read(archive, "word/document.xml"))
    lines = []
    for paragraph in root.iter(f"{W}p"):
        parts = []
        for node in paragraph.iter():
            if node.tag == f"{W}t" and node.text:
                parts.append(node.text)
            elif node.tag == f"{W}tab":
                parts.append("\t")
            elif node.tag in (f"{W}br", f"{W}cr"):
                parts.append("\n")
        text = "".join(parts).strip()
        if text:
            lines.append(text)
    return ["\n".join(lines)]


def _pptx(archive: zipfile.ZipFile) -> list[str]:
    numbers = sorted(int(m.group(1)) for name in archive.namelist() if (m := _SLIDE.match(name)))
    if len(numbers) > MAX_SLIDES:
        raise TooLarge
    slides = []
    for number in numbers:
        root = _xml(_read(archive, f"ppt/slides/slide{number}.xml"))
        paragraphs = []
        for paragraph in root.iter(f"{A}p"):
            text = "".join(node.text or "" for node in paragraph.iter(f"{A}t")).strip()
            if text:
                paragraphs.append(text)
        slides.append("\n".join(paragraphs))
    return slides


def extract(path: str, kind: str, max_chars: int) -> dict:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_MEMBERS or sum(i.file_size for i in infos) > MAX_TOTAL_BYTES:
                return {"error": "too_large"}
            pages = _docx(archive) if kind == "docx" else _pptx(archive)
    except TooLarge:
        return {"error": "too_large"}
    except (zipfile.BadZipFile, KeyError, ValueError, ET.ParseError, OSError, RuntimeError, EOFError):
        return {"error": "unreadable"}
    if sum(len(p) for p in pages) > max_chars:
        return {"error": "too_much_text"}
    return {"pages": pages}


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[2] not in ("docx", "pptx"):
        print(json.dumps({"error": "usage"}))
        return 2
    result = extract(argv[1], argv[2], int(argv[3]))
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
