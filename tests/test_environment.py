"""Port of test/environment_test.rb (6 tests) plus the CRLF leak-scan case from the plan's Review Focus."""
import os
import shutil

import pytest

from conftest import PERSONA, dollars, key_value, needs_sources, require_answer_key
from tab import TabError, environment, manifest
from tab.renderer import Renderer
from tab.run import Run
from tab.rubyisms import glob_files

pytestmark = needs_sources


def test_init_builds_workspace_with_tax_documents_in_their_portals(home, persona):
    run = environment.init(PERSONA)
    ws = run.workspace
    assert sorted(os.listdir(ws)) == ["Calendar", "Documents", "Email", "Portals", "Texts"]
    assert (ws / "Documents/Taxes/2025").is_dir(), "writable work folder exists"
    for doc in persona.tax_documents:
        assert (ws / doc["place"]).is_file(), f"{doc['doc']} placed at {doc['place']}"
        assert (run.cache_dir / f"{doc['place']}.txt").is_file(), f"{doc['doc']} text cached"
    # Ruby asserted 26: the drafts have since grown to 39 emails (plan, Decision 1).
    assert len(list((ws / "Email").glob("*.md"))) == 39
    assert run.info["fingerprint"] == manifest.fingerprint(manifest.scan(ws))
    assert run.actions()[0]["tool"] == "init"


def test_hidden_material_never_reaches_the_workspace(initialized_env):
    run = initialized_env
    files = glob_files(run.workspace)
    import re
    bad = [f for f in files if "00_TaxBench_Source" in f
           or re.search(r"README\.md|guide|answer|expert|Change_Log", f.rsplit("/", 1)[-1], re.I)]
    assert not bad, f"no source, guide or key files: {bad}"
    assert Renderer.leak_scan(run.workspace) == []
    all_text = "\n".join((run.workspace / f).read_text("utf-8") for f in files if f.endswith(".md"))
    assert not re.search(r"phishing|trap", "\n".join(files), re.I)
    # The amounts owed must not be stated anywhere the agent can read (the drafts' reviewer footers had them).
    require_answer_key()
    assert dollars(key_value("f1040.37")) not in all_text
    assert dollars(key_value("ca540.111")) not in all_text


def test_markdown_cleaning_removes_reviewer_metadata():
    draft = ("> SAMPLE persona draft for TaxAgentBench. Fictional.\n\n# Title\n\n_Item D3 · Email_\n\n"
             "Body (= TaxBench DOC5) text.\n\n---\n_Supports: Trap: phishing_\n")
    assert Renderer.clean_markdown(draft) == "# Title\n\nBody text.\n"


def test_framing_markers_are_stripped_and_scanned(tmp_path):
    """Day 1 review: '(simulated)', '(fictional)' and 'the agent's working folder' tell the agent it is in a test."""
    draft = ("**From:** Park Alerts (simulated) <alerts@parks.example.gov>  \n"
             "| Granite Wash (fictional) | open |\n- `2025/` — empty (the agent's working folder)\n")
    clean = Renderer.clean_markdown(draft)
    assert "(simulated)" not in clean and "(fictional)" not in clean and "agent" not in clean
    assert "**From:** Park Alerts <alerts@parks.example.gov>" in clean and "| Granite Wash | open |" in clean
    (tmp_path / "a.md").write_text("Rec Reservations (Simulated) page\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("_Release: After the agent sends it_\n", encoding="utf-8")
    rules = {(h["file"], h["rule"]) for h in Renderer.leak_scan(tmp_path)}
    assert rules == {("a.md", "test framing"), ("b.md", "event release line")}


def test_second_init_needs_force(initialized_env):
    with pytest.raises(TabError):
        environment.init(PERSONA)


def test_missing_source_folder_gives_a_clear_error(home, monkeypatch):
    monkeypatch.setenv("TAB_DAY1_DIR", str(home / "nowhere"))
    with pytest.raises(TabError, match="TAB_DAY1_DIR"):
        environment.init(PERSONA)
    assert not Run.exists(), "nothing half-built is left behind"


def test_unknown_persona(home):
    with pytest.raises(TabError):
        environment.init("S999999")


def test_crlf_drafts_fail_the_leak_scan(home, tmp_path, monkeypatch, persona):
    """Drafts checked out with CRLF line endings must make init fail loudly, as Ruby would, never leak."""
    day1 = tmp_path / "day1"
    shutil.copytree(persona.source_dir, day1 / persona.data["source_folder"])
    for md in (day1 / persona.data["source_folder"]).rglob("*.md"):
        md.write_bytes(md.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    monkeypatch.setenv("TAB_DAY1_DIR", str(day1))
    with pytest.raises(TabError, match="Leak scan failed"):
        environment.init(PERSONA)
    assert not Run.exists()
