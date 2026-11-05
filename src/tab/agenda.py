"""The persona's calendar as a tool (`tab calendar`).

  tab calendar                                   list the entries, with ids
  tab calendar add --date D [--end D] [--time HH:MM] [--until HH:MM] --title TEXT
  tab calendar remove --id ID [--date D]       remove an entry, or one date of a recurring entry

The calendar starts as the drafted calendar file (persona.json "calendar", a workspace path): each row of its
"| Date | Event |" table is an entry. Dates like "2026-03-13 → 03-15", "2026-03-14 10:00", "2026-03-21 09:00–13:00"
or "2026-03-06" are parsed; any other date text ("Fridays in Lent 18:00") is kept as written, undated: a recurring
entry, which can skip single dates ("except"). The entries,
with every addition and removal and its simulated time, live in state.json "calendar". After each change the
environment re-renders the workspace file (logged and recorded as a release, so integrity stays clean).
"""
import datetime as dt
import re

from . import TabError
from .run import read_text
from .timeline import deliver

_ROW = re.compile(r"^\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$")
_RANGE = re.compile(r"^(\d{4})-(\d{2}-\d{2})\s*→\s*(?:(\d{4})-)?(\d{2}-\d{2})$")
_TIMED = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})(?:\s*[–-]\s*(\d{1,2}:\d{2}))?$")
_DAY = re.compile(r"^(\d{4}-\d{2}-\d{2})$")
_TIME = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def parse_when(text: str) -> dict:
    m = _RANGE.match(text)
    if m:
        return {"date": f"{m.group(1)}-{m.group(2)}", "end": f"{m.group(3) or m.group(1)}-{m.group(4)}"}
    m = _TIMED.match(text)
    if m:
        return {"date": m.group(1), "time": m.group(2), **({"until": m.group(3)} if m.group(3) else {})}
    m = _DAY.match(text)
    if m:
        return {"date": m.group(1)}
    return {"date": None, "when": text}


def describe(e: dict) -> str:
    if not e.get("date"):
        skipped = e.get("except") or []
        return e["when"] + (f" (not on {', '.join(skipped)})" if skipped else "")
    if e.get("end"):
        return f"{e['date']} → {e['end']}"
    if e.get("time"):
        return f"{e['date']} {e['time']}" + (f"–{e['until']}" if e.get("until") else "")
    return e["date"]


def _table_when(e: dict) -> str:
    """How the calendar file shows an entry (the drafted style: a range shows only the end's month and day)."""
    if e.get("date") and e.get("end") and e["end"][:4] == e["date"][:4]:
        return f"{e['date']} → {e['end'][5:]}"
    return describe(e)


class Agenda:
    def __init__(self, run):
        self.run = run
        self.path = run.persona.data.get("calendar")

    def entries(self, state) -> list:
        """The calendar's entries, read from the drafted file the first time and kept in the state after that."""
        if state.data.get("calendar") is None:
            if not self.path or not (self.run.workspace / self.path).is_file():
                raise TabError("There is no calendar here.")
            rows = []
            for line in read_text(self.run.workspace / self.path).split("\n"):
                m = _ROW.match(line)
                if m and not set(m.group(1)) <= set("-: ") and m.group(1).lower() != "date":
                    rows.append({"id": f"E{len(rows) + 1}", **parse_when(m.group(1)), "title": m.group(2),
                                 "origin": "calendar"})
            state.data["calendar"] = rows
        return state.data["calendar"]

    def handle(self, argv: list):
        state = self.run.state()
        loaded = state.data.get("calendar") is None
        entries = self.entries(state)
        if loaded:
            state.save()  # the calendar's ids are fixed from the first time it is shown
        if not argv:
            active = [e for e in entries if not e.get("removed_at")]
            lines = [f"Calendar ({self.path}):"] + [f"  {e['id']:<4} {describe(e):<26} {e['title']}" for e in active]
            return "\n".join(lines), {"entries": len(active)}
        cmd, opts = argv[0], self._options(argv[1:])
        if cmd == "add":
            return self._add(state, entries, opts)
        if cmd == "remove":
            return self._remove(state, entries, opts)
        raise TabError("Usage: tab calendar | tab calendar add --date D [--end D] [--time HH:MM] [--until HH:MM] "
                       "--title TEXT | tab calendar remove --id ID [--date D]")

    @staticmethod
    def _options(args) -> dict:
        opts, i = {}, 0
        while i < len(args):
            if not args[i].startswith("--") or i + 1 >= len(args):
                raise TabError(f"Give options as --name value (got {args[i]!r})")
            opts[args[i][2:]] = args[i + 1]
            i += 2
        return opts

    def _add(self, state, entries, opts):
        unknown = set(opts) - {"date", "end", "time", "until", "title"}
        if unknown:
            raise TabError(f"Unknown option --{sorted(unknown)[0]}")
        title = (opts.get("title") or "").strip()
        if not title or len(title) > 200:
            raise TabError("Give a --title (1 to 200 characters)")
        if "date" not in opts:
            raise TabError("Give a --date YYYY-MM-DD")
        entry = {"id": f"E{len(entries) + 1}", "date": self._date(opts["date"], "date"), "title": title,
                 "origin": "agent", "added_at": state.data["now"]}
        if "end" in opts:
            entry["end"] = self._date(opts["end"], "end")
            if entry["end"] < entry["date"]:
                raise TabError("--end can't be before --date")
        for key in ("time", "until"):
            if key in opts:
                if not _TIME.match(opts[key]):
                    raise TabError(f"--{key} must be a time like 18:00")
                entry[key] = opts[key]
        entries.append(entry)
        self._render(state, entries)
        state.save()
        return f"Added {entry['id']}: {describe(entry)}  {title}", {"id": entry["id"], "date": entry["date"],
                                                                     "title": title}

    def _remove(self, state, entries, opts):
        unknown = set(opts) - {"id", "date"}
        if unknown:
            raise TabError(f"Unknown option --{sorted(unknown)[0]}")
        entry = next((e for e in entries if e["id"] == opts.get("id")), None)
        if entry is None or entry.get("removed_at"):
            raise TabError(f"No calendar entry {opts.get('id')!r}. Run: tab calendar")
        if "date" in opts:  # one date of a recurring entry
            if entry.get("date"):
                raise TabError(f"Only a recurring entry can skip a date; {entry['id']} is on {describe(entry)}. "
                               f"To remove it: tab calendar remove --id {entry['id']}")
            day = self._date(opts["date"], "date")
            weekday = dt.date.fromisoformat(day).weekday()
            named = [i for i, name in enumerate(_WEEKDAYS) if re.search(rf"\b{name}s?\b", entry["when"], re.I)]
            if named and weekday not in named:  # "Fridays in Lent": only a Friday can be skipped
                on = " or ".join(f"{_WEEKDAYS[i].capitalize()}s" for i in named)
                raise TabError(f"{entry['id']} is on {on}; {day} is a {_WEEKDAYS[weekday].capitalize()}.")
            if day in (entry.get("except") or []):
                raise TabError(f"{entry['id']} already skips {day}.")
            entry["except"] = sorted((entry.get("except") or []) + [day])
            self._render(state, entries)
            state.save()
            return f"{entry['id']} now skips {day}: {describe(entry)}  {entry['title']}", {"id": entry["id"],
                                                                                           "skipped": day}
        entry["removed_at"] = state.data["now"]
        self._render(state, entries)
        state.save()
        return f"Removed {entry['id']}: {describe(entry)}  {entry['title']}", {"id": entry["id"]}

    @staticmethod
    def _date(text, name) -> str:
        try:
            return dt.date.fromisoformat(text).isoformat()
        except ValueError:
            raise TabError(f"--{name} must be a date like 2026-03-20 (got {text!r})") from None

    def _render(self, state, entries):
        original = read_text(self.run.workspace / self.path).split("\n")
        head = []
        for line in original:
            if _ROW.match(line):
                break
            head.append(line)
        active = [e for e in entries if not e.get("removed_at")]
        active.sort(key=lambda e: (e.get("date") or "9999", e.get("time") or "", int(e["id"][1:])))
        rows = ["| Date | Event |", "| --- | --- |"] + [f"| {_table_when(e)} | {e['title']} |" for e in active]
        deliver(self.run, self.path, "\n".join(head + rows) + "\n", state)
