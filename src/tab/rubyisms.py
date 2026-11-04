"""Ruby behaviours the MVP's outputs depend on, reproduced in Python.

The Ruby prototype's files (logs, submissions, results, reports) and its printed text must come out byte for byte
the same, so rounding, JSON layout, line splitting, string stripping, regex dialect and directory globbing follow
Ruby here rather than Python's defaults.
"""
import json
import os
import re
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path

_RUBY_SPACE = " \t\n\v\f\r\0"


def ruby_round(x, ndigits=0):
    """Float#round: half away from zero. Integers come back unchanged; ndigits == 0 returns an int."""
    if isinstance(x, int) and not isinstance(x, bool):
        return x
    if x.is_integer():  # already whole (every |x| >= 2**52 is): Ruby returns the float's exact value
        return int(x) if ndigits == 0 else x
    with localcontext() as ctx:
        ctx.prec = 400  # beyond any float's digits; the default 28 raised InvalidOperation for |x| >= 1e28
        q = Decimal(repr(x)).quantize(Decimal(1).scaleb(-ndigits), rounding=ROUND_HALF_UP)
    return int(q) if ndigits == 0 else float(q)


def ruby_integer(value) -> int:
    """Kernel#Integer for the CLI's numeric arguments, with Ruby's error message (an ArgumentError there)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    try:
        return int(str(value).strip(_RUBY_SPACE))
    except ValueError:
        raise ValueError(f"invalid value for Integer(): {json.dumps(str(value))}") from None


def ruby_strip(s: str) -> str:
    """String#strip: ASCII whitespace and NUL only (Python's strip also removes Unicode spaces)."""
    return s.strip(_RUBY_SPACE)


def ruby_lines(s: str) -> list:
    """String#lines: split after each "\\n", keeping it; no other character ends a line."""
    if not s:
        return []
    parts = s.split("\n")
    lines = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def iso8601(dt: datetime = None, ms: bool = False) -> str:
    """Time#utc.iso8601 / iso8601(3): '2026-11-03T23:10:30Z' or '2026-11-03T23:10:30.095Z' (milliseconds truncated)."""
    dt = (dt or datetime.now(timezone.utc)).astimezone(timezone.utc)
    frac = f".{dt.microsecond // 1000:03d}" if ms else ""
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + frac + "Z"


def json_pretty(obj, depth: int = 0) -> str:
    """JSON.pretty_generate as the macOS system Ruby's C generator writes it: two-space indent, and an empty array
    as "[", a blank line, then "]" at the parent's indent (an empty object as "{", newline, "}")."""
    pad = "  " * depth
    inner = "  " * (depth + 1)
    if isinstance(obj, dict):
        if not obj:
            return "{\n" + pad + "}"
        items = [f"{inner}{json.dumps(str(k), ensure_ascii=False)}: {json_pretty(v, depth + 1)}" for k, v in obj.items()]
        return "{\n" + ",\n".join(items) + "\n" + pad + "}"
    if isinstance(obj, list):
        if not obj:
            return "[\n\n" + pad + "]"
        return "[\n" + ",\n".join(inner + json_pretty(v, depth + 1) for v in obj) + "\n" + pad + "]"
    return json.dumps(obj, ensure_ascii=False)


def json_compact(obj) -> str:
    """JSON.generate."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def to_s(value) -> str:
    """Object#to_s as the report prints values: nil -> '', true -> 'true', arrays as Array#inspect."""
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


_ESCAPES = {"z": r"\Z", "Z": r"(?=\n?\Z)", "h": "[0-9a-fA-F]", "H": "[^0-9a-fA-F]"}
_CLASS_ESCAPES = {"h": "0-9a-fA-F"}


def _translate(pattern: str) -> str:
    out = []
    i = 0
    in_class = False
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            nxt = pattern[i + 1]
            table = _CLASS_ESCAPES if in_class else _ESCAPES
            out.append(table.get(nxt, c + nxt))
            i += 2
            continue
        if c == "[" and not in_class:
            in_class = True
        elif c == "]" and in_class:
            in_class = False
        out.append(c)
        i += 1
    return "".join(out)


def ruby_regex(pattern: str, ignorecase: bool = False) -> "re.Pattern":
    """Regexp.new(pattern[, IGNORECASE]) in Ruby's dialect: ^ and $ match at every line, \\z \\Z \\h are translated,
    and \\w \\d \\s \\b are ASCII-only as in Ruby. Raises re.error for patterns Python can't compile."""
    flags = re.MULTILINE | re.ASCII | (re.IGNORECASE if ignorecase else 0)
    return re.compile(_translate(pattern), flags)


def glob_files(root, dotmatch: bool = False) -> list:
    """Dir.glob('**/*', base: root) filtered to regular files, sorted, as '/'-separated relative paths.
    Hidden names (leading '.') are skipped unless dotmatch, and symlinked directories are not descended into."""
    root = Path(root)
    found = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames
                       if (dotmatch or not d.startswith(".")) and not is_dir_link(os.path.join(dirpath, d))]
        for name in filenames:
            if not dotmatch and name.startswith("."):
                continue
            full = Path(dirpath) / name
            if full.is_file():
                found.append(full.relative_to(root).as_posix())
    return sorted(found)


_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT


def is_dir_link(path) -> bool:
    """A symlink, or on Windows a junction or other reparse point (os.walk follows junctions on its own)."""
    if os.path.islink(path):
        return True
    try:
        return bool(getattr(os.lstat(path), "st_file_attributes", 0) & _REPARSE_POINT)
    except OSError:
        return False


def puts(stream, text: str) -> None:
    """IO#puts to a binary stream: UTF-8, with a newline added unless the text already ends with one."""
    data = text if text.endswith("\n") else text + "\n"
    stream.write(data.encode("utf-8"))
