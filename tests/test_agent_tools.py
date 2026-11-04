"""Agent tools, driven through the agent CLI (ported from test/agent_tools_test.rb, plus later additions). The
simulated user's own tests are with the hidden evaluation data (tests/hidden/test_simulated_user.py)."""
# ---- Ported from test/agent_tools_test.rb (11 tests) ----

import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402

import pytest  # noqa: E402

from conftest import agent, dollars, needs_sources, require_answer_key  # noqa: E402
from test_rubyisms import make_dir_link  # noqa: E402


@pytest.fixture
def run(initialized_env):
    return initialized_env


@needs_sources
def test_task_lists_lines_but_no_answers(run, persona):
    status, out, _ = agent("task")
    assert status == 0
    assert "Thomas Gonzalez" in out and "`11a`" in out
    require_answer_key()
    headline = [s for e in persona.answer_key()["forms"].values() for s in e.values() if s.get("headline")]
    for value in [s["value"] for s in headline if isinstance(s["value"], int) and not isinstance(s["value"], bool)
                  and s["value"] >= 100]:
        assert str(value) not in out and dollars(value) not in out


@needs_sources
def test_ls_and_tree(run):
    status, out, _ = agent("ls")
    assert status == 0
    assert out.split("\n")[:-1] == ["Calendar/", "Documents/", "Email/", "Portals/", "Texts/"]
    _, tree, _ = agent("tree")
    assert "Portals/Northfield_Investments/Documents/Tax_Center/2025_Form_1099-B.pdf" in tree


@needs_sources
def test_read_markdown_and_pdf(run):
    _, out, _ = agent("read", "Portals/IRS_Online_Account/Account_Summary.md")
    assert "$50,700" in out
    status, pdf, _ = agent("read", "Portals/Northfield_Investments/Documents/Tax_Center/2025_Form_1099-B.pdf")
    assert status == 0
    assert "Northfield Investments" in pdf and "Granite Design Co." in pdf
    _, page, _ = agent("read", "Portals/Marlowe_Design_Payroll/Pay/Tax_Statements/2025_Form_W-2.pdf", "--page", "1")
    assert "--- page 1 of" in page
    first = next(a for a in run.actions() if a["tool"] == "read" and (a.get("detail") or {}).get("doc"))
    assert first["detail"]["doc"] == "DOC5"


@needs_sources
def test_search_covers_markdown_and_pdfs(run):
    _, out, _ = agent("search", "Lakeshore")
    assert re.search(r"Email/.+Lakeshore|Portals/Lakeshore_Loan_Servicing/2025_Payment_History\.md", out)
    assert re.search(r"2025_Form_1098-E\.pdf:p1:", out)
    _, none, _ = agent("search", "zzz-no-such-text")
    assert none == "No matches.\n"


@needs_sources
def test_inbox_lists_all_messages(run):
    _, out, _ = agent("inbox")
    assert out.startswith("39 messages"), "Ruby asserted 26; the drafts have since grown to 39 (plan, Decision 1)"
    assert "Action required: claim your tax refund" in out


@needs_sources
def test_paths_cannot_leave_the_workspace(run, tmp_path):
    paths = ["../run.json", "../../hidden", "/etc/passwd", "~/x", "Portals/../../manifest.json",
             # Windows forms, refused the same way on every OS:
             "..\\..\\run.json", "C:\\Windows\\win.ini", "\\\\server\\share\\x", "Portals\\..\\..\\manifest.json"]
    for path in paths:
        status, _, err = agent("read", path)
        assert status == 1, path
        assert re.search(r"outside the environment|inside the environment", err), (path, err)
    target = "/etc" if os.name != "nt" else str(tmp_path)
    make_dir_link(target, run.workspace / "Documents" / "link")
    status, _, err = agent("ls", "Documents/link")
    assert status == 1
    assert "outside the environment" in err


@needs_sources
def test_write_only_in_the_work_folder(run):
    status, _, err = agent("write", "Email/new.md", stdin="x")
    assert status == 1
    assert "only write under Documents/Taxes/2025" in err
    for escape in ["Documents/Taxes/2025/../../../Email/x.md", "Documents\\Taxes\\2025\\..\\..\\..\\Email\\x.md"]:
        status, _, _ = agent("write", escape, stdin="x")
        assert status == 1, escape
    status, out, _ = agent("write", "Documents/Taxes/2025/notes.md", stdin="AGI check\n")
    assert status == 0
    assert "Saved Documents/Taxes/2025/notes.md" in out
    _, found, _ = agent("search", "AGI check")
    assert "Documents/Taxes/2025/notes.md:1" in found


@needs_sources
def test_open_link_does_not_load_external_sites(run):
    _, out, _ = agent("open", "https://www.example.org")
    assert "can't be reached" in out


@needs_sources
def test_unknown_command(run):
    status, _, err = agent("rm", "-rf", "/")
    assert status == 1
    assert "Unknown command" in err


def test_os_errors_show_no_host_paths(initialized_env):
    agent("write", "Documents/Taxes/2025/d/x.md", stdin="x")
    status, _, err = agent("write", "Documents/Taxes/2025/d", stdin="y")
    assert status == 1 and "Documents/Taxes/2025/d" in err
    for host in (str(initialized_env.workspace), initialized_env.workspace.as_posix(), "runtime", "current"):
        assert host not in err, err


def test_commands_hold_the_run_while_they_change_it(initialized_env, monkeypatch):
    """Deterministic form of the race: a slow state write must not let another command read stale state."""
    import threading
    import time
    from tab.state import State
    real_save = State.save

    def slow_save(self):
        time.sleep(0.3)
        real_save(self)
    monkeypatch.setattr(State, "save", slow_save)
    statuses = []
    threads = [threading.Thread(target=lambda: statuses.append(agent("wait", "--hours", "1")[0])) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert statuses == [0, 0, 0]
    assert initialized_env.state().data["now"] == "2026-03-20T12:00"
