"""The event engine: mid-task emails released by time or by what the agent does.

A task lists its events:
  {"id": "...", "source": "06_Events/...md", "delay_hours": 0,
   "trigger": {"type": "time", "after_start_hours": 24}
            | {"type": "email_sent", "to": ADDRESS, "attaches": PATH, "not_attaches": PATH,
               "attachment": REGEX, "no_attachment": REGEX, "unless_fired": [ids], "requires_fired": [ids]}
            | {"type": "action", "tool": TOOL, "args": REGEX},
   "page_updates": [{"path": WORKSPACE_PATH, "replace": [[OLD, NEW], ...]}],   a site page that changes when the
                                                                              event is delivered (checked at init)
   "cancels_portal": [{"site": SITE, "action": ACTION, "fields": {...}}]}     the site cancels the agent's matching
                                                                              records then (a closed campground)
After every successful agent command the engine fires each event whose trigger matches, once. An event with a delay is
scheduled and delivered when the clock passes its time. "attaches"/"not_attaches" name a workspace document: an
attachment counts when its bytes are that document's (so a renamed exact copy counts, and a different file with a
similar name doesn't). "unless_fired" also counts an event already scheduled.
The email is rendered from the Day 1 event file at init (reviewer lines stripped) into <run>/pending/events/, so a run
never needs the drafts again; when it fires, its Date placeholder is set to the delivery date and it is written into
Email/ like any arrival.
"""
import datetime as dt
import re

from . import TabError, clock
from .renderer import Renderer
from .run import read_text
from .timeline import deliver

_RELEASE_LINE = re.compile(r"^_Release:[^\n]*_[ \t]*\n+", re.MULTILINE)
_DATE_PLACEHOLDER = re.compile(r"^(\*\*Date:\*\*[ \t]*)\([^)\n]*\)", re.MULTILINE)


class Engine:
    def __init__(self, run):
        self.run = run
        self.events = run.task.get("events") or []

    def after_command(self, tool: str, args: list, detail: dict) -> list:
        """Fires what this command (or the time it let pass) triggered; returns the delivered paths. Everything that
        comes due in one command is delivered in the order of its time, whatever order the task lists it in."""
        if not self.events:
            return []
        state = self.run.state()
        start = clock.parse_when(self.run.task_start())
        by_id = {e["id"]: e for e in self.events}
        due = []  # (time, event) to deliver now
        for item in list(state["scheduled"]):
            at = dt.datetime.strptime(item["at"], "%Y-%m-%dT%H:%M")
            if state.now >= at:
                state["scheduled"].remove(item)
                due.append((at, by_id[item["id"]]))
        due_ids = {e["id"] for _, e in due}
        for event in self.events:
            waiting = {s["id"] for s in state["scheduled"]}
            if event["id"] in state["fired"] or event["id"] in waiting or event["id"] in due_ids:
                continue
            trigger = event["trigger"]
            if not self._conditions(trigger, state, due_ids):
                continue
            if trigger["type"] == "time":
                at = start + dt.timedelta(hours=trigger["after_start_hours"])
                if state.now >= at:
                    due.append((at, event))
                    due_ids.add(event["id"])
            elif self._matches(trigger, tool, args, detail or {}):
                delay = event.get("delay_hours") or 0
                if delay:
                    at = state.now + dt.timedelta(hours=delay)
                    state["scheduled"].append({"id": event["id"], "at": at.strftime("%Y-%m-%dT%H:%M")})
                else:
                    due.append((state.now, event))
                    due_ids.add(event["id"])
        delivered = []
        for at, event in sorted(due, key=lambda d: d[0]):  # stable: same-time events keep the task's order
            self._fire(event, at, state, delivered)
        state.save()
        return delivered

    @staticmethod
    def _conditions(trigger, state, due_ids) -> bool:
        """unless_fired: blocked by an event that fired, is on its way (scheduled) or is being delivered now.
        requires_fired: met by an event that fired or is being delivered now. Applies to every trigger type."""
        on_the_way = set(state["fired"]) | {s["id"] for s in state["scheduled"]} | due_ids
        if any(i in on_the_way for i in trigger.get("unless_fired") or []):
            return False
        happened = set(state["fired"]) | due_ids
        return all(i in happened for i in trigger.get("requires_fired") or [])

    def _matches(self, trigger, tool, args, detail) -> bool:
        if trigger["type"] == "email_sent":
            if tool != "send":
                return False
            recipients = [a.lower() for a in (detail.get("to") or []) + (detail.get("cc") or [])]
            if trigger["to"].lower() not in recipients:
                return False
            said = f"{detail.get('subject') or ''}\n{detail.get('body') or ''}"
            if "body" in trigger and not re.search(trigger["body"], said, re.I):
                return False
            attachments = detail.get("attachments") or []
            hashes = detail.get("attachment_sha256") or []
            if "attaches" in trigger and self.run.placed_sha(trigger["attaches"]) not in hashes:
                return False
            if "not_attaches" in trigger and self.run.placed_sha(trigger["not_attaches"]) in hashes:
                return False
            if "attachment" in trigger and not any(re.search(trigger["attachment"], a, re.I) for a in attachments):
                return False
            if "no_attachment" in trigger and any(re.search(trigger["no_attachment"], a, re.I) for a in attachments):
                return False
            return True
        if trigger["type"] == "action":
            return tool == trigger["tool"] and bool(re.search(trigger.get("args", ""), " ".join(args)))
        return False

    def _fire(self, event, when: dt.datetime, state, delivered: list) -> None:
        pages = updated_pages(self.run.workspace, event)  # first: a page that doesn't match changes nothing
        text = read_text(self.run.path("pending", "events", f"{event['id']}.md"))
        text = _DATE_PLACEHOLDER.sub(lambda m: m.group(1) + clock.format_email_date(when.date()), text)
        name = Renderer.email_name(f"{when.date().isoformat()}_event.md", text)
        path = unique_path(self.run.workspace, f"Email/{name}")
        deliver(self.run, path, text, state)
        state["fired"].append(event["id"])
        # When it fired and how many emails the agent had sent by then, so graders can tell before from after.
        outbox = self.run.path("outbox.jsonl")
        sent = sum(1 for _ in open(outbox, "rb")) if outbox.is_file() else 0
        state["fired_at"][event["id"]] = {"at": when.strftime("%Y-%m-%dT%H:%M"), "path": path, "emails_sent": sent}
        delivered.append(path)
        for page_path, page in pages:  # delivered like an arrival, so the integrity check expects the new page
            deliver(self.run, page_path, page, state)
        stamp = when.strftime("%Y-%m-%dT%H:%M")
        for spec in event.get("cancels_portal") or []:
            for record in state["portal_actions"]:
                if (record["site"], record["action"]) == (spec["site"], spec["action"])                         and not record.get("cancelled_at")                         and all(record["fields"].get(k) == v for k, v in (spec.get("fields") or {}).items()):
                    record["cancelled_at"], record["cancelled_by"] = stamp, event["id"]


def render_source(persona, event) -> str:
    """An event's email as the agent will see it, except for its date: reviewer lines and the release note removed."""
    source = persona.source_dir / event["source"]
    if not source.is_file():
        from . import TabError
        raise TabError(f"Missing event draft for {event['id']}: {source.as_posix()}")
    return _RELEASE_LINE.sub("", Renderer.clean_markdown(read_text(source)))


def updated_pages(workspace, event) -> list:
    """The pages an event changes, as (path, new text): each OLD text must occur exactly once on its page, or the
    drafts don't match the task's events (checked at init too)."""
    out = []
    for update in event.get("page_updates") or []:
        target = workspace / update["path"]
        page = read_text(target) if target.is_file() else ""
        for old, new in update["replace"]:
            if page.count(old) != 1:
                raise TabError(f"Event {event['id']}: {old!r} must occur once in {update['path']}; these drafts "
                               "don't match the task's events (check TAB_DAY1_DIR).")
            page = page.replace(old, new)
        out.append((update["path"], page))
    return out


def unique_path(workspace, rel: str) -> str:
    """rel, or rel with _2, _3... before the extension if that file already exists in the workspace."""
    if not (workspace / rel).exists():
        return rel
    stem, ext = rel.rsplit(".", 1)
    n = 2
    while (workspace / f"{stem}_{n}.{ext}").exists():
        n += 1
    return f"{stem}_{n}.{ext}"
