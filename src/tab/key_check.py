"""Checks the hand-transcribed answer key against the expert's (flattened) return PDF: every amount must appear as
a whole token on the page it cites, and the key must cover exactly the lines the task asks for."""
import re

from . import TabError, pdf_text
from .rubyisms import to_s


def run(persona) -> dict:
    pdf = persona.expert_return_path
    if not pdf.is_file():
        raise TabError(f"Expert return not found: {pdf.as_posix()}")

    pages = pdf_text.extract(pdf)
    key = persona.answer_key()
    rows = []
    for fid, entries in key["forms"].items():
        for lid, spec in entries.items():
            value = spec.get("value")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                index = int(spec.get("page") or 0) - 1  # Ruby: pages[page.to_i - 1] || '' (index -1 = last page)
                text = pages[index] if -len(pages) <= index < len(pages) else ""
                found = re.search(r"(?<!\d)" + re.escape(to_s(abs(value))) + r"(?!\d)", text)
                # A zero the expert left blank has no text to find; the form reads a blank amount line as 0.
                status = "found" if found else "blank (read as 0)" if value == 0 else "NOT FOUND"
            else:
                status = "not text-checkable (checked on the rendered page)"
            rows.append({"line": f"{fid}.{lid}", "value": value, "page": spec.get("page"), "status": status})

    key_lines = sorted(f"{fid}.{lid}" for fid, entries in key["forms"].items() for lid in entries)
    spec_lines = sorted(f"{fid}.{lid}" for fid, f in persona.output_spec.items() for lid in f["lines"])
    return {"pdf": pdf.as_posix(), "pages": len(pages), "rows": rows,
            "not_found": [r["line"] for r in rows if r["status"] == "NOT FOUND"],
            "in_key_not_in_task": [l for l in key_lines if l not in spec_lines],
            "in_task_not_in_key": [l for l in spec_lines if l not in key_lines]}
