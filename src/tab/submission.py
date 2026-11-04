"""Parses and normalizes a submitted return:
  {"persona_id": "...", "tax_year": 2025, "forms": {"f1040": {"11a": 12345, ...}, ...}, "notes": "..."}
Form and line ids are lower-cased ("Line 11a" -> "11a"); "$1,234", "(1,234)" and "-1234" become numbers;
"true"/"x"/"yes" become true. Unknown forms or lines are kept but reported, and never scored.
"""
import json
import re

from . import TabError
from .rubyisms import ruby_round, ruby_strip, to_s

NUMBER = re.compile(r"\A(\()?\s*(-)?\s*\$?\s*(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*(\))?\Z", re.ASCII)
TRUE_WORDS = ("true", "yes", "x", "checked")
FALSE_WORDS = ("false", "no", "unchecked")


def _inspect(value) -> str:
    """String#inspect for the ids quoted in messages."""
    return json.dumps(str(value), ensure_ascii=False)


def _reject_constant(name):
    raise ValueError(f"unexpected token '{name}'")


def parse(text, persona) -> dict:
    try:
        data = json.loads(to_s(text), parse_constant=_reject_constant)
    except ValueError as e:
        first = ruby_strip(str(e).split("\n")[0])[:200]
        raise TabError(f"Submission is not valid JSON: {first}") from None
    if not isinstance(data, dict):
        raise TabError("Submission must be a JSON object")
    if to_s(data.get("persona_id")) != persona.id:
        raise TabError(f"persona_id must be {_inspect(persona.id)}")
    if to_s(data.get("tax_year")) != to_s(persona.tax_year):
        raise TabError(f"tax_year must be {persona.tax_year}")

    forms = data.get("forms")
    if not isinstance(forms, dict) or not forms:
        raise TabError('forms must be a non-empty object of {"form id": {"line": value}}')

    spec = persona.output_spec
    warnings = []
    out = {}
    for form_id, lines in forms.items():
        fid = ruby_strip(to_s(form_id)).lower()
        if not isinstance(lines, dict):
            raise TabError(f'forms.{form_id} must be an object of {{"line": value}}')
        if fid not in spec:
            warnings.append(f"Unknown form {_inspect(form_id)} (kept, not scored)")
        out.setdefault(fid, {})
        for line, value in lines.items():
            lid = normalize_line(line)
            if fid in spec and lid not in spec[fid]["lines"]:
                warnings.append(f"{fid} line {_inspect(line)} is not on the list (kept, not scored)")
            out[fid][lid] = normalize_value(value, f"{fid}.{lid}")

    for fid, form in spec.items():
        if form.get("optional"):
            continue
        missing = [lid for lid in form["lines"] if out.get(fid, {}).get(lid) is None]
        if missing:
            warnings.append(f"{fid}: {len(missing)} listed line(s) not given: {', '.join(missing)}")

    return {"submission": {"persona_id": persona.id, "tax_year": persona.tax_year, "forms": out,
                           "notes": to_s(data.get("notes"))},
            "warnings": warnings,
            "line_count": sum(len(v) for v in out.values())}


def normalize_line(line) -> str:
    return re.sub(r"\Aline\s*", "", ruby_strip(to_s(line)).lower(), count=1, flags=re.ASCII)


def normalize_value(value, where: str):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return ruby_round(value) if value == ruby_round(value) else ruby_round(value, 2)
    if isinstance(value, str):
        return normalize_string(value)
    raise TabError(f"{where} must be a number, true/false or text")


def normalize_string(value: str):
    s = ruby_strip(value)
    if not s:
        return None
    m = NUMBER.match(s)
    if m:
        number = float(m.group(3).replace(",", "") + (m.group(4) or ""))
        if m.group(2) or (m.group(1) and m.group(5)):
            number = -number
        return ruby_round(number) if number == ruby_round(number) else ruby_round(number, 2)
    if s.lower() in TRUE_WORDS:
        return True
    if s.lower() in FALSE_WORDS:
        return False
    return s.lower()
