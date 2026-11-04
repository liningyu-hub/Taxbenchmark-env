"""PDF text through PyMuPDF, the same on Windows, macOS and Linux (the Ruby version used macOS PDFKit).

Text is extracted once at init and cached per run, so agent reads don't depend on this. Lines are rebuilt from
PyMuPDF's words in reading order, joined with single spaces. Measured against the PDFKit text the saved Ruby run
cached, this keeps every page's words identical (plain get_text(sort=True) glues neighbouring words, e.g.
"RecipientOrchard") and gives the closest word order (similarity 0.74-0.92; the default mode gives 0.59-0.72).
"""
from pathlib import Path

from . import TabError


def available() -> bool:
    try:
        import pymupdf  # noqa: F401
    except ImportError:
        return False
    return True


def extract(path) -> list:
    """One string per page."""
    if not available():
        raise TabError("PDF text extraction needs PyMuPDF: pip install pymupdf")
    path = Path(path)
    if not path.is_file():
        raise TabError(f"No such PDF: {path.as_posix()}")
    import pymupdf
    try:
        with pymupdf.open(path) as doc:
            return [_page_text(page) for page in doc]
    except Exception as e:  # PyMuPDF raises its own types for damaged files
        raise TabError(f"Could not read {path.name}: {e}") from e


def _page_text(page) -> str:
    """Text blocks ordered by their top-left corner, each block's lines kept together, words joined by single
    spaces in extraction order; lines joined by newlines with no trailing newline, as PDFKit returns a page."""
    tops = {b[5]: (b[1], b[0]) for b in page.get_text("blocks") if b[6] == 0}  # block no -> (y0, x0)
    lines = {}
    for *_box, word, block, line, word_no in page.get_text("words"):
        lines.setdefault((block, line), []).append((word_no, word))
    order = sorted(lines, key=lambda bl: (tops.get(bl[0], (0.0, 0.0)), bl[0], bl[1]))
    return "\n".join(" ".join(w for _, w in sorted(lines[bl])) for bl in order)
