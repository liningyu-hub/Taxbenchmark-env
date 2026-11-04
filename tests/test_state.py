"""Run state: the action log, submissions, results and integrity.

The first five tests call Run directly (Task 4). The five ported from test/state_test.rb drive the agent CLI and
follow below them (Task 6).
"""
import json
import os
import re
import subprocess
import sys

import pytest

from conftest import needs_sources
from tab import ROOT
from tab.run import Run

pytestmark = needs_sources


def test_log_entries_have_the_ruby_shape(initialized_env):
    e = initialized_env.log(actor="agent", tool="ls", detail={"entries": 5})
    assert list(e) == ["seq", "ts", "actor", "tool", "args", "ok", "detail"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", e["ts"])
    failed = initialized_env.log(actor="agent", tool="read", args=["x"], ok=False, error="No such file or folder: x")
    assert list(failed) == ["seq", "ts", "actor", "tool", "args", "ok", "error"]
    last = (initialized_env.path("actions.jsonl").read_bytes().split(b"\n"))[-2]
    assert last.startswith(b'{"seq":') and b", " not in last, "one compact JSON object per line"


def test_concurrent_logging_keeps_seq_unique(initialized_env, home):
    script = ("import sys; sys.path.insert(0, sys.argv[1]); from tab.run import Run; r = Run.current()\n"
              "for _ in range(100): r.log(actor='agent', tool='status')")
    env = dict(os.environ, TAB_HOME=str(home))
    procs = [subprocess.Popen([sys.executable, "-c", script, str(ROOT / "src")], env=env) for _ in range(2)]
    assert [p.wait(timeout=120) for p in procs] == [0, 0]
    seqs = [a["seq"] for a in Run.current().actions()]
    assert len(seqs) == 201 and seqs == list(range(1, 202))


@pytest.mark.skipif(os.name != "nt", reason="the sidecar lock loop exists only on Windows")
def test_windows_lock_gives_up_with_a_clear_error(initialized_env, monkeypatch):
    """Final review: the lock loop retried forever; now contention gets a deadline and other errors fail at once."""
    import errno
    import msvcrt
    from tab import TabError, run as run_mod

    def busy(fd, mode, n):
        raise OSError(errno.EACCES, "locked")
    monkeypatch.setattr(msvcrt, "locking", busy)
    monkeypatch.setattr(run_mod, "LOCK_TIMEOUT", 0.3)
    with pytest.raises(TabError, match="Could not lock the action log"):
        initialized_env.log(actor="agent", tool="status")

    def broken(fd, mode, n):
        raise OSError(errno.EBADF, "bad file descriptor")
    monkeypatch.setattr(msvcrt, "locking", broken)
    monkeypatch.setattr(run_mod, "LOCK_TIMEOUT", 60)
    with pytest.raises(TabError, match="Could not lock the action log"):
        initialized_env.log(actor="agent", tool="status")


def test_save_submission_numbers_upward_and_reads_back(initialized_env):
    run = initialized_env
    assert run.submission() is None
    assert run.save_submission({"forms": {}}, "raw 1") == 1
    assert run.save_submission({"forms": {"f1040": {"15": 1}}}, "raw 2") == 2
    assert run.submission_ids() == [1, 2]
    assert run.submission()["id"] == 2 and run.submission(1)["raw"] == "raw 1"
    assert run.path("submissions/001.json").read_bytes().startswith(b'{\n  "id": 1,\n  "submitted_at": "')


def test_save_result_keeps_earlier_results(initialized_env):
    run = initialized_env
    first = run.save_result({"submission_id": 1}, "# one\n")
    second = run.save_result({"submission_id": 1}, "# two\n")
    assert first.endswith("results/verify_001") and second.endswith("results/verify_001_2")
    assert sorted(os.listdir(run.path("results"))) == ["verify_001.json", "verify_001.md",
                                                       "verify_001_2.json", "verify_001_2.md"]
    assert json.loads(run.path("results/verify_001_2.json").read_text("utf-8")) == {"submission_id": 1}


def test_integrity_reports_modified_missing_and_added(initialized_env):
    run = initialized_env
    assert run.integrity()["clean"]
    (run.workspace / "Documents/Taxes/2025/notes.md").write_bytes(b"ok")
    assert run.integrity()["clean"], "saving in the work folder is allowed"
    (run.workspace / "Portals/IRS_Online_Account/Account_Summary.md").write_bytes(b"edited")
    (run.workspace / "Texts/Texts_2025_sample.md").unlink()
    (run.workspace / "Email/extra.md").write_bytes(b"new")
    i = run.integrity()
    assert not i["clean"]
    assert i["modified"] == ["Portals/IRS_Online_Account/Account_Summary.md"]
    assert i["missing"] == ["Texts/Texts_2025_sample.md"]
    assert i["added_elsewhere"] == ["Email/extra.md"]
    assert i["added_in_work_folder"] == ["Documents/Taxes/2025/notes.md"]


# ---- Ported from test/state_test.rb (5 tests) ----

from conftest import agent, key_submission, key_submission_json, key_value, submit_json  # noqa: E402


def test_every_agent_call_is_logged_in_order_with_errors(initialized_env):
    agent("ls")
    agent("read", "no/such/file.md")
    agent("ask", "Were you married?")
    actions = [a for a in Run.current().actions() if a["actor"] == "agent"]
    assert [a["tool"] for a in actions] == ["ls", "read", "ask"]
    assert [a["ok"] for a in actions] == [True, False, True]
    assert re.search(r"No such file", actions[1]["error"])
    seqs = [a["seq"] for a in Run.current().actions()]
    assert seqs == list(range(1, len(seqs) + 1))


def test_submissions_persist_and_number_upwards(initialized_env):
    status, out, _ = submit_json(key_submission())
    assert status == 0
    assert "Submission #1 saved" in out
    submit_json(key_submission({"f1040.15": 1}))
    fresh = Run(Run.current_dir())  # a new process would see the same
    assert fresh.submission_ids() == [1, 2]
    assert fresh.submission()["submission"]["forms"]["f1040"]["15"] == 1
    assert fresh.submission(1)["submission"]["forms"]["f1040"]["15"] == key_value("f1040.15")


def test_submit_from_a_workspace_file(initialized_env):
    agent("write", "Documents/Taxes/2025/return.json", stdin=key_submission_json())
    status, out, _ = agent("submit", "Documents/Taxes/2025/return.json")
    assert status == 0
    assert "Warnings" not in out


def test_bad_submissions_are_rejected_and_logged(initialized_env):
    base = key_submission()
    cases = [("not json", r"not valid JSON"),
             (json.dumps(dict(base, persona_id="S000007")), r"persona_id"),
             (json.dumps(dict(base, tax_year=2024)), r"tax_year"),
             (json.dumps(dict(base, forms={})), r"forms must be"),
             (json.dumps(dict(base, forms={"f1040": {"15": [1]}})), r"must be a number")]
    for text, message in cases:
        status, _, err = agent("submit", "-", stdin=text)
        assert status == 1
        assert re.search(message, err)
    assert Run.current().submission_ids() == []
    assert sum(1 for a in Run.current().actions() if a["tool"] == "submit" and not a["ok"]) == 5


def test_integrity_detects_tampering_outside_the_work_folder(initialized_env):
    assert Run.current().integrity()["clean"]
    agent("write", "Documents/Taxes/2025/notes.md", stdin="ok")
    assert Run.current().integrity()["clean"], "saving in the work folder is allowed"
    (initialized_env.workspace / "Portals/IRS_Online_Account/Account_Summary.md").write_bytes(b"edited")
    (initialized_env.workspace / "Email/extra.md").write_bytes(b"new")
    integrity = Run.current().integrity()
    assert not integrity["clean"]
    assert integrity["modified"] == ["Portals/IRS_Online_Account/Account_Summary.md"]
    assert integrity["added_elsewhere"] == ["Email/extra.md"]
