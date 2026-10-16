"""Extract the text of a PDF, page by page. Runs as a separate process.

PDFs are untrusted input, and PDF parsers have had endless-loop and memory
bugs on crafted files. So this script is started through run_command
(app/tools/process.py): isolated mode (-I), an allowlisted environment without
keys, and a timeout that kills the process. It imports nothing from the agent,
reads one file, and prints one JSON object:

    {"pages": ["text of page 1", ...]}        on success
    {"error": "encrypted" | "too_many_pages" | "too_much_text" | "unreadable"}

Usage: python -I pdf_extract.py <path> <max_pages> <max_chars>
"""

import json
import sys


def extract(path: str, max_pages: int, max_chars: int) -> dict:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(path, strict=False)
        if reader.is_encrypted:
            return {"error": "encrypted"}
        if len(reader.pages) > max_pages:
            return {"error": "too_many_pages"}
        pages: list[str] = []
        total = 0
        for page in reader.pages:
            text = page.extract_text() or ""
            total += len(text)
            if total > max_chars:
                return {"error": "too_much_text"}
            pages.append(text)
        return {"pages": pages}
    except (PdfReadError, ValueError, KeyError, TypeError, OSError):
        return {"error": "unreadable"}


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(json.dumps({"error": "usage"}))
        return 2
    result = extract(argv[1], int(argv[2]), int(argv[3]))
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
