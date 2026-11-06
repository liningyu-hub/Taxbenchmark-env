"""Builds the agent's workspace from the persona drafts in the Day 1 Environment folder:
- copies the visible folders (email, portals, documents, calendar, texts) under plain names
- strips reviewer-only draft metadata (SAMPLE banner, "_Item N · ..._" lines, "Supports" footers)
- renames emails by date and subject, as a mail client would show them (draft file names label the traps)
- places each tax form PDF where its portal shows it, and caches the PDF text
- refuses to finish if any reviewer-only marker is left (leak scan)
00_TaxBench_Source (answer key, guides, change log) and the README files are never copied.
"""
import json
import posixpath
import re
import shutil
from pathlib import Path

from . import TabError, clock, pdf_text
from .rubyisms import glob_files, ruby_regex, ruby_strip
from .run import read_text, write_text

LEAK_PATTERNS = {
    "draft banner": ruby_regex(r"SAMPLE persona draft"),
    "reviewer footer": ruby_regex(r"_Supports:"),
    "trap label": ruby_regex(r"\bTrap:"),
    "answer key": ruby_regex(r"answer key", ignorecase=True),
    "TaxBench reference": ruby_regex(r"TaxBench"),
    "CPA fill-in note": ruby_regex(r"CPA fill-in", ignorecase=True),
    "item metadata": ruby_regex(r"^_Item [^\n]*_\s*$"),
    "test framing": ruby_regex(r"\((?:simulated|fictional)\)|agent's working folder", ignorecase=True),
    "event release line": ruby_regex(r"^_Release:"),
}
_FRAMING = re.compile(r" ?\((?:simulated|fictional)\)| \(the agent's working folder\)", re.IGNORECASE)
LEAK_FILENAME = ruby_regex(r"phishing|trap|taxbench|answer|expert|guide", ignorecase=True)

_BANNER = re.compile(r"\A> SAMPLE persona draft[^\n]*\n+", re.ASCII)
_ITEM = re.compile(r"^_Item [^\n]*_[ \t]*\n+", re.MULTILINE | re.ASCII)
_FOOTER = re.compile(r"\n---[ \t]*\n+_Supports:.*\Z", re.DOTALL | re.ASCII)
_DOC_REF = re.compile(r" ?\(= TaxBench DOC\d+\)", re.ASCII)
_BLANK_RUNS = re.compile(r"\n{3,}")
_DATE = re.compile(r"\A(\d{4}-\d{2}-\d{2})_", re.ASCII)
_SUBJECT = re.compile(r"^\*\*Subject:\*\*\s*(.+?)\s*$", re.MULTILINE | re.ASCII)
_HEADING = re.compile(r"^# (.+)$", re.MULTILINE | re.ASCII)


class Renderer:
    def __init__(self, persona, workspace, cache_dir, today=None, pending_dir=None):
        """With `today` and `pending_dir`, items dated after `today` (emails by their file-name date, text-message
        blocks by their stamp) are written to `pending_dir` instead of the workspace and listed in `self.pending`."""
        self.persona = persona
        self.workspace = Path(workspace)
        self.cache_dir = Path(cache_dir)
        self.today = today
        self.pending_dir = Path(pending_dir) if pending_dir else None
        self.pending = []
        self.hidden_pages = {}  # page path -> the date its hidden form line comes back (one per page)
        self._used = set()

    def build(self) -> dict:
        src = self.persona.source_dir
        if not src.is_dir():
            raise TabError(f"Persona source folder not found: {src.as_posix()}. "
                           "Set TAB_DAY1_DIR to the 'Day 1 Environment' folder.")
        copied = 0
        for frm, to in self.persona.visible_folders.items():
            from_dir = src / frm
            if not from_dir.is_dir():
                raise TabError(f"Missing source folder: {from_dir.as_posix()}")
            copied += self._copy_folder(from_dir, to)
        (self.workspace / self.persona.writable_folder).mkdir(parents=True, exist_ok=True)
        placed = [self._place(doc) for doc in self.persona.tax_documents]

        leaks = self.leak_scan(self.workspace)
        if leaks:
            raise TabError("Leak scan failed: " + ", ".join(f"{l['file']} ({l['rule']})" for l in leaks))
        return {"copied_files": copied, "tax_documents": placed}

    @staticmethod
    def clean_markdown(text: str) -> str:
        out = _BANNER.sub("", text, count=1)
        out = _ITEM.sub("", out)
        out = _FOOTER.sub("\n", out, count=1)
        out = Renderer.strip_framing(_DOC_REF.sub("", out))
        return ruby_strip(_BLANK_RUNS.sub("\n\n", out)) + "\n"

    @staticmethod
    def strip_framing(text: str) -> str:
        """Removes markers that tell the agent it is in a test: "(simulated)", "(fictional)" and
        "(the agent's working folder)" (Day 1 review). The drafts themselves are not changed."""
        return _FRAMING.sub("", text)

    @staticmethod
    def email_name(source_name: str, text: str) -> str:
        """File name an email gets in the workspace: its date (if the draft name has one) and its subject."""
        m = _DATE.match(source_name)
        date = m.group(1) if m else None
        s = _SUBJECT.search(text) or _HEADING.search(text)
        subject = s.group(1) if s else (source_name[:-3] if source_name.endswith(".md") else source_name)
        slug = re.sub(r"\A_+|_+\Z", "", re.sub(r"[^A-Za-z0-9]+", "_", subject))[:60]
        return "_".join(x for x in (date, slug) if x is not None) + ".md"

    @staticmethod
    def leak_scan(root) -> list:
        root = Path(root)
        hits = []
        for rel in glob_files(root):
            if LEAK_FILENAME.search(posixpath.basename(rel)):
                hits.append({"file": rel, "rule": "file name"})
            if not rel.endswith(".md"):
                continue
            text = read_text(root / rel)
            for rule, rx in LEAK_PATTERNS.items():
                if rx.search(text):
                    hits.append({"file": rel, "rule": rule})
        return hits

    def _copy_folder(self, from_dir: Path, to: str) -> int:
        count = 0
        email = to == self.persona.visible_folders.get("01_Email")
        texts = to == self.persona.visible_folders.get("05_Texts")
        for rel in glob_files(from_dir):
            source = from_dir / rel
            if rel.endswith(".md"):
                text = self.clean_markdown(read_text(source))
                dest_rel = rel
                if email:
                    folder = posixpath.dirname(rel)
                    name = self.email_name(posixpath.basename(rel), text)
                    dest_rel = posixpath.join(folder, name) if folder else name
                path = self._unique(f"{to}/{dest_rel}")
                day = clock.email_date(posixpath.basename(rel)) if email else None
                if self.today and day and day > self.today:
                    self._withhold(path, text, "file", day)
                elif texts and self.today:
                    self._write_texts(path, text)
                else:
                    (self.workspace / path).parent.mkdir(parents=True, exist_ok=True)
                    write_text(self.workspace / path, text)
            else:
                path = self._unique(f"{to}/{rel}")
                (self.workspace / path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, self.workspace / path)
            count += 1
        return count

    def _unique(self, rel: str) -> str:
        """A workspace path not yet used by this build (visible or withheld): name.md, name_2.md, ..."""
        if rel not in self._used:
            self._used.add(rel)
            return rel
        stem, ext = posixpath.splitext(rel)
        n = 2
        while f"{stem}_{n}{ext}" in self._used:
            n += 1
        self._used.add(f"{stem}_{n}{ext}")
        return f"{stem}_{n}{ext}"

    def _withhold(self, path: str, text: str, kind: str, day) -> None:
        target = self.pending_dir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        write_text(target, text)
        self.pending.append({"path": path, "kind": kind, "release": day.isoformat(), "source": path})

    def _write_texts(self, path: str, text: str) -> None:
        """A texts file shows only the message blocks dated up to today; later blocks wait in the pending store."""
        later = sorted({d for d in clock.text_block_dates(text) if d > self.today})
        (self.workspace / path).parent.mkdir(parents=True, exist_ok=True)
        write_text(self.workspace / path, clock.render_texts(text, self.today))
        if later:
            source = f"texts_full/{path}"
            (self.pending_dir / source).parent.mkdir(parents=True, exist_ok=True)
            write_text(self.pending_dir / source, text)
            self.pending += [{"path": path, "kind": "texts", "release": d.isoformat(), "source": source} for d in later]

    def _hide_line(self, doc: dict, available: str) -> None:
        """A withheld form's "page_line" (the site page's "your form is available" line) is taken off the page until
        the form's date; the full page waits in the pending store and is released with the form."""
        line = doc["page_line"]
        if line["path"] in self.hidden_pages:  # the page comes back whole from one snapshot: one hidden line per page
            raise TabError(f"{doc['doc']}: {line['path']} already hides another form's line (until "
                           f"{self.hidden_pages[line['path']]}); a page can hide one")
        page = self.workspace / line["path"]
        text = read_text(page) if page.is_file() else ""
        if text.count(line["text"]) != 1:
            raise TabError(f"{doc['doc']}: the line {line['text']!r} must occur once on {line['path']}")
        source = f"pages_full/{line['path']}"
        (self.pending_dir / source).parent.mkdir(parents=True, exist_ok=True)
        write_text(self.pending_dir / source, text)
        start = text.index(line["text"])
        end = start + len(line["text"])
        if text[end:end + 1] == "\n":
            end += 1
        write_text(page, text[:start] + text[end:])
        self.pending.append({"path": line["path"], "kind": "file", "release": available, "source": source})
        self.hidden_pages[line["path"]] = available

    def _place(self, doc: dict) -> dict:
        """Copies a tax form where the persona's world shows it. A form with "available_from" later than today (an
        email attachment that hasn't arrived) waits in the pending store until that date."""
        import datetime as dt
        source = self.persona.source_dir / doc["source"]
        if not source.is_file():
            raise TabError(f"Missing tax document {doc['doc']}: {source.as_posix()}")
        available = doc.get("available_from")
        withhold = bool(self.today and available and dt.date.fromisoformat(available) > self.today)
        if withhold:
            self.pending.append({"path": doc["place"], "kind": "file", "release": available, "source": doc["place"]})
            if doc.get("page_line"):
                self._hide_line(doc, available)
        dest = (self.pending_dir if withhold else self.workspace) / doc["place"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        pages = pdf_text.extract(dest)
        text = "\n".join(pages)
        missing = [t for t in doc.get("expect_text") or [] if t not in text]
        if missing:
            raise TabError(f"{doc['doc']} placed at {doc['place']} does not contain {json.dumps(missing)}; "
                           "check the placement table")
        cache = self.cache_dir / f"{doc['place']}.txt"
        cache.parent.mkdir(parents=True, exist_ok=True)
        write_text(cache, "\f".join(pages))
        return {"doc": doc["doc"], "place": doc["place"], "pages": len(pages)}
