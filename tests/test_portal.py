"""Portal actions (`tab portal`): the generic framework, tested on a synthetic site so no task's rubric appears here.
Covers listing, field validation (choices, dates against the clock, conditional rules), once-only actions,
cancellation, receipts written into the inbox, logging of every attempt, event triggers, and isolation."""
import json

import pytest

from conftest import PERSONA, agent, needs_sources
from tab import environment
from tab.persona import Persona
from tab.renderer import Renderer
from tab.run import Run

pytestmark = needs_sources

SITE = "Rosebowl_Climbing_Gym"  # a real Portals/ folder; the actions below are synthetic
PORTALS = {SITE: {
    "name": "Test Gym",
    "actions": {
        "book-class": {
            "label": "Book a class", "description": "Book a class.",
            "fields": {"kind": {"type": "choice", "choices": ["yoga", "climbing"]},
                       "day": {"type": "date", "min": "today", "max": "2026-03-31"},
                       "extras": {"type": "multi", "choices": ["towel", "locker"], "optional": True},
                       "note": {"type": "text", "optional": True},
                       "fee": {"type": "amount", "optional": True}},
            "rules": [{"when": {"kind": "climbing"}, "field": "day", "choices": ["2026-03-14", "2026-03-21"]},
                      {"when": {"day": "2026-03-21"}, "field": "extras", "excludes": ["locker"],
                       "error": "No lockers that day."}],
            "message": "Class booked for {day}.",
            "receipt": {"from": "Test Gym <desk@testgym.example.com>", "subject": "Class confirmed: {day}",
                        "body": "Your {kind} class on {day} is booked. Confirmation {confirmation}."}},
        "cancel-class": {"label": "Cancel a class", "description": "Cancel a booked class.", "cancels": "book-class",
                         "message": "Class cancelled."},
        "freeze": {"label": "Freeze membership", "description": "Freeze once.", "once": True,
                   "message": "Membership frozen."},
        "unfreeze-request": {"label": "Ask to unfreeze", "description": "Needs a freeze first.", "requires": ["freeze"],
                             "message": "Unfreeze requested."},
        "leave": {"label": "Leave", "description": "Free until the deadline.", "once": True,
                  "deadline": {"at": "2026-03-11T00:00", "after": "A late fee applies."},
                  "message": "You have left."},
        "book-slot": {"label": "Book a slot", "description": "Choices that are dates.",
                      "fields": {"slot": {"type": "choice",
                                          "choices": ["2026-03-10 08:00", "2026-03-10 18:00", "Mar 9-11", "Mar 10-12"],
                                          "starts": {"2026-03-10 08:00": "2026-03-10T08:00",
                                                     "2026-03-10 18:00": "2026-03-10T18:00",
                                                     "Mar 9-11": "2026-03-09", "Mar 10-12": "2026-03-10"}}},
                      "message": "Booked {slot}."}}}}


@pytest.fixture
def gym(home, monkeypatch):
    monkeypatch.setattr(Persona, "portals", property(lambda self: PORTALS))
    real = Persona.task

    def task(self, task_id=None):
        if task_id == "portaltest":
            return {"id": "portaltest", "kind": "consumer", "start": "2026-03-10T09:00", "template": "task.md.tmpl",
                    "events": [{"id": "after_freeze", "source": "06_Events/C1_gym/Reply_cancellation_confirmed.md",
                                "trigger": {"type": "action", "tool": "portal", "args": f"^{SITE} freeze"}}]}
        return real(self, task_id)
    monkeypatch.setattr(Persona, "task", task)
    return environment.init(PERSONA, task_id="portaltest")


def records():
    return Run.current().state()["portal_actions"]


def test_portal_lists_sites_and_describes_actions(gym):
    status, out, _ = agent("portal")
    assert status == 0 and SITE in out and "book-class" in out
    status, out, _ = agent("portal", SITE)
    assert status == 0 and "[Book a class]" in out and "--kind yoga|climbing" in out and "--day" in out
    assert "after_freeze" not in out and "events" not in out  # nothing about triggers or grading
    assert agent("portal", "No_Such_Site")[0] == 1


def test_an_action_validates_its_fields_against_the_clock(gym):
    bad = [["--kind", "boxing", "--day", "2026-03-12"],      # not a choice
           ["--kind", "yoga", "--day", "2026-03-09"],        # before today (Mar 10)
           ["--kind", "yoga", "--day", "2026-04-02"],        # after the last date
           ["--kind", "yoga", "--day", "March 12"],          # not a date
           ["--kind", "yoga"],                               # a required field missing
           ["--kind", "yoga", "--day", "2026-03-12", "--colour", "red"],  # an unknown field
           ["--kind", "climbing", "--day", "2026-03-12"],    # a conditional rule: climbing only on two days
           ["--kind", "yoga", "--day", "2026-03-21", "--extras", "locker"],  # a conditional exclusion
           ["--kind", "yoga", "--day", "2026-03-12", "--fee", "-5"]]
    for args in bad:
        status, _, err = agent("portal", SITE, "book-class", *args)
        assert status == 1 and err.startswith("error: "), args
    assert records() == []
    attempts = [a for a in Run.current().actions() if a["tool"] == "portal" and len(a["args"]) > 1]
    assert len(attempts) == len(bad) and not any(a["ok"] for a in attempts)  # every refused attempt is logged


def test_an_action_records_its_fields_and_writes_its_receipt(gym):
    status, out, _ = agent("portal", SITE, "book-class", "--kind", "climbing", "--day", "2026-03-14",
                           "--extras", "towel,locker", "--fee", "$12.50")
    assert status == 0 and "Class booked for 2026-03-14." in out and "New email: Email/" in out
    rec = records()[0]
    assert rec["fields"] == {"kind": "climbing", "day": "2026-03-14", "extras": ["towel", "locker"], "fee": 12.5}
    assert rec["at"] == "2026-03-10T09:00" and rec["confirmation"] in out
    receipt = out.split("New email: ")[1].strip()
    text = (gym.workspace / receipt).read_text("utf-8")
    assert "Your climbing class on 2026-03-14 is booked." in text and rec["confirmation"] in text
    assert Run.current().integrity()["clean"] and Renderer.leak_scan(gym.workspace) == []


def test_once_only_actions_and_cancellation(gym):
    assert agent("portal", SITE, "freeze")[0] == 0
    status, _, err = agent("portal", SITE, "freeze")
    assert status == 1 and "already" in err
    _, out, _ = agent("portal", SITE, "book-class", "--kind", "yoga", "--day", "2026-03-12")
    conf = records()[-1]["confirmation"]
    assert agent("portal", SITE, "cancel-class", "--confirmation", "NOPE-1")[0] == 1
    assert agent("portal", SITE, "cancel-class", "--confirmation", conf)[0] == 0
    booked = [r for r in records() if r["action"] == "book-class"][0]
    assert booked["cancelled_at"] == "2026-03-10T09:00"
    assert agent("portal", SITE, "cancel-class", "--confirmation", conf)[0] == 1  # already cancelled


def test_an_action_can_trigger_an_event(gym):
    _, out, _ = agent("portal", SITE, "freeze")
    assert "New email: Email/" in out
    assert Run.current().state()["fired"] == ["after_freeze"]


def test_portal_needs_no_access_outside_the_workspace(gym):
    for site in ("..", "../hidden", "../../personas"):
        status, _, err = agent("portal", site)
        assert status == 1 and "Unknown site" in err


def test_an_action_can_require_an_earlier_one(gym):
    status, _, err = agent("portal", SITE, "unfreeze-request")
    assert status == 1 and "freeze" in err
    agent("portal", SITE, "freeze")
    assert agent("portal", SITE, "unfreeze-request")[0] == 0


def test_a_deadline_is_recorded_against_the_clock(gym):
    status, out, _ = agent("portal", SITE, "leave")
    assert status == 0 and "late fee" not in out and not records()[-1].get("after_deadline")


def test_an_action_after_its_deadline_is_marked(gym):
    agent("wait", "--until", "2026-03-11T09:00")
    status, out, _ = agent("portal", SITE, "leave")
    assert status == 0 and "A late fee applies." in out and records()[-1]["after_deadline"] is True


def test_a_dated_choice_that_has_passed_is_refused(gym):
    # The clock is Mar 10, 09:00: a slot earlier today and a stay that began yesterday have passed; a stay starting
    # today and a slot later today haven't.
    for slot in ("2026-03-10 08:00", "Mar 9-11"):
        status, _, err = agent("portal", SITE, "book-slot", "--slot", slot)
        assert status == 1 and "passed" in err
    for slot in ("2026-03-10 18:00", "Mar 10-12"):
        assert agent("portal", SITE, "book-slot", "--slot", slot)[0] == 0
    assert [r["fields"]["slot"] for r in records()] == ["2026-03-10 18:00", "Mar 10-12"]


def test_a_rule_can_start_at_a_time(home, monkeypatch):
    """A rule with "from" applies only once the clock reaches it (a site that closes at a known time)."""
    site = {SITE: {"name": "Test Gym", "actions": {"book-court": {
        "label": "Book a court", "description": "Book a court.",
        "fields": {"court": {"type": "choice", "choices": ["A", "B"]}},
        "rules": [{"from": "2026-03-11T09:00", "when": {"court": "A"}, "field": "court", "excludes": ["A"],
                   "error": "Court A is closed."}],
        "message": "Court {court} booked."}}}}
    monkeypatch.setattr(Persona, "portals", property(lambda self: site))
    real = Persona.task
    monkeypatch.setattr(Persona, "task", lambda self, task_id=None: {
        "id": "portaltest", "kind": "consumer", "start": "2026-03-10T09:00", "template": "task.md.tmpl"}
        if task_id == "portaltest" else real(self, task_id))
    environment.init(PERSONA, task_id="portaltest")
    assert agent("portal", SITE, "book-court", "--court", "A")[0] == 0
    agent("wait", "--until", "2026-03-11T09:00")
    status, _, err = agent("portal", SITE, "book-court", "--court", "A")
    assert status == 1 and "Court A is closed." in err
    assert agent("portal", SITE, "book-court", "--court", "B")[0] == 0


def test_an_action_can_generate_a_value(home, monkeypatch):
    """"generates": {"pin": "digits:6"} gives each record a value derived from the run, site, action and record, and
    the message can show it (a port-out PIN, a one-time code)."""
    import hashlib
    site = {SITE: {"name": "Test Gym", "actions": {"get-code": {
        "label": "Get a code", "description": "A one-time code.", "generates": {"pin": "digits:6"},
        "message": "Your code is {pin}."}}}}
    monkeypatch.setattr(Persona, "portals", property(lambda self: site))
    real = Persona.task
    monkeypatch.setattr(Persona, "task", lambda self, task_id=None: {
        "id": "portaltest", "kind": "consumer", "start": "2026-03-10T09:00", "template": "task.md.tmpl"}
        if task_id == "portaltest" else real(self, task_id))
    run = environment.init(PERSONA, task_id="portaltest")
    status, out, _ = agent("portal", SITE, "get-code")
    record = records()[-1]
    seed = f"{run.run_id}|{SITE}|get-code|{record['id']}".encode("utf-8")
    expected = f"{int(hashlib.sha256(seed).hexdigest(), 16) % 10 ** 6:06d}"
    assert status == 0 and record["generated"] == {"pin": expected} and f"Your code is {expected}." in out
