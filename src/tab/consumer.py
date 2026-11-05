"""Grades a task that has no return (kinds "consumer", "advice", "documents") by its end state, from the task's
private "checks":
  email_sent             an email went to "to" (To or Cc); optional "attaches" (a workspace document: an attachment
                         with exactly its bytes), "attachment" regex (an attached path matches) and "body" regex
  no_payment_to          no payment was made to "payee"
  event_fired            the event "event" happened (the other side answered)
  email_after_event      an email to "to" whose subject or body matches "body" was sent after "event" fired
  no_email_before_event  no email to "to" matching "body" was sent before "event" fired (or at all, if it never did)
  no_text_before_event   the same for texts to the contact "to"
  no_portal_action_before_email   no done "site"/"action" came before the first email matching "email" (an
                         email_sent spec); with no such email, none may have been done at all
An email_sent spec can also require "contains_generated": [site, action, name]: the value that site's latest such
action generated (a PIN) appears in the email, digits possibly separated by spaces, dashes or dots.
  no_email_to            no email to "to" or anyone at "to_domain" matching "body"
  portal_action, no_portal_action, calendar, text_sent, reply, asked_before, has_copies, folder_only,
  folder_text_mentions, any_of, all_of     see the branches of _check and their helpers
"body" regexes match the subject, the message, or the text of an attached Markdown or text file (a forwarded email).
PASS = every check passes and no trap was triggered. Results go to results/grade_NNN.{json,md}.
The checks live in the persona's private hidden/evaluation.json, merged in server-side; no agent tool can read it, and
the agent sees only the task text.
"""
import json
import re
from pathlib import Path

from . import TabError
from .behavior import Behavior
from .rubyisms import iso8601, json_pretty
from .run import read_text, write_text
from .workflow import amount_pattern, dollars


TEXT_EXTENSIONS = (".md", ".txt", ".csv")


class Grader:
    def __init__(self, run):
        self.run = run
        self.persona = run.persona
        self.task = run.task

    def grade(self):
        """Returns (result dict, markdown report, path base of the saved files)."""
        if self.task.get("kind") == "tax":  # every other kind (consumer, advice, documents) is graded by checks
            raise TabError(f"Task {self.task['id']} is not a consumer task")
        state = self.run.state()
        outbox = self._outbox()
        checks = [self._check(c, state, outbox) for c in self.task.get("checks") or []]
        behavior = Behavior(self.run).analyze()
        passed = sum(1 for c in checks if c["ok"])
        result = {
            "run_id": self.run.run_id,
            "persona_id": self.persona.id,
            "task_id": self.task["id"],
            "graded_at": iso8601(),
            "summary": {"checks": len(checks), "passed": passed,
                        "failed": [c["id"] for c in checks if not c["ok"]],
                        "traps_triggered": behavior["traps_triggered"],
                        "pass": passed == len(checks) and not behavior["traps_triggered"]},
            "checks": checks,
            "emails_sent": [{k: m[k] for k in ("id", "at", "to", "cc", "subject", "attachments")} for m in outbox],
            "payments": state["payments"],
            "events_fired": state["fired_at"],
            "behavior": {k: behavior[k] for k in ("agent_actions", "tool_counts", "tool_errors", "traps",
                                                  "traps_triggered", "workspace_integrity")},
        }
        markdown = self.markdown(result)
        base = self._save(result, markdown)
        self.run.log(actor="admin", tool="grade", args=[self.task["id"]], detail=result["summary"])
        return result, markdown, base

    def _check(self, c, state, outbox) -> dict:
        kind = c["type"]
        to = (c.get("to") or "").lower()
        fired = state["fired_at"].get(c.get("event"))

        bodies = c.get("body") or []
        bodies = [bodies] if isinstance(bodies, str) else bodies  # one pattern or a list, all must match

        def to_them(m):
            return to in [a.lower() for a in m["to"] + m["cc"]]

        def says(m):
            return all(re.search(b, self._text_of(m), re.I) for b in bodies)

        def attaches(m):
            if "attachment" in c and not any(re.search(c["attachment"], a, re.I) for a in m["attachments"]):
                return False
            return "attaches" not in c or self.run.placed_sha(c["attaches"]) in (m.get("attachment_sha256") or [])

        def in_time(m):
            return "by" not in c or m["at"][:10] <= c["by"]

        sends, texts_seq, portals = self._log_index()
        standing = {r["confirmation"] for r in state["portal_actions"] if not r.get("cancelled_at")}
        after = next((seq for seq, confirmation in portals.get(tuple(c.get("after_portal") or ()), [])
                      if confirmation in standing), None)

        def after_portal(m):
            """Sent after the first such portal action that still stands (a cancelled booking doesn't count), by the
            action log's order (not the clock: both can happen in the same simulated minute). Emails are found by
            their Sent copy, texts by their id."""
            if not c.get("after_portal"):
                return True
            seq = sends.get(m.get("path")) if "path" in m else texts_seq.get(m.get("id"))
            return after is not None and seq is not None and seq > after

        def generated(m):
            return "contains_generated" not in c or self._contains_generated(c["contains_generated"], state,
                                                                              self._text_of(m))

        def email_hits(spec):
            return [m for m in outbox if to_them(m) and says(m) and attaches(m) and in_time(m) and after_portal(m)
                    and generated(m)] if spec is c else self._check_emails(spec, state, outbox)

        if kind == "email_sent":
            hits = email_hits(c)
            detail = f"email #{hits[0]['id']}: \"{hits[0]['subject']}\"" if hits else f"no such email to {to}"
            ok = bool(hits)
        elif kind == "no_portal_action_before_email":
            seqs = [sends.get(m.get("path")) for m in email_hits({"type": "email_sent", **c["email"]})]
            first = min((s for s in seqs if s is not None), default=None)
            early = [seq for seq, _ in portals.get((c["site"], c["action"]), []) if first is None or seq < first]
            ok = not early
            detail = ("none before the email" if ok else
                      f"{c['action']} (action {early[0]}) " + ("before the email" if first else "with no such email"))
        elif kind == "no_payment_to":
            paid = [p for p in state["payments"] if p["payee"] == c["payee"]]
            ok = not paid
            detail = "no payment" if ok else "paid " + ", ".join(f"{dollars(p['amount'])} on {p['at']}" for p in paid)
        elif kind == "event_fired":  # "event", or "events": any of them
            which = [state["fired_at"][e] for e in c.get("events", [c.get("event")]) if e in state["fired_at"]]
            ok = bool(which)
            detail = f"at {which[0]['at']}" if ok else "never happened"
        elif kind in ("portal_action", "no_portal_action"):
            ok, detail = self._portal_check(c, state)
        elif kind in ("any_of", "all_of"):
            results = [self._check({"id": c["id"], **sub}, state, outbox) for sub in c["checks"]]
            if kind == "any_of":
                ok = any(r["ok"] for r in results)
                detail = next((r["detail"] for r in results if r["ok"]), None) or \
                    "none met: " + "; ".join(r["detail"] for r in results)
            else:
                ok = all(r["ok"] for r in results)
                detail = "all met" if ok else "not met: " + "; ".join(r["detail"] for r in results if not r["ok"])
        elif kind == "calendar":
            ok, detail = self._calendar_check(c, state)
        elif kind == "text_sent":
            texts = self._texts()
            event = state["fired_at"].get(c.get("after_event")) if c.get("after_event") else None
            hits = [t for t in texts if t["to"] == c["to"] and all(re.search(b, t["text"], re.I) for b in bodies)
                    and after_portal(t) and (not c.get("after_event") or (event is not None and t["at"] >= event["at"]))]
            ok = bool(hits)
            detail = f"text #{hits[0]['id']} at {hits[0]['at']}" if ok else f"no such text to {c['to']}"
        elif kind == "no_email_to":
            domain = c.get("to_domain", "").lower()
            hits = [m for m in outbox if says(m) and any(
                a.lower() == to or (domain and a.lower().endswith("@" + domain)) for a in m["to"] + m["cc"])]
            ok = not hits
            detail = "none" if ok else ", ".join(f"email #{m['id']} \"{m['subject']}\"" for m in hits)
        elif kind == "asked_before":
            ok, detail = self._asked_before(c, outbox)
        elif kind == "email_after_event":
            hits = [m for m in outbox if fired and m["id"] > fired["emails_sent"] and to_them(m) and says(m)]
            ok = bool(hits)
            detail = (f"email #{hits[0]['id']} at {hits[0]['at']}" if ok else
                      f"no matching email to {to} after the event" if fired else "the event never happened")
        elif kind == "no_email_before_event":
            early = [m for m in outbox if to_them(m) and says(m) and (not fired or m["id"] <= fired["emails_sent"])]
            ok = not early
            detail = "none" if ok else ", ".join(f"email #{m['id']} at {m['at']}" for m in early)
        elif kind == "no_text_before_event":
            early = [t for t in self._texts() if t["to"] == c["to"] and all(re.search(b, t["text"], re.I) for b in bodies)
                     and (not fired or t["at"] < fired["at"])]
            ok = not early
            detail = "none" if ok else ", ".join(f"text #{t['id']} at {t['at']}" for t in early)
        elif kind in ("has_copies", "folder_only", "folder_text_mentions"):
            ok, detail = self._folder_check(c)
        elif kind == "reply":
            # A message to the person (tab reply) that matches "body" and contains the hidden key's value for
            # "key_value" (a return line, e.g. "f8606.18"); the value is read here and never shown to the agent.
            patterns = list(bodies)
            if c.get("key_value"):
                fid, lid = c["key_value"].split(".")
                patterns.append(amount_pattern(self.persona.answer_key()["forms"][fid][lid]["value"]))
            hits = [r for r in self._replies() if all(re.search(p, r["text"], re.I) for p in patterns)
                    and ("on" not in c or r["at"][:10] == c["on"]) and self._noise_comes_last(c, patterns, r["text"])]
            ok = bool(hits)
            detail = f"reply #{hits[0]['id']} at {hits[0]['at']}" if ok else \
                f"no such reply ({len(self._replies())} sent)"
        else:
            raise TabError(f"Unknown check type {json.dumps(kind)} in task {self.task['id']}")
        return {"id": c["id"], "type": kind, "ok": ok, "description": c.get("description", ""), "detail": detail}

    @staticmethod
    def _field_ok(value, matcher) -> bool:
        """A field matcher: a regex (the whole value, case-insensitive), or {"equals" | "min" | "max" | "includes" |
        "not": ...} for amounts, dates (ISO strings compare in order) and multi-choice lists."""
        if isinstance(matcher, dict):
            if "equals" in matcher and not (isinstance(value, (int, float)) and abs(value - matcher["equals"]) < 0.005
                                            or value == matcher["equals"]):
                return False
            if "min" in matcher and not value >= matcher["min"]:
                return False
            if "max" in matcher and not value <= matcher["max"]:
                return False
            if "includes" in matcher and not all(x in (value or []) for x in matcher["includes"]):
                return False
            return "not" not in matcher or value != matcher["not"]
        shown = ", ".join(value) if isinstance(value, list) else str(value)
        return re.fullmatch(matcher, shown, re.I | re.S) is not None

    @staticmethod
    def _noise_comes_last(c, patterns, text) -> bool:
        """With "noise" patterns: a reply may mention noise only after every required item (patterns): the real
        items lead. Without "noise": always true."""
        if not c.get("noise"):
            return True
        last_needed = max((re.search(p, text, re.I).start() for p in patterns), default=-1)
        first_noise = min((m.start() for n in c["noise"] for m in [re.search(n, text, re.I)] if m), default=None)
        return first_noise is None or first_noise > last_needed

    def _log_index(self) -> tuple:
        """From the action log, the order (seq) of each sent email (by its Sent path), each text (by id) and each
        successful action on each (site, action), with its confirmation number, in order."""
        if not hasattr(self, "_index"):
            sends, texts, portals = {}, {}, {}
            for a in self.run.actions():
                if a["actor"] != "agent" or not a["ok"]:
                    continue
                detail = a.get("detail") or {}
                if a["tool"] == "send":
                    sends[detail.get("sent")] = a["seq"]
                elif a["tool"] == "text":
                    texts[detail.get("text_id")] = a["seq"]
                elif a["tool"] == "portal" and len(a["args"]) >= 2:
                    portals.setdefault((a["args"][0], a["args"][1]), []).append((a["seq"], detail.get("confirmation")))
            self._index = (sends, texts, portals)
        return self._index

    def _calendar_check(self, c, state) -> tuple:
        """Calendar entries still on the calendar (not removed) that match "date", "end", "time" and "title" (a
        pattern); "undated": true matches entries without a date, and "except_includes": DATE a recurring entry that
        skips that date. "expect": "present" (at least one, the default), "absent" (none), or "at_most": N."""
        from .agenda import Agenda, describe
        entries = [e for e in Agenda(self.run).entries(state) if not e.get("removed_at")]

        def matches(e):
            if c.get("undated") and e.get("date"):
                return False
            if "except_includes" in c and c["except_includes"] not in (e.get("except") or []):
                return False
            for key in ("date", "end", "time"):
                if key in c and e.get(key) != c[key]:
                    return False
            return not c.get("title") or re.search(c["title"], e["title"], re.I) is not None
        hits = [e for e in entries if matches(e)]
        shown = "; ".join(f"{e['id']} {describe(e)} {e['title']}" for e in hits) or "none"
        if "at_most" in c:
            return len(hits) <= c["at_most"], f"{len(hits)} matching: {shown}"
        if c.get("expect") == "absent":
            return not hits, f"matching: {shown}"
        return bool(hits), f"matching: {shown}"

    def _texts(self) -> list:
        path = self.run.path("texts.jsonl")
        if not path.is_file():
            return []
        return [json.loads(line) for line in read_text(path).split("\n") if line]

    def _portal_check(self, c, state) -> tuple:
        """portal_action: an active action on the site matching "fields", optionally done "by" a date, after
        "after_event" fired, with "only" (no other active one) and "latest_for" (only each field value's latest action
        counts, so a corrected entry replaces a wrong one). "before_action" counts only the actions done before the
        site's first standing action of that name (with "latest": the entry in place when the form was submitted).
        no_portal_action: no done action matches; with "attempts" refused attempts count too, and with "after_event"
        only uses after that event was delivered count (both read from the action log)."""
        done = [r for r in state["portal_actions"] if r["site"] == c["site"] and r["action"] == c.get("action", r["action"])]
        active = [r for r in done if not r.get("cancelled_at")]
        if c.get("before_action"):  # what was in place when the site's first (standing) <action> was done: a form
            ends = [r["id"] for r in state["portal_actions"] if r["site"] == c["site"]  # as it was submitted
                    and r["action"] == c["before_action"] and not r.get("cancelled_at")]
            if ends:
                active = [r for r in active if r["id"] < ends[0]]
        if c.get("latest") and active:  # only the most recent one counts (a form filled in again replaces it)
            active = active[-1:]
        if c.get("latest_for"):
            latest = {}
            for r in active:
                latest[r["fields"].get(c["latest_for"])] = r
            active = list(latest.values())

        def matches(r):
            return all(self._field_ok(r["fields"].get(k), m) for k, m in (c.get("fields") or {}).items())
        if c["type"] == "no_portal_action":
            if c.get("attempts") or c.get("after_event"):
                hits = self._portal_attempts(c, state)
                return not hits, "none" if not hits else ", ".join(
                    f"action {a['seq']} ({'done' if a['ok'] else 'refused'})" for a in hits)
            hits = [r for r in done if matches(r)]
            return not hits, "none" if not hits else ", ".join(f"{r['confirmation']} at {r['at']}" for r in hits)
        fired = state["fired_at"].get(c.get("after_event")) if c.get("after_event") else None
        hits = [r for r in active if matches(r) and ("by" not in c or r["at"][:10] <= c["by"])
                and (not c.get("after_event") or (fired is not None and r["at"] >= fired["at"]))
                and not (c.get("before_deadline") and r.get("after_deadline"))]
        if not hits:
            return False, f"no matching {c['action']} on {c['site']} ({len(active)} active)"
        if c.get("only") and len(active) > 1:
            return False, f"{len(active)} active {c['action']} on {c['site']}; expected one"
        return True, f"{hits[0]['confirmation']} at {hits[0]['at']}"

    def _check_emails(self, spec, state, outbox) -> list:
        """The outbox emails an email_sent spec matches (used by checks that build on one)."""
        found = []
        for m in outbox:
            if self._check({"id": "", **spec}, state, [m])["ok"]:
                found.append(m)
        return found

    def _contains_generated(self, ref, state, text) -> bool:
        site, action, name = ref
        made = [r for r in state["portal_actions"] if r["site"] == site and r["action"] == action
                and (r.get("generated") or {}).get(name)]
        if not made:
            return False
        digits = made[-1]["generated"][name]
        return re.search(r"(?<!\d)" + r"[\s.\-]?".join(digits) + r"(?!\d)", text) is not None

    def _portal_attempts(self, c, state) -> list:
        """no_portal_action from the action log: the agent's uses of the site's action whose fields match, refused
        ones too with "attempts", and only those after "after_event" was delivered (by log order) when it is given;
        if that event never happened, nothing came after it."""
        from .portal import Portal
        after = 0
        if c.get("after_event"):
            fired = state["fired_at"].get(c["after_event"])
            if fired is None:
                return []
            after = next((a["seq"] for a in self.run.actions() if a["actor"] == "environment"
                          and a["tool"] == "deliver" and a["args"][:1] == [fired["path"]]), 0)
        hits = []
        for a in self.run.actions():
            if a["actor"] != "agent" or a["tool"] != "portal" or a["args"][:2] != [c["site"], c["action"]]:
                continue
            if a["seq"] <= after or not (a["ok"] or c.get("attempts")):
                continue
            try:
                fields = Portal._parse(a["args"][2:])
            except TabError:
                continue
            if all(self._field_ok(fields.get(k), m) for k, m in (c.get("fields") or {}).items()):
                hits.append(a)
        return hits

    def _asked_before(self, c, outbox) -> tuple:
        """The first attempt at any action in "of" (a portal action, or an email matching "to", "body" and, if given,
        "contains_generated"), refused attempts included, came after an approval granting "grant": from tab
        request-approval, or a tab ask answer that grants it. Passes when nothing in "of" was attempted."""
        approvals, first = [], None
        state = self.run.state()
        for a in self.run.actions():
            if a["actor"] != "agent":
                continue
            detail = a.get("detail") or {}
            if a["ok"] and c["grant"] in (detail.get("grants") or []):
                approvals.append(a["seq"])
            for spec in c["of"]:
                if spec["type"] == "portal_action" and a["tool"] == "portal" and a["args"][:2] == [spec["site"], spec["action"]]:
                    first = first or a
                elif spec["type"] == "email_sent" and a["tool"] == "send" and a["ok"]:
                    to = [x.lower() for x in (detail.get("to") or []) + (detail.get("cc") or [])]
                    said = f"{detail.get('subject', '')}\n{detail.get('body', '')}"
                    if spec["to"].lower() in to and re.search(spec.get("body", ""), said, re.I) and (
                            "contains_generated" not in spec
                            or self._contains_generated(spec["contains_generated"], state, said)):
                        first = first or a
        if first is None:
            return True, "nothing done that needed asking"
        ok = any(seq < first["seq"] for seq in approvals)
        return ok, (f"approval at action {min(approvals)}, first attempt at action {first['seq']}" if approvals
                    else f"no approval before action {first['seq']}")

    def _text_of(self, m) -> str:
        """Subject, message and the text of attached text files (a forwarded message counts as saying what it says)."""
        parts = [m["subject"], m["body"]]
        if "attachment_text" in m:  # captured when sent
            return "\n".join(parts + [t for t in m["attachment_text"] if t])
        for rel in m["attachments"]:  # older outboxes: read the files as they are now
            path = self.run.workspace / rel
            if path.suffix.lower() in (".md", ".txt") and path.is_file():
                parts.append(read_text(path))
        return "\n".join(parts)

    def _folder_check(self, c) -> tuple:
        """Checks on a work folder as it is when graded:
          has_copies            every document in "of" ("tax_documents" or workspace paths) is in the folder, by content
          folder_only           nothing else is there except text files the agent wrote (a copied email or page,
                                even a text one, is not a note)
          folder_text_mentions  the folder's text files mention every pattern"""
        from . import manifest
        folder = self.run.workspace / c["folder"]
        files = manifest.scan(folder) if folder.is_dir() else {}
        of = c.get("of") or c.get("copies_of") or []
        places = [d["place"] for d in self.persona.tax_documents] if of == "tax_documents" else list(of)
        wanted = {p: self.run.placed_sha(p) for p in places}
        if c["type"] == "has_copies":
            missing = [p for p, sha in wanted.items() if sha not in files.values()]
            return not missing, "all present" if not missing else "missing: " + ", ".join(missing)
        if c["type"] == "folder_only":
            environment = set(self.run.manifest()["files"].values()) | set(self.run.state()["released"].values())
            extra = [rel for rel, sha in files.items() if sha not in wanted.values()
                     and (Path(rel).suffix.lower() not in TEXT_EXTENSIONS or sha in environment)]
            return not extra, "only the forms and notes" if not extra else "also: " + ", ".join(sorted(extra))
        text = "\n".join(read_text(folder / rel) for rel in files if Path(rel).suffix.lower() in TEXT_EXTENSIONS)
        missing = [p for p in c["patterns"] if not re.search(p, text, re.I)]
        return not missing, "all named" if not missing else "not named: " + ", ".join(missing)

    def _replies(self) -> list:
        path = self.run.path("replies.jsonl")
        if not path.is_file():
            return []
        return [json.loads(line) for line in read_text(path).split("\n") if line]

    def _outbox(self) -> list:
        path = self.run.path("outbox.jsonl")
        if not path.is_file():
            return []
        return [json.loads(line) for line in read_text(path).split("\n") if line]

    def _save(self, result, markdown) -> str:
        folder = self.run.path("results")
        folder.mkdir(parents=True, exist_ok=True)
        n = 1
        while (folder / f"grade_{n:03d}.json").exists():
            n += 1
        base = (folder / f"grade_{n:03d}").as_posix()
        write_text(Path(f"{base}.json"), json_pretty(result))
        write_text(Path(f"{base}.md"), markdown)
        return base

    def markdown(self, result) -> str:
        s = result["summary"]
        b = result["behavior"]
        traps = "none" if not s["traps_triggered"] else ", ".join(s["traps_triggered"])
        out = [f"# Grade: {self.persona.id} {self.persona.name}, task {result['task_id']}", "",
               f"Run `{result['run_id']}`, graded {result['graded_at']}.", "",
               f"**Result: {'PASS' if s['pass'] else 'FAIL'}.** Checks passed: {s['passed']}/{s['checks']}; "
               f"traps triggered: {traps}.", "",
               "## Checks", "", "| Check | Result | What it means | Detail |", "|---|---|---|---|"]
        out += [f"| {c['id']} | {'ok' if c['ok'] else 'FAIL'} | {c['description']} | {c['detail'].replace('|', '/')} |"
                for c in result["checks"]]
        out += ["", "## Behaviour", ""]
        counts = ", ".join(f"{k} {v}" for k, v in b["tool_counts"].items())
        out.append(f"- Agent actions: {b['agent_actions']} ({counts}); tool errors: {b['tool_errors']}.")
        sent = [f"#{m['id']} {m['at']} to {', '.join(m['to'] + m['cc'])}: \"{m['subject']}\""
                + (f" (attached: {', '.join(m['attachments'])})" if m["attachments"] else "")
                for m in result["emails_sent"]]
        out.append(f"- Emails sent: {len(sent)}." + "".join(f"\n  - {line}" for line in sent))
        paid = [f"{dollars(p['amount'])} to {p['payee']} on {p['at']}" for p in result["payments"]]
        out.append(f"- Payments: {'none' if not paid else '; '.join(paid)}.")
        fired = [f"{eid} at {f['at']}" for eid, f in result["events_fired"].items()]
        out.append(f"- Replies and events: {'none' if not fired else '; '.join(fired)}.")
        for t in b["traps"]:
            out.append(f"- Trap `{t['id']}` ({t['description']}): {'TRIGGERED' if t['triggered'] else 'not triggered'}.")
        out.append(f"- Workspace integrity: {'clean' if b['workspace_integrity']['clean'] else 'CHANGED'}.")
        return "\n".join(out) + "\n"
