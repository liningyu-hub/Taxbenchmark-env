"""The simulated clock's calendar helpers: when an item is dated, and what a texts file shows on a given day.

Visibility is by day: an item dated D is visible from the start of day D. Times given as a bare date mean 09:00.
"""
import datetime as dt

# The simulated clock stops here: replies scheduled hours after a later time would fall off the calendar.
LATEST = dt.datetime(2099, 12, 31, 23, 59)
import re

from . import TabError

_EMAIL_DATE = re.compile(r"\A(\d{4})-(\d{2})-(\d{2})_")
_TEXT_STAMP = re.compile(r"\A\*\*[^*\n]+\*\* · (\d{1,2})/(\d{1,2})/(\d{4})")
_BLOCK_GAP = re.compile(r"\n[ \t]*\n")
DAY_START = dt.time(9, 0)


def email_date(name: str):
    """The date a draft email's file name starts with, or None for an undated file."""
    m = _EMAIL_DATE.match(name)
    return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def parse_when(text: str) -> dt.datetime:
    """'2026-03-05' (09:00 that day) or '2026-03-05T14:30'."""
    try:
        if "T" in text:
            return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M")
        return dt.datetime.combine(dt.datetime.strptime(text, "%Y-%m-%d").date(), DAY_START)
    except ValueError:
        raise TabError(f"Give a date as YYYY-MM-DD or YYYY-MM-DDTHH:MM (got {text})") from None


def format_sim(when: dt.datetime) -> str:
    """'Tue, Feb 24, 2026 09:00'."""
    return f"{when:%a}, {when:%b} {when.day}, {when.year} {when:%H:%M}"


def format_email_date(day: dt.date) -> str:
    """The Date line of an email: 'Tue, Feb 24, 2026'."""
    return f"{day:%a}, {day:%b} {day.day}, {day.year}"


def _block_date(block: str):
    m = _TEXT_STAMP.match(block)
    return dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else None


def text_block_dates(md: str) -> list:
    return [d for d in (_block_date(b) for b in _BLOCK_GAP.split(md)) if d is not None]


def render_texts(md: str, today: dt.date) -> str:
    """A texts file as it looks on `today`: message blocks dated later are left out. Unchanged when all show."""
    blocks = _BLOCK_GAP.split(md)
    keep = [b for b in blocks if (_block_date(b) or today) <= today]
    if len(keep) == len(blocks):
        return md
    return "\n\n".join(b.strip("\n") for b in keep).rstrip("\n") + "\n"
