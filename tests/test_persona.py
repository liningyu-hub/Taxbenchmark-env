"""Config, persona, manifest and PDF text: the pieces every other module builds on."""
import hashlib
import json

import pytest

from tab import TabError, config, manifest, pdf_text
from tab.persona import Persona


def test_saved_fingerprint_recomputes(saved_run):
    m = json.loads((saved_run / "manifest.json").read_text("utf-8"))
    assert manifest.fingerprint(m["files"]) == m["fingerprint"]
    assert json.loads((saved_run / "run.json").read_text("utf-8"))["fingerprint"] == m["fingerprint"]


def test_manifest_scan_includes_dotfiles(tmp_path):
    (tmp_path / ".x").write_bytes(b"1")
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "y.md").write_bytes(b"2")
    files = manifest.scan(tmp_path)
    assert list(files) == [".x", "d/y.md"]
    assert files["d/y.md"] == hashlib.sha256(b"2").hexdigest()


def test_task_text_reproduces_the_erb_loop():
    text = Persona.load("S000005").task_text()
    assert text.startswith("# Task: prepare Thomas Gonzalez's 2025 tax returns\n")
    assert "Today is 2026-03-20." in text
    assert "### `f1040`: Form 1040\n\n- `filing_status`: " in text
    assert '"persona_id": "S000005",' in text and "only under Documents/Taxes/2025/" in text
    assert text.endswith("\n\n") and "<%" not in text and "{{" not in text
    spec = Persona.load("S000005").output_spec
    assert text.count("\n### `") == len(spec)
    assert sum(1 for line in text.split("\n") if line.startswith("- `")) == sum(len(f["lines"]) for f in spec.values())


def test_unknown_persona_lists_the_available_ones():
    with pytest.raises(TabError, match=r'Unknown persona "S999999"\. Available: S000005'):
        Persona.load("S999999")


def test_config_defaults_and_overrides(monkeypatch, tmp_path):
    monkeypatch.delenv("TAB_HOME", raising=False)
    from tab import ROOT
    assert config.home() == ROOT / "runtime"
    monkeypatch.setenv("TAB_HOME", str(tmp_path))
    assert config.home() == tmp_path
    monkeypatch.delenv("TAB_DAY1_DIR", raising=False)
    assert config.day1_dir().name == "Day 1 Environment" and config.day1_dir().parent.name == "Downloads"


def test_pdf_text_reads_every_page():
    p = Persona.load("S000005")
    doc = next(d for d in p.tax_documents if d["doc"] == "DOC5")
    src = p.source_dir / doc["source"]
    if not src.is_file():
        pytest.skip("Day 1 drafts not available")
    pages = pdf_text.extract(src)
    assert len(pages) == 4 and "Northfield Investments" in "\n".join(pages)
    with pytest.raises(TabError, match="No such PDF"):
        pdf_text.extract(src.with_name("missing.pdf"))


def test_without_private_data_the_persona_still_loads_and_answers(tmp_path, monkeypatch):
    """A public copy has no hidden/ folder: tasks load without evaluation data and `tab ask` gives the default."""
    import json
    import shutil
    from tab import config
    from tab.persona import NO_FACTS_ANSWER, Persona
    from tab.sim_user import SimUser
    src = config.personas_dir() / "S000005"
    shutil.copytree(src, tmp_path / "S000005", ignore=shutil.ignore_patterns("hidden"))
    monkeypatch.setattr(config, "personas_dir", lambda: tmp_path)
    persona = Persona.load("S000005")
    assert not persona.has_evaluation() and persona.traps == []
    assert set(persona.task("T1")) == {"id", "kind", "start", "template"}
    assert SimUser(persona.simulated_user()).answer("When were you born?") == (NO_FACTS_ANSWER, [])
    public = json.loads((src / "persona.json").read_text("utf-8"))
    assert persona.task("T1")["start"] == public["tasks"]["T1"]["start"]
