"""The run's changing state, in <run>/state.json (outside the workspace, never shown to the agent):
  now        the simulated clock (YYYY-MM-DDTHH:MM)
  pending    items withheld until their date: {"path", "kind" ("file" | "texts"), "release" (YYYY-MM-DD), "source"}
  released   workspace paths the environment wrote after init, with the SHA-256 it wrote (integrity expects them)
  fired      ids of events already fired
  fired_at   event id -> {"at", "path", "emails_sent" (outbox messages sent before it fired)}
  scheduled  events triggered with a delay: {"id", "at"}
  approvals, filings, payments   the approval and filing workflow (M5)
  portal_actions   website actions done (tab portal): site, action, fields, confirmation, cancelled_at
Runs created before the clock existed (the saved Ruby run) have no state file and read as their start time.
"""
import datetime as dt
import json

from .rubyisms import json_pretty
from .run import read_text, write_text

FIELDS = {"pending": list, "released": dict, "fired": list, "fired_at": dict, "scheduled": list,
          "approvals": list, "filings": list, "payments": list, "portal_actions": list}


class State:
    def __init__(self, run):
        self.run = run
        self.path = run.path("state.json")
        if self.path.is_file():
            self.data = json.loads(read_text(self.path))
        else:
            self.data = {"now": run.task_start()}
        for key, kind in FIELDS.items():
            self.data.setdefault(key, kind())

    @property
    def now(self) -> dt.datetime:
        return dt.datetime.strptime(self.data["now"], "%Y-%m-%dT%H:%M")

    @now.setter
    def now(self, when: dt.datetime):
        self.data["now"] = when.strftime("%Y-%m-%dT%H:%M")

    def __getitem__(self, key):
        return self.data[key]

    def save(self) -> None:
        write_text(self.path, json_pretty(self.data))
