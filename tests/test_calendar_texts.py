"""The calendar tool (`tab calendar`) and text messages (`tab text`): generic behaviour, no task rubric. The calendar
starts as the persona's drafted calendar and keeps every change in the run's state; the workspace calendar file is
re-rendered by the environment, so integrity stays clean."""
import json

import pytest

from conftest import agent, needs_sources
from tab import environment
from tab.renderer import Renderer
from tab.run import Run

pytestmark = needs_sources
CAL = "Calendar/2026_Spring_events.md"


@pytest.fixture
def run(home):
    return environment.init("S000005", task_id="C3")


def entries():
    return Run.current().state().data["calendar"]


def test_calendar_lists_the_drafted_entries(run):
    status, out, _ = agent("calendar")
    assert status == 0 and CAL in out
    assert "2026-03-05 10:00" in out and "Dentist cleaning" in out
    assert "2026-04-17 → 2026-04-19" in out
    assert all(line.split()[0].startswith("E") for line in out.splitlines()[1:] if line.strip())


def test_adding_and_removing_entries_rewrites_the_calendar_file(run):
    status, out, _ = agent("calendar", "add", "--date", "2026-03-20", "--end", "2026-03-22", "--title", "Weekend away")
    assert status == 0 and "Added E6" in out
    status, out, _ = agent("calendar", "add", "--date", "2026-03-24", "--time", "18:00", "--until", "19:30",
                           "--title", "Book club")
    assert status == 0
    dentist = next(e for e in entries() if e["title"] == "Dentist cleaning")
    assert agent("calendar", "remove", "--id", dentist["id"])[0] == 0
    text = (run.workspace / CAL).read_text("utf-8")
    assert "Weekend away" in text and "2026-03-20 → 03-22" in text and "2026-03-24 18:00–19:30" in text
    assert "Dentist cleaning" not in text and "# Calendar: spring 2026" in text
    added = [e for e in entries() if e["origin"] == "agent"]
    assert [(e["date"], e.get("end"), e.get("time"), e["title"]) for e in added] == [
        ("2026-03-20", "2026-03-22", None, "Weekend away"), ("2026-03-24", None, "18:00", "Book club")]
    assert added[0]["added_at"] == "2026-03-10T09:00" and dentist["id"] in [e["id"] for e in entries() if e.get("removed_at")]
    assert Run.current().integrity()["clean"] and Renderer.leak_scan(run.workspace) == []


def test_calendar_input_is_validated(run):
    for args in (["add", "--date", "2026-02-30", "--title", "x"], ["add", "--date", "2026-03-20", "--title", ""],
                 ["add", "--title", "no date"], ["add", "--date", "2026-03-20", "--end", "2026-03-19", "--title", "x"],
                 ["add", "--date", "2026-03-20", "--time", "25:00", "--title", "x"], ["remove", "--id", "E99"],
                 ["rename"]):
        assert agent("calendar", *args)[0] == 1, args
    assert agent("calendar", "remove", "--id", "E1")[0] == 0
    assert agent("calendar", "remove", "--id", "E1")[0] == 1  # already removed


def test_texts_go_to_known_contacts_and_are_kept(run):
    status, out, _ = agent("text", "--to", "Dan", "See you on the 17th.")
    assert status == 0 and "Dan" in out
    assert agent("text", "--to", "Dan", stdin="Bringing the stove.\n")[0] == 0
    assert agent("text", "--to", "Nobody", "hi")[0] == 1
    assert agent("text", "--to", "Dan")[0] == 1  # nothing to say
    texts = [json.loads(l) for l in Run.current().path("texts.jsonl").read_text("utf-8").splitlines()]
    assert [(t["to"], t["text"]) for t in texts] == [("Dan", "See you on the 17th."), ("Dan", "Bringing the stove.\n")]
    sent = (run.workspace / "Texts/Sent.md").read_text("utf-8")
    assert "See you on the 17th." in sent and "Bringing the stove." in sent
    assert Run.current().integrity()["clean"]


def with_weekly_choir(run):
    """Adds a recurring row to the drafted calendar before its first use (Thomas's drafts have none)."""
    path = run.workspace / CAL
    path.write_bytes(path.read_bytes() + "| Tuesdays 19:00 | Choir practice |\n".encode("utf-8"))
    return next(l.split()[0] for l in agent("calendar")[1].splitlines()[1:] if "Choir" in l)


def test_remove_one_date_of_a_recurring_entry(run):
    choir = with_weekly_choir(run)
    status, out, _ = agent("calendar", "remove", "--id", choir, "--date", "2026-03-24")
    assert status == 0 and "2026-03-24" in out
    assert next(e for e in entries() if e["id"] == choir)["except"] == ["2026-03-24"]
    assert "Tuesdays 19:00 (not on 2026-03-24)" in agent("calendar")[1]
    assert "| Tuesdays 19:00 (not on 2026-03-24) | Choir practice |" in (run.workspace / CAL).read_text("utf-8")


def test_removing_a_date_needs_a_recurring_entry(run):
    dentist = next(l.split()[0] for l in agent("calendar")[1].splitlines()[1:] if "Dentist" in l)
    status, _, err = agent("calendar", "remove", "--id", dentist, "--date", "2026-03-05")
    assert status == 1 and "Only a recurring entry" in err
    assert not next(e for e in entries() if e["id"] == dentist).get("removed_at")


def test_removing_a_date_twice_is_refused(run):
    choir = with_weekly_choir(run)
    assert agent("calendar", "remove", "--id", choir, "--date", "2026-03-24")[0] == 0
    status, _, err = agent("calendar", "remove", "--id", choir, "--date", "2026-03-24")
    assert status == 1 and "already" in err
    assert agent("calendar", "remove", "--id", choir, "--date", "March 31")[0] == 1
    assert next(e for e in entries() if e["id"] == choir)["except"] == ["2026-03-24"]


def test_a_date_that_isnt_an_occurrence_is_refused(run):
    choir = with_weekly_choir(run)  # Tuesdays
    status, _, err = agent("calendar", "remove", "--id", choir, "--date", "2026-03-26")  # a Thursday
    assert status == 1 and "Tuesday" in err and "Thursday" in err
    assert not next(e for e in entries() if e["id"] == choir).get("except")
