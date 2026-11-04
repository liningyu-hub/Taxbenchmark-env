"""The tools the agent can use. Every method returns (output text, detail dict for the action log).

All paths are relative to the workspace; nothing outside it can be read or written, and no tool returns hidden
material (answer key, guides, the action log itself). Paths are checked against real paths, so symlinks and
Windows junctions out of the workspace are refused, and "\\" counts as a separator on every OS.
"""
import datetime as dt
import hashlib
import json
import os
import posixpath
import re
from pathlib import Path, PureWindowsPath

from . import TabError, pdf_text
from .rubyisms import glob_files, ruby_integer, ruby_lines, ruby_regex, ruby_strip, to_s
from .run import read_text, write_text
from .sim_user import SimUser
from . import submission as submission_mod

MAX_SEARCH_HITS = 200
MAX_WRITE_BYTES = 1_000_000
TEXT_EXTENSIONS = (".md", ".txt", ".json", ".csv")

_DATE = re.compile(r"^\*\*Date:\*\*\s*(.+?)\s*$", re.M | re.ASCII)
_FROM = re.compile(r"^\*\*From:\*\*\s*(.+?)\s*$", re.M | re.ASCII)
_SUBJECT = re.compile(r"^\*\*Subject:\*\*\s*(.+?)\s*$", re.M | re.ASCII)
_HEADING = re.compile(r"^# (.+)$", re.M | re.ASCII)
_ATTACHMENT = re.compile(r"^\*\*Attachment:\*\*\s*(.+?)\s*$", re.M | re.ASCII)
_ADDRESS = re.compile(r"\A[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+\Z")


def _first(rx, text):
    m = rx.search(text)
    return m.group(1) if m else None


def _within(child: str, parent: str) -> bool:
    """child is parent or inside it (case-insensitive where the OS is)."""
    c, p = os.path.normcase(child), os.path.normcase(parent)
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


class AgentTools:
    def __init__(self, run):
        self.run = run
        self.persona = run.persona
        self.root = os.path.realpath(run.workspace)

    def task(self):
        return self.persona.task_text(self.run.task), {}

    def status(self):
        ids = self.run.submission_ids()
        latest = "" if not ids else f" (latest is #{ids[-1]})"
        lines = [f"Environment run: {self.run.run_id}",
                 f"Taxpayer: {self.persona.name}, tax year {self.persona.tax_year}",
                 f"Today's date: {self.run.state().now.date().isoformat()}",
                 f"Submissions so far: {len(ids)}{latest}"]
        return "\n".join(lines), {}

    def ls(self, path="."):
        full = self._resolve(path)
        if os.path.isfile(full):
            raise TabError(f"{path} is a file; use: tab read {path}")
        entries = []
        for e in sorted(n for n in os.listdir(full) if not n.startswith(".")):
            f = os.path.join(full, e)
            entries.append(f"{e}/" if os.path.isdir(f) else f"{e:<64} {os.path.getsize(f):>9} bytes")
        return ("\n".join(entries) if entries else "(empty)"), {"path": self._rel(full), "entries": len(entries)}

    def tree(self):
        files = self._workspace_files()
        return "\n".join(files), {"files": len(files)}

    def read(self, path, page=None):
        full = self._resolve(path)
        if os.path.isdir(full):
            raise TabError(f"{path} is a folder; use: tab ls {path}")
        relp = self._rel(full)
        doc = next((d for d in self.persona.tax_documents if d["place"] == relp), None)
        if relp.lower().endswith(".pdf"):
            pages = self._pdf_pages(relp, full)
            numbers = [page] if page is not None else list(range(1, len(pages) + 1))
            for n in numbers:
                if not 1 <= n <= len(pages):
                    raise TabError(f"Page {n} is out of range (1-{len(pages)})")
            text = "\n".join(f"--- page {n} of {len(pages)} ---\n{pages[n - 1]}" for n in numbers)
            detail = {"path": relp, "kind": "pdf", "pages": len(pages)}
            if page is not None:
                detail["page"] = page
            if doc:
                detail["doc"] = doc["doc"]
            return text, detail
        if posixpath.splitext(relp)[1].lower() in TEXT_EXTENSIONS:
            return read_text(full), {"path": relp, "kind": "text"}
        raise TabError(f"{path} is not a text or PDF file")

    def search(self, pattern):
        if not ruby_strip(to_s(pattern)):
            raise TabError('Give a search pattern, for example: tab search "1099"')
        try:
            rx = ruby_regex(pattern, ignorecase=True)
        except re.error:
            rx = ruby_regex(re.escape(pattern), ignorecase=True)
        hits = []
        for relp in self._workspace_files():
            for line, loc in self._searchable_lines(relp):
                if rx.search(line):
                    hits.append(f"{relp}:{loc}: {ruby_strip(line)}")
        shown = hits[:MAX_SEARCH_HITS]
        out = "\n".join(shown) if shown else "No matches."
        if len(hits) > len(shown):
            out += f"\n... and {len(hits) - len(shown)} more matches"
        return out, {"pattern": pattern, "matches": len(hits)}

    def inbox(self):
        rows = []
        for relp in self._workspace_files():
            if not (relp.startswith("Email/") and relp.endswith(".md")):
                continue
            text = read_text(os.path.join(self.root, relp))
            date = _first(_DATE, text) or "-"
            sender = _first(_FROM, text) or "-"
            subject = _first(_SUBJECT, text) or _first(_HEADING, text) or ""
            attachment = _first(_ATTACHMENT, text)
            if attachment:  # an attached file the environment placed beside the email is shown by its path
                stored = f"Email/attachments/{posixpath.basename(relp)[:-3]}/{attachment}"
                if os.path.isfile(os.path.join(self.root, stored)):
                    attachment = stored
            extra = f" [attachment: {attachment}]" if attachment else ""
            rows.append(f"{date} | {sender} | {subject}{extra}\n    {relp}")
        return f"{len(rows)} messages\n" + "\n".join(rows), {"messages": len(rows)}

    def open_link(self, url):
        if not ruby_strip(to_s(url)):
            raise TabError("Give a URL, for example: tab open https://example.com")
        hits = [t for t in self.persona.traps if t.get("tool") == "open" and t["match"] in url]
        page = next((t["page"] for t in hits if t.get("page") is not None), None)
        if page is None:
            page = (f"Simulated browser: {url} can't be reached from this environment. "
                    f"The websites {self.persona.name} uses are under Portals/.")
        return page, {"url": url, "traps": [t["id"] for t in hits]}

    def ask(self, question):
        if not ruby_strip(to_s(question)):
            raise TabError('Ask a question, for example: tab ask "What is your date of birth?"')
        data = self.persona.simulated_user()
        answer, facts = SimUser(data, self.run.task).answer(question)
        detail = {"question": question, "facts": facts}
        # A fact can carry what saying it grants (a "yes" grants the action asked about): an approval given in words.
        grants = sorted({g for f in data["facts"] if f["id"] in facts for g in f.get("grants", [])})
        if grants:
            detail["grants"] = grants
        return f"{self.persona.name}: {answer}", detail

    def write(self, path, content: bytes):
        writable = self.persona.writable_folder
        allowed = os.path.join(self.root, *writable.split("/"))
        full = os.path.normpath(os.path.join(self.root, to_s(path).replace("\\", "/")))
        if not _within(full, allowed) or os.path.normcase(full) == os.path.normcase(allowed):
            raise TabError(f"You can only write under {writable}/")
        if not content:
            raise TabError("Nothing to write: send the content on standard input")
        if len(content) > MAX_WRITE_BYTES:
            raise TabError("Content is larger than 1 MB")
        os.makedirs(os.path.dirname(full), exist_ok=True)
        if not _within(os.path.realpath(os.path.dirname(full)), os.path.realpath(allowed)):
            raise TabError(f"You can only write under {writable}/")
        Path(full).write_bytes(content)
        return f"Saved {self._rel(full)} ({len(content)} bytes)", {"path": self._rel(full), "bytes": len(content)}

    def wait(self, days=None, hours=None, until=None):
        """Let simulated time pass: emails and messages dated up to the new time arrive."""
        from . import clock, timeline
        given = [x for x in (days, hours, until) if x is not None]
        if len(given) != 1:
            raise TabError("Usage: tab wait --days N | --hours N | --until YYYY-MM-DD[THH:MM]")
        now = self.run.state().now
        if until is not None:
            target = clock.parse_when(until)
        else:
            amount = ruby_integer(days if days is not None else hours)
            if amount <= 0:
                raise TabError("Give a positive number of days or hours")
            try:
                target = now + (dt.timedelta(days=amount) if days is not None else dt.timedelta(hours=amount))
            except OverflowError:
                raise TabError("That is further ahead than the calendar goes; wait a shorter time") from None
        if target > clock.LATEST:
            raise TabError("That is further ahead than the calendar goes; wait a shorter time")
        if target < now:
            raise TabError(f"Time only moves forward: it is already {clock.format_sim(now)}")
        released = timeline.advance(self.run, target)
        out = [f"It is now {clock.format_sim(target)}."]
        if released:
            out.append(f"{len(released)} new item{'s' if len(released) != 1 else ''}:")
            out += [f"  {p}" for p in released]
        else:
            out.append("Nothing new arrived.")
        return "\n".join(out), {"now": target.strftime("%Y-%m-%dT%H:%M"), "released": released}

    def send(self, to: list, cc: list, subject, attachments: list, body: bytes):
        """Send an email as the persona. Attachments are files the agent can see in the workspace."""
        from . import clock
        from .events import unique_path
        from .renderer import Renderer
        from .rubyisms import json_compact
        from .timeline import deliver
        if not to:
            raise TabError("Usage: tab send --to ADDRESS [--cc ADDRESS] --subject TEXT [--attach PATH]... < MESSAGE")
        for address in to + cc:
            if not _ADDRESS.match(address):
                raise TabError(f"Not an email address: {address}")
        if not ruby_strip(to_s(subject)):
            raise TabError("Give a subject: --subject TEXT")
        if len(body) > MAX_WRITE_BYTES:
            raise TabError("The message is larger than 1 MB")
        message = body.decode("utf-8")
        if not ruby_strip(message):
            raise TabError("Nothing to send: write the message on standard input")
        if len(attachments) > 10:
            raise TabError("Attach at most 10 files")
        paths = []
        for path in attachments:
            full = self._resolve(path)
            if os.path.isdir(full):
                raise TabError(f"{path} is a folder; attach a file")
            paths.append(self._rel(full))
        hashes = [hashlib.sha256(Path(self.root, p).read_bytes()).hexdigest() for p in paths]
        # What each attached text file said when sent (graders read this, not the file as it is later)
        texts = [read_text(Path(self.root, p)) if posixpath.splitext(p)[1].lower() in (".md", ".txt") else None
                 for p in paths]

        state = self.run.state()
        day = state.now.date()
        lines = [f"# {subject}", "", f"**From:** {self.persona.name} <{self.persona.data.get('email', '')}>  ",
                 f"**To:** {', '.join(to)}  "]
        if cc:
            lines.append(f"**Cc:** {', '.join(cc)}  ")
        lines += [f"**Date:** {clock.format_email_date(day)}  ", f"**Subject:** {subject}  "]
        if paths:
            lines.append(f"**Attachment:** {', '.join(posixpath.basename(p) for p in paths)}  ")
        content = "\n".join(lines + ["", message.rstrip("\n"), ""])
        sent = unique_path(self.run.workspace, "Sent/" + Renderer.email_name(f"{day.isoformat()}_sent.md", content))
        deliver(self.run, sent, content, state, log=False)
        outbox = self.run.path("outbox.jsonl")
        number = sum(1 for _ in open(outbox, "rb")) + 1 if outbox.is_file() else 1
        record = {"id": number, "at": state.data["now"], "to": to, "cc": cc, "subject": subject, "body": message,
                  "attachments": paths, "attachment_sha256": hashes, "attachment_text": texts,
                  "path": sent}
        with open(outbox, "ab") as f:
            f.write((json_compact(record) + "\n").encode("utf-8"))
        state.save()
        out = [f'Sent to {", ".join(to)}: "{subject}"']
        if paths:
            out.append("Attached: " + ", ".join(paths))
        out.append(f"A copy is in {sent}.")
        return "\n".join(out), {"to": to, "cc": cc, "subject": subject, "attachments": paths,
                                     "attachment_sha256": hashes, "sent": sent, "body": message}

    def copy(self, src, dest):
        """Copy a visible workspace file (a PDF too) into the work folder: DEST is a file path, or a folder ending
        in "/" (the file keeps its name)."""
        import shutil
        full = self._resolve(src)
        if os.path.isdir(full):
            raise TabError(f"{src} is a folder; copy files one at a time")
        writable = self.persona.writable_folder
        allowed = os.path.join(self.root, *writable.split("/"))
        rel_dest = ruby_strip(to_s(dest)).replace("\\", "/")
        if rel_dest.startswith(("/", "~")) or PureWindowsPath(rel_dest).drive:
            raise TabError(f"You can only copy into {writable}/")
        if rel_dest.endswith("/") or os.path.isdir(os.path.join(self.root, rel_dest)):
            rel_dest = posixpath.join(rel_dest.rstrip("/"), posixpath.basename(self._rel(full)))
        target = os.path.normpath(os.path.join(self.root, rel_dest))
        if not _within(target, allowed) or os.path.normcase(target) == os.path.normcase(allowed):
            raise TabError(f"You can only copy into {writable}/")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if not _within(os.path.realpath(os.path.dirname(target)), os.path.realpath(allowed)):
            raise TabError(f"You can only copy into {writable}/")
        if os.path.isdir(target):
            raise TabError(f"{self._rel(target)} is a folder")
        if os.path.isfile(target) and Path(target).read_bytes() != Path(full).read_bytes():
            # Forms from different issuers often share a file name: never replace one with another silently.
            raise TabError(f"{self._rel(target)} already exists with different content; give the copy another name")
        shutil.copyfile(full, target)
        size = os.path.getsize(target)
        return (f"Copied {self._rel(full)} to {self._rel(target)} ({size} bytes)",
                {"from": self._rel(full), "to": self._rel(target), "bytes": size})

    def reply(self, message: bytes):
        """A message to the person the agent works for (an answer, a summary, a brief), kept with its simulated
        time in <run>/replies.jsonl, outside the workspace."""
        from .rubyisms import json_compact
        if len(message) > MAX_WRITE_BYTES:
            raise TabError("The message is larger than 1 MB")
        text = message.decode("utf-8")
        if not ruby_strip(text):
            raise TabError('Nothing to say: tab reply "MESSAGE" or tab reply < FILE')
        path = self.run.path("replies.jsonl")
        number = sum(1 for _ in open(path, "rb")) + 1 if path.is_file() else 1
        record = {"id": number, "at": self.run.state().data["now"], "text": text}
        with open(path, "ab") as f:
            f.write((json_compact(record) + "\n").encode("utf-8"))
        return f"Sent to {self.persona.name}.", {"reply_id": number, "chars": len(text)}

    def submit(self, path=None, text=None):
        """Submit from a workspace file (path) or from text given on standard input (text)."""
        if path is not None:
            text = self._read_workspace_file(path)
        parsed = submission_mod.parse(text, self.persona)
        id = self.run.save_submission(parsed["submission"], text)
        out = [f"Submission #{id} saved for {self.persona.name}, tax year {self.persona.tax_year}.",
               f"Lines received: {parsed['line_count']}."]
        if parsed["warnings"]:
            out.append("Warnings:\n  - " + "\n  - ".join(parsed["warnings"]))
        out.append("You can resubmit; only the last submission counts.")
        return "\n".join(out), {"submission_id": id, "lines": parsed["line_count"], "warnings": parsed["warnings"]}

    # -- helpers -------------------------------------------------------------------------------------------------

    def _resolve(self, path) -> str:
        """A workspace-relative path; refuses anything that leaves the workspace (.., absolute, drive letters,
        UNC paths, symlinks and junctions)."""
        raw = ruby_strip(to_s(path)) or "."
        rel_path = raw.replace("\\", "/")
        if rel_path.startswith(("/", "~")) or PureWindowsPath(raw).drive:
            raise TabError(f"Use a path inside the environment, for example Email or Portals (got {path})")
        full = os.path.normpath(os.path.join(self.root, rel_path))
        if not _within(full, self.root):
            raise TabError(f"Path is outside the environment: {path}")
        if not os.path.exists(full):
            raise TabError(f"No such file or folder: {path}")
        if not _within(os.path.realpath(full), self.root):
            raise TabError(f"Path is outside the environment: {path}")
        return full

    def _rel(self, full: str) -> str:
        if os.path.normcase(full) == os.path.normcase(self.root):
            return "."
        return Path(os.path.relpath(full, self.root)).as_posix()

    def _read_workspace_file(self, path) -> str:
        full = self._resolve(path)
        if os.path.isdir(full):
            raise TabError(f"{path} is a folder")
        return read_text(full)

    def _workspace_files(self) -> list:
        return glob_files(self.root)

    def _pdf_pages(self, relp: str, full: str) -> list:
        cache = self.run.cache_dir / f"{relp}.txt"
        if cache.is_file():
            return read_text(cache).split("\f")
        return pdf_text.extract(full)

    def _searchable_lines(self, relp: str) -> list:
        """[(line, location)] for a file: "12" is a line number, "p2" a PDF page."""
        full = os.path.join(self.root, relp)
        if relp.lower().endswith(".pdf"):
            return [(line, f"p{i + 1}") for i, text in enumerate(self._pdf_pages(relp, full))
                    for line in ruby_lines(text)]
        if posixpath.splitext(relp)[1].lower() in TEXT_EXTENSIONS:
            return [(line, str(i + 1)) for i, line in enumerate(ruby_lines(read_text(full)))]
        return []
