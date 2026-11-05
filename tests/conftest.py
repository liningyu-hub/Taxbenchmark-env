"""Shared fixtures and helpers (port of test/test_helper.rb).

Tests that need the Day 1 drafts or the answer key skip themselves when those are missing, as the Ruby tests did.
Two versions of the drafts sit next to this project:
  DRAFTS_DIR     the 2026-11-05 drafts, which the tests use when TAB_DAY1_DIR is not set
  DRAFTS_V1_DIR  the first drafts (2026-11-03), used only by the Ruby replay (fixture drafts_v1, or TAB_DAY1_V1_DIR)
"""
import os
import shutil
from pathlib import Path

import pytest

from tab import ROOT

DRAFTS_DIR = ROOT.parent / "Day 1 Environment-20261105T205710Z-1-001" / "Day 1 Environment"
DRAFTS_V1_DIR = ROOT.parent / "Day 1 Environment"
DAY1_FROM_ENV = "TAB_DAY1_DIR" in os.environ
if not DAY1_FROM_ENV:
    for candidate in (DRAFTS_DIR, DRAFTS_V1_DIR):
        if candidate.is_dir():
            os.environ["TAB_DAY1_DIR"] = str(candidate)
            break

PERSONA = "S000005"
# The last run of the retired Ruby version (2026-11-03), kept as golden data. Git-ignored: it holds the answers.
GOLDEN_RUN_DEFAULT = ROOT / "tests" / "golden" / "saved_run"


def sources_available() -> bool:
    from tab import config, pdf_text
    return (config.day1_dir() / "S000005_Thomas_Gonzalez").is_dir() and pdf_text.available()


needs_sources = pytest.mark.skipif(not sources_available(),
                                   reason="needs the Day 1 Environment folder and PyMuPDF")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Each test gets its own TAB_HOME."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("TAB_HOME", str(h))
    return h


@pytest.fixture
def drafts_v1(monkeypatch):
    """The first drafts (2026-11-03), for tests pinned to the Ruby run made from them."""
    path = Path(os.environ.get("TAB_DAY1_V1_DIR") or DRAFTS_V1_DIR)
    if not (path / "S000005_Thomas_Gonzalez").is_dir():
        pytest.skip(f"the first drafts are not at {path} (set TAB_DAY1_V1_DIR)")
    monkeypatch.setenv("TAB_DAY1_DIR", str(path))
    return path


@pytest.fixture(scope="session")
def template_home(tmp_path_factory):
    """Building a workspace reads 8 PDFs, so one initialized run is built per test session and copied per test."""
    if not sources_available():
        pytest.skip("needs the Day 1 Environment folder and PyMuPDF")
    from tab import environment
    h = tmp_path_factory.mktemp("tab-template")
    old = os.environ.get("TAB_HOME")
    os.environ["TAB_HOME"] = str(h)
    try:
        environment.init(PERSONA)
    finally:
        if old is None:
            os.environ.pop("TAB_HOME", None)
        else:
            os.environ["TAB_HOME"] = old
    return h


@pytest.fixture
def initialized_env(home, template_home):
    """A fresh copy of an initialized environment in this test's TAB_HOME."""
    from tab.run import Run
    shutil.copytree(template_home / "current", home / "current")
    return Run.current()


@pytest.fixture
def persona():
    from tab.persona import Persona
    return Persona.load(PERSONA)


def require_answer_key():
    """Public copies of the repository leave the answer key out; tests that need it skip."""
    from tab.persona import Persona
    if not Persona.load(PERSONA).has_answer_key():
        pytest.skip("answer key not present (personas/S000005/hidden/answer_key.json)")


def key_value(path: str):
    """A key value, e.g. key_value('f1040.15'). Tests read answers from the key rather than quoting them."""
    from tab.persona import Persona
    require_answer_key()
    fid, lid = path.split(".", 1)
    return Persona.load(PERSONA).answer_key()["forms"][fid][lid]["value"]


def dollars(amount) -> str:
    """1234567 -> '$1,234,567'. Like the Ruby helper, it keeps only the digits, so a minus sign is dropped."""
    return f"${abs(int(amount)):,}"


def agent(*args, stdin=""):
    """Runs an agent command in-process; returns (exit status, stdout, stderr)."""
    import io
    from tab import cli
    out, err = io.BytesIO(), io.BytesIO()
    data = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
    status = cli.agent(list(args), stdin=io.BytesIO(data), out=out, err=err)
    return status, out.getvalue().decode("utf-8"), err.getvalue().decode("utf-8")


run_agent = agent


def admin(*args):
    """Runs an operator command in-process; returns (exit status, stdout, stderr)."""
    import io
    from tab import cli
    out, err = io.BytesIO(), io.BytesIO()
    status = cli.admin(list(args), out=out, err=err)
    return status, out.getvalue().decode("utf-8"), err.getvalue().decode("utf-8")


def key_submission(overrides=None, persona_id=PERSONA) -> dict:
    """Test fixture only: a submission holding the answer key's values (to check that the verifier accepts it)."""
    from tab.persona import Persona
    if not Persona.load(persona_id).has_answer_key():
        pytest.skip(f"answer key not present for {persona_id}")
    forms = {fid: {lid: spec["value"] for lid, spec in entries.items()}
             for fid, entries in Persona.load(persona_id).answer_key()["forms"].items()}
    for path, value in (overrides or {}).items():
        fid, lid = path.split(".", 1)
        if value is None:
            forms[fid].pop(lid, None)
        else:
            forms[fid][lid] = value
    return {"persona_id": persona_id, "tax_year": 2025, "forms": forms}


def key_submission_json(overrides=None, persona_id=PERSONA) -> str:
    from tab.rubyisms import json_compact
    return json_compact(key_submission(overrides, persona_id))


def submit_json(d: dict):
    from tab.rubyisms import json_compact
    return agent("submit", "-", stdin=json_compact(d))


@pytest.fixture
def saved_run(tmp_path):
    """A copy of the saved Ruby run (tests/golden/saved_run), so nothing touches the original."""
    src = Path(os.environ.get("TAB_GOLDEN_RUN", GOLDEN_RUN_DEFAULT))
    if not (src / "run.json").is_file():
        pytest.skip(f"saved Ruby run not found at {src} (set TAB_GOLDEN_RUN)")
    dst = tmp_path / "saved_run"
    shutil.copytree(src, dst)
    return dst
