"""Port of test/key_and_cli_test.rb (3 tests). The executables test also covers the plan's Review Focus 2: output
is UTF-8 whatever the console code page (this Windows machine defaults to GBK)."""
import os
import subprocess
import sys

from conftest import needs_sources, require_answer_key
from tab import ROOT, key_check

BIN = ROOT / "bin"
pytestmark = needs_sources


def test_answer_key_matches_the_expert_return_and_the_task(persona):
    require_answer_key()
    report = key_check.run(persona)
    assert report["not_found"] == [], "every key amount appears on the page it cites"
    assert report["in_key_not_in_task"] == []
    assert report["in_task_not_in_key"] == []


def test_headline_lines_are_marked(persona):
    require_answer_key()
    headline = [f"{fid}.{lid}" for fid, entries in persona.answer_key()["forms"].items()
                for lid, spec in entries.items() if spec.get("headline")]
    assert headline == ["f1040.11a", "f1040.15", "f1040.24", "f1040.33", "f1040.37", "f1040.38",
                        "ca540.17", "ca540.19", "ca540.64", "ca540.78", "ca540.111"]


def test_executables_run_as_separate_processes(home):
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    env["TAB_HOME"] = str(home)

    def run(tool, *args, stdin=b""):
        p = subprocess.run([sys.executable, str(BIN / tool), *args], env=env, input=stdin,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return p.returncode, p.stdout.decode("utf-8")

    status, out = run("tab-admin", "init", "S000005")
    assert status == 0, out
    status, out = run("tab", "ls", "Portals")
    assert status == 0, out
    assert "Meridian_Retirement_Services/" in out
    status, out = run("tab", "read", "../manifest.json")
    assert status != 0
    assert "outside the environment" in out
    status, out = run("tab-admin", "log", "--agent")
    assert status == 0
    assert len(out.splitlines()) == 2

    text = "± · — é\n"
    assert run("tab", "write", "Documents/Taxes/2025/n.md", stdin=text.encode("utf-8"))[0] == 0
    status, out = run("tab", "read", "Documents/Taxes/2025/n.md")
    assert status == 0 and out == text + ("" if text.endswith("\n") else "\n")
