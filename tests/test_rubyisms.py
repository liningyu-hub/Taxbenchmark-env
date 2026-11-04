"""Ruby-compatible helpers: every output the Ruby MVP wrote depends on these behaving as Ruby does."""
import io
import os
from datetime import datetime, timezone

from tab.rubyisms import (glob_files, iso8601, json_compact, json_pretty, puts, ruby_lines, ruby_regex, ruby_round,
                          ruby_strip, to_s)


def test_ruby_round_half_away_from_zero():
    assert ruby_round(1234.5) == 1235 and ruby_round(-0.5) == -1 and ruby_round(2.5) == 3
    assert ruby_round(0.12345, 4) == 0.1235 and ruby_round(59 / 61, 4) == 0.9672
    assert isinstance(ruby_round(2.0), int) and ruby_round(7) == 7


def test_ruby_round_handles_huge_numbers():
    """Final review: Decimal's default 28-digit precision raised InvalidOperation for |x| >= 1e28."""
    assert ruby_round(1e30) == int(1e30) and ruby_round(-1.5e40) == int(-1.5e40)
    assert ruby_round(1.2345678901234568e29, 2) == 1.2345678901234568e29


def test_ruby_strip_only_ascii_whitespace_and_nul():
    assert ruby_strip(" \t\x00a b\r\n\x0b\f") == "a b"
    assert ruby_strip(" x ") == " x "


def test_ruby_lines_splits_only_on_newline():
    assert ruby_lines("a\r\nb\fc\n") == ["a\r\n", "b\fc\n"]
    assert ruby_lines("x\ny") == ["x\n", "y"] and ruby_lines("") == []


def test_json_layouts_match_ruby():
    assert json_compact({"a": [1, "é"], "b": {}}) == '{"a":[1,"é"],"b":{}}'
    # The macOS system Ruby's JSON.pretty_generate (C generator, json 2.x): an empty array is "[", a blank line,
    # then "]" at the parent's indent; seen in the saved verify_001_2.json. Empty objects follow the same
    # generator code with one newline (not present in the saved files; inferred from the generator).
    assert json_pretty({"a": [], "b": {"c": 1.0, "d": ["x"]}}) == \
        '{\n  "a": [\n\n  ],\n  "b": {\n    "c": 1.0,\n    "d": [\n      "x"\n    ]\n  }\n}'
    assert json_pretty({"e": {}}) == '{\n  "e": {\n  }\n}'
    assert json_pretty([]) == "[\n\n]" and json_pretty("é") == '"é"'


def test_iso8601_formats():
    t = datetime(2026, 11, 3, 23, 10, 30, 95000, tzinfo=timezone.utc)
    assert iso8601(t) == "2026-11-03T23:10:30Z"
    assert iso8601(t, ms=True) == "2026-11-03T23:10:30.095Z"


def test_to_s_matches_ruby():
    assert [to_s(None), to_s(True), to_s(False), to_s(1.0), to_s(12), to_s("x")] == ["", "true", "false", "1.0", "12", "x"]
    assert to_s(["a", "b"]) == '["a", "b"]' and to_s([]) == "[]"


def test_ruby_regex_is_line_mode():
    assert ruby_regex(r"^b$").search("a\nb\nc")
    assert ruby_regex(r"x\z").search("ax") and not ruby_regex(r"x\z").search("x\n")
    assert ruby_regex(r"\h+").fullmatch("0aF9")
    assert ruby_regex("lakeshore", ignorecase=True).search("LAKESHORE")


def test_glob_files_skips_dotfiles_unless_dotmatch(tmp_path):
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "x.md").write_text("x")
    (tmp_path / "a.md").write_text("a")
    (tmp_path / ".DS_Store").write_text("d")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "y.md").write_text("y")
    assert glob_files(tmp_path) == ["a.md", "b/x.md"]
    assert glob_files(tmp_path, dotmatch=True) == [".DS_Store", ".hidden/y.md", "a.md", "b/x.md"]


def test_glob_files_does_not_descend_into_symlinked_dirs(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("s")
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.md").write_text("a")
    make_dir_link(outside, root / "link")
    assert glob_files(root) == ["a.md"]


def make_dir_link(target, link):
    """A symlink to a directory, or on Windows without symlink rights, a directory junction (no rights needed)."""
    import pytest
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("cannot create a symlink here")
        import _winapi
        _winapi.CreateJunction(str(target), str(link))


def test_ruby_integer_accepts_ints_and_digit_strings_with_rubys_error():
    from tab.rubyisms import ruby_integer
    import pytest
    assert ruby_integer(3) == 3 and ruby_integer("12") == 12 and ruby_integer(" 7 ") == 7
    with pytest.raises(ValueError) as e:
        ruby_integer("x")
    assert str(e.value) == 'invalid value for Integer(): "x"'


def test_puts_adds_newline_unless_present():
    out = io.BytesIO()
    puts(out, "a")
    puts(out, "b\n")
    puts(out, "± ·")
    assert out.getvalue() == "a\nb\n± ·\n".encode("utf-8")
