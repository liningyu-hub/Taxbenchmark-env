"""Submission parsing (Task 5): values normalize the way the Ruby parser did, errors keep Ruby's wording."""
import json

import pytest

from tab import TabError
from tab.submission import normalize_line, normalize_string, normalize_value, parse


def test_number_strings_normalize_like_ruby():
    cases = {"$1,234": 1234, "(1,234)": -1234, "-12.50": -12.5, "1234.5": 1234.5, "x": True, "No": False,
             "Single": "single", "  ": None, "$ 1,234,567": 1234567, "(12)": -12, "12)": 12}
    for s, v in cases.items():
        assert normalize_string(s) == v, s
        assert type(normalize_string(s)) is type(v), s


def test_values_normalize_like_ruby():
    assert normalize_value(1234.0, "f1040.15") == 1234 and isinstance(normalize_value(1234.0, "w"), int)
    assert normalize_value(12.3456, "w") == 12.35
    assert normalize_value(True, "w") is True and normalize_value(None, "w") is None and normalize_value(7, "w") == 7
    with pytest.raises(TabError, match=r"f1040\.15 must be a number, true/false or text"):
        normalize_value([1], "f1040.15")
    assert normalize_line(" Line 11a ") == "11a" and normalize_line("LINE11A") == "11a"


def test_huge_amounts_normalize_like_ruby():
    """Ruby: "123456789012345678901234567890".to_f.round == 123456789012345677877719597056."""
    huge = normalize_string("123456789012345678901234567890")
    assert huge == 123456789012345677877719597056 and type(huge) is int
    assert normalize_value(1e30, "f1040.15") == int(1e30) and type(normalize_value(1e30, "w")) is int


def test_submission_with_crlf_parses(persona):
    text = '{"persona_id":"S000005",\r\n"tax_year":2025,\r\n"forms":{"f1040":{"1a":1}}}\r\n'
    assert parse(text, persona)["line_count"] == 1


@pytest.mark.parametrize("text, message", [
    ("not json", r"^Submission is not valid JSON: "),
    ("[1]", r"^Submission must be a JSON object$"),
    ('{"persona_id": "S000007", "tax_year": 2025, "forms": {"f1040": {}}}', r'^persona_id must be "S000005"$'),
    ('{"persona_id": "S000005", "tax_year": 2024, "forms": {"f1040": {}}}', r"^tax_year must be 2025$"),
    ('{"persona_id": "S000005", "tax_year": 2025, "forms": {}}',
     r'^forms must be a non-empty object of \{"form id": \{"line": value\}\}$'),
    ('{"persona_id": "S000005", "tax_year": 2025, "forms": {"f1040": [1]}}',
     r'^forms\.f1040 must be an object of \{"line": value\}$'),
    ('{"persona_id": "S000005", "tax_year": 2025, "forms": {"f1040": {"15": NaN}}}', r"^Submission is not valid JSON"),
])
def test_bad_submissions_keep_rubys_messages(persona, text, message):
    with pytest.raises(TabError, match=message):
        parse(text, persona)


def test_warnings_and_normalized_record(persona):
    text = json.dumps({"persona_id": "S000005", "tax_year": "2025", "notes": None,
                       "forms": {" F1040 ": {"Line 1a": "$100", "99": 5}, "f9999": {"1": 1}}})
    out = parse(text, persona)
    assert out["submission"] == {"persona_id": "S000005", "tax_year": 2025, "notes": "",
                                 "forms": {"f1040": {"1a": 100, "99": 5}, "f9999": {"1": 1}}}
    assert out["line_count"] == 3
    w = out["warnings"]
    assert 'f1040 line "99" is not on the list (kept, not scored)' in w
    assert 'Unknown form "f9999" (kept, not scored)' in w
    assert any(x.startswith("f1040: 24 listed line(s) not given: filing_status, 1z") for x in w)
    assert not any(x.startswith("f2210:") for x in w), "Form 2210 is optional"
