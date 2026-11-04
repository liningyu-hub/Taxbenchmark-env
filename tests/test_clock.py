"""M2: the simulated clock. Items dated after the clock are invisible to every tool until `tab wait` reaches them."""
import datetime as dt

import pytest

from conftest import PERSONA, agent, needs_sources
from tab import TabError, clock, environment
from tab.persona import Persona
from tab.run import Run

pytestmark = needs_sources

LATER_EMAIL = "Email/2026-03-09_Statement_amount_due_185_00.md"


@pytest.fixture
def feb20(home, monkeypatch):
    """S000005 started on Feb 20, 2026: ten of its emails (Feb 24 to Mar 9) are still in the future."""
    real = Persona.task

    def task(self, task_id=None):
        if task_id == "clocktest":
            return {"id": "clocktest", "kind": "tax", "start": "2026-02-20T09:00", "template": "task.md.tmpl"}
        return real(self, task_id)
    monkeypatch.setattr(Persona, "task", task)
    return environment.init(PERSONA, task_id="clocktest")


def test_clock_helpers():
    assert clock.email_date("2026-02-24_Dan_Spring_Joshua_Tree.md") == dt.date(2026, 2, 24)
    assert clock.email_date("Hotel_folios_2025_all_drills.md") is None
    assert clock.parse_when("2026-03-05") == dt.datetime(2026, 3, 5, 9, 0)
    assert clock.parse_when("2026-03-05T14:30") == dt.datetime(2026, 3, 5, 14, 30)
    assert clock.format_sim(dt.datetime(2026, 2, 24, 9, 0)) == "Tue, Feb 24, 2026 09:00"
    with pytest.raises(TabError):
        clock.parse_when("next week")


def test_text_blocks_release_by_date():
    md = ("# Text messages (2026)\n\n**Jin** · 2/18/2026  \nJin: deadline is march 1\n\n"
          "**Dana** · 2/27/2026  \nDana: Still nothing from Morrow\n")
    assert clock.render_texts(md, dt.date(2026, 3, 1)) == md, "everything visible: unchanged"
    early = clock.render_texts(md, dt.date(2026, 2, 20))
    assert "Jin: deadline" in early and "Dana" not in early
    assert clock.render_texts(md, dt.date(2026, 2, 1)) == "# Text messages (2026)\n"
    assert clock.text_block_dates(md) == [dt.date(2026, 2, 18), dt.date(2026, 2, 27)]


def test_future_items_are_invisible_to_every_tool(feb20):
    assert not (feb20.workspace / LATER_EMAIL).exists()
    _, inbox, _ = agent("inbox")
    assert inbox.startswith("29 messages") and "Statement: amount due" not in inbox
    _, tree, _ = agent("tree")
    assert "2026-03-09" not in tree and "2026-02-24" not in tree
    _, found, _ = agent("search", "185.00")
    assert found == "No matches.\n"
    status, _, err = agent("read", LATER_EMAIL)
    assert status == 1 and "No such file" in err
    _, status_out, _ = agent("status")
    assert "Today's date: 2026-02-20" in status_out


def test_wait_releases_due_items_in_order(feb20):
    status, out, _ = agent("wait", "--until", "2026-03-04")
    assert status == 0
    assert out.startswith("It is now Wed, Mar 4, 2026 09:00.")
    assert "7 new items:" in out and LATER_EMAIL not in out
    delivered = [a["args"][0] for a in Run.current().actions() if a["actor"] == "environment"]
    assert delivered == sorted(delivered), "released in date order"
    assert len(delivered) == 7
    _, inbox, _ = agent("inbox")
    assert inbox.startswith("36 messages")
    status, out, _ = agent("wait", "--days", "5")
    assert "It is now Mon, Mar 9, 2026 09:00." in out and LATER_EMAIL in out
    _, again, _ = agent("wait", "--hours", "1")
    assert again.rstrip().endswith("Nothing new arrived.")


def test_wait_never_moves_backwards(feb20):
    status, _, err = agent("wait", "--until", "2026-02-01")
    assert status == 1 and "only moves forward" in err
    for bad in (["wait"], ["wait", "--days", "-1"], ["wait", "--days", "x"], ["wait", "--days", "1", "--hours", "2"]):
        assert agent(*bad)[0] == 1, bad
    assert "Today's date: 2026-02-20" in agent("status")[1]


def test_released_files_keep_integrity_clean(feb20):
    agent("wait", "--until", "2026-03-10")
    run = Run.current()
    assert run.integrity()["clean"]
    (run.workspace / LATER_EMAIL).write_bytes(b"edited")
    assert run.integrity()["modified"] == [LATER_EMAIL]


def test_pending_items_live_outside_the_workspace(feb20):
    pending = feb20.path("pending")
    assert any(pending.rglob("*.md")), "the future is stored in the run folder"
    assert feb20.workspace not in pending.parents


def test_default_task_starts_on_the_environment_date(home):
    run = environment.init(PERSONA)
    assert run.info["task_id"] == "prepare"
    assert not any(run.path("pending").rglob("*")) if run.path("pending").exists() else True
    assert "Today's date: 2026-03-20" in agent("status")[1]
    status, out, _ = agent("wait", "--days", "1")
    assert status == 0 and "Nothing new arrived." in out


def test_unknown_task_is_refused(home):
    with pytest.raises(TabError, match="Unknown task"):
        environment.init(PERSONA, task_id="Z9")


def test_wait_far_beyond_the_calendar_is_refused(initialized_env):
    before = initialized_env.state().data["now"]
    for args in (["--days", "999999999"], ["--hours", "99999999999"]):
        status, _, err = agent("wait", *args)
        assert status == 1 and err.startswith("error: ") and "Traceback" not in err, args
    assert initialized_env.state().data["now"] == before


def test_wait_until_the_end_of_the_calendar_is_refused(initialized_env):
    """A date the calendar can hold, but replies scheduled after it couldn't: refused, and the clock stays put."""
    before = initialized_env.state().data["now"]
    status, _, err = agent("wait", "--until", "9999-12-31T22:00")
    assert status == 1 and err.startswith("error: ")
    assert initialized_env.state().data["now"] == before
