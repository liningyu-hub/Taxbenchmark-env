"""Portal actions: the buttons and forms on the persona's websites, as one generic tool.

  tab portal                          the sites that offer actions
  tab portal SITE                     a site's actions and their fields
  tab portal SITE ACTION --field V…   do it

A site's actions are declared in the public persona.json under "portals" (they are what the agent can see and press;
nothing about grading lives there):
  {"name": "...", "actions": {"<action>": {
      "label": "Request cancellation",           the button's text on the page
      "description": "...",
      "fields": {"<field>": {"type": "choice" | "multi" | "date" | "amount" | "text", "choices": [...],
                             "min": "today" | DATE, "max": "today" | DATE, "optional": true}},
      "rules": [{"when": {"<field>": value}, "field": "<field>", "choices": [...]},       a narrower choice list
                {"when": {...}, "field": "<field>", "excludes": [...], "error": "..."}],    a refusal
      "once": true,                              only while no earlier one is active
      "cancels": "<action>",                     takes --confirmation and cancels that earlier action
      "message": "... {field} ...",              shown when done
      "receipt": {"from": "...", "subject": "...", "body": "... {field} {confirmation} ..."}}}}   an email
Every attempt is logged by the CLI (refusals with ok: false). Done actions go to state.json "portal_actions" with their
simulated time and a confirmation number. Dates are checked against the simulated clock. Events can trigger on an
action (trigger type "action", tool "portal", args "^SITE ACTION"); graders read the records and the log.
"""
import datetime as dt
import json
import re
from decimal import Decimal, InvalidOperation

from . import TabError, clock
from .renderer import Renderer
from .timeline import deliver

MAX_TEXT = 2000


class Portal:
    def __init__(self, run):
        self.run = run
        self.persona = run.persona
        self.sites = self.persona.portals

    def handle(self, argv: list):
        if not argv:
            return self.list_sites()
        site = argv[0]
        if len(argv) == 1:
            return self.describe(site)
        return self.perform(site, argv[1], argv[2:])

    def list_sites(self):
        if not self.sites:
            return "No website here offers actions.", {}
        lines = ["Websites where you can act (tab portal SITE shows how):"]
        for sid, s in self.sites.items():
            lines.append(f"  {sid} ({s.get('name', sid)}): {', '.join(s['actions'])}")
        return "\n".join(lines), {}

    def describe(self, sid):
        site = self._site(sid)
        lines = [f"{site.get('name', sid)} (Portals/{sid})", ""]
        for aid, a in site["actions"].items():
            lines.append(f"{aid}  [{a.get('label', aid)}]  {a.get('description', '')}".rstrip())
            fields = self._fields(a)
            if not fields:
                lines.append("    (no fields)")
            for name, f in fields.items():
                lines.append(f"    --{name} {self._field_help(f)}" + ("   (optional)" if f.get("optional") else ""))
            lines.append(f"    tab portal {sid} {aid}" + "".join(f" --{n} ..." for n, f in fields.items()
                                                                if not f.get("optional")))
            lines.append("")
        return "\n".join(lines).rstrip("\n"), {"site": sid}

    def perform(self, sid, aid, args):
        site = self._site(sid)
        if aid not in site["actions"]:
            raise TabError(f"Unknown action {json.dumps(aid)} on {sid}. Actions: {', '.join(site['actions'])}")
        action = site["actions"][aid]
        state = self.run.state()
        records = state["portal_actions"]
        given = self._parse(args)
        fields = self._fields(action)
        unknown = [n for n in given if n not in fields]
        if unknown:
            raise TabError(f"Unknown field --{unknown[0]}. This action takes: "
                           + (", ".join(f"--{n}" for n in fields) or "no fields"))
        missing = [n for n, f in fields.items() if not f.get("optional") and n not in given]
        if missing:
            raise TabError(f"Missing --{missing[0]}. Run: tab portal {sid}")
        values = {n: self._value(n, fields[n], v, state.now.date()) for n, v in given.items()}
        for rule in action.get("rules", []):
            if all(values.get(k) == v for k, v in rule.get("when", {}).items()) and rule["field"] in values:
                value = values[rule["field"]]
                chosen = value if isinstance(value, list) else [value]
                if "choices" in rule and not all(c in rule["choices"] for c in chosen):
                    raise TabError(f"--{rule['field']} must be one of: {', '.join(rule['choices'])}")
                if any(c in rule.get("excludes", []) for c in chosen):
                    raise TabError(rule.get("error", f"--{rule['field']} can't be {', '.join(chosen)} here"))
        active = [r for r in records if r["site"] == sid and not r.get("cancelled_at")]
        if action.get("once"):
            done = [r for r in active if r["action"] == aid]
            if done:
                raise TabError(f"That is already done (confirmation {done[0]['confirmation']}).")
        target = None
        if action.get("cancels"):
            target = next((r for r in active if r["action"] == action["cancels"]
                           and r["confirmation"] == values["confirmation"]), None)
            if target is None:
                raise TabError(f"No active {action['cancels']} with confirmation {values['confirmation']}.")
        number = len(records) + 1
        prefix = "".join(w[0] for w in sid.split("_") if w)[:4].upper()
        record = {"id": number, "at": state.data["now"], "site": sid, "action": aid, "fields": values,
                  "confirmation": f"{prefix}-{number:04d}"}
        if target is not None:
            target["cancelled_at"] = state.data["now"]
            record["cancels"] = target["confirmation"]
        records.append(record)
        out = [f"{action.get('label', aid)}: done. Confirmation {record['confirmation']}."]
        if action.get("message"):
            out.append(self._fill(action["message"], values, record))
        if action.get("receipt"):
            out.append(f"New email: {self._receipt(action['receipt'], values, record, state)}")
        state.save()
        return "\n".join(out), {"site": sid, "action": aid, "fields": values, "confirmation": record["confirmation"]}

    # -- helpers -------------------------------------------------------------------------------------------------

    def _site(self, sid):
        if sid not in self.sites:
            known = ", ".join(self.sites) or "none"
            raise TabError(f"Unknown site {json.dumps(sid)}. Sites with actions: {known}")
        return self.sites[sid]

    @staticmethod
    def _fields(action) -> dict:
        fields = dict(action.get("fields") or {})
        if action.get("cancels"):
            fields["confirmation"] = {"type": "text"}
        return fields

    @staticmethod
    def _field_help(f) -> str:
        kind = f.get("type", "text")
        if kind in ("choice", "multi"):
            joined = "|".join(f["choices"])
            return joined if kind == "choice" else f"{joined} (one or more, comma-separated)"
        if kind == "date":
            limits = [f"from {f['min']}" if "min" in f else "", f"until {f['max']}" if "max" in f else ""]
            return "YYYY-MM-DD" + (" (" + ", ".join(x for x in limits if x) + ")" if any(limits) else "")
        if kind == "amount":
            return "AMOUNT (dollars)"
        return "TEXT"

    @staticmethod
    def _parse(args) -> dict:
        given, i = {}, 0
        while i < len(args):
            name = args[i]
            if not name.startswith("--") or i + 1 >= len(args):
                raise TabError(f"Give fields as --name value (got {json.dumps(name)})")
            given[name[2:]] = args[i + 1]
            i += 2
        return given

    @staticmethod
    def _limit(text, today):
        return today if text == "today" else dt.date.fromisoformat(text)

    def _value(self, name, f, raw, today):
        kind = f.get("type", "text")
        raw = raw.strip()
        if kind == "choice":
            if raw not in f["choices"]:
                raise TabError(f"--{name} must be one of: {', '.join(f['choices'])}")
            return raw
        if kind == "multi":
            chosen = [c.strip() for c in raw.split(",") if c.strip()]
            bad = [c for c in chosen if c not in f["choices"]]
            if not chosen or bad:
                raise TabError(f"--{name} takes one or more of: {', '.join(f['choices'])} (comma-separated)")
            return chosen
        if kind == "date":
            try:
                day = dt.date.fromisoformat(raw)
            except ValueError:
                raise TabError(f"--{name} must be a date like 2026-03-14 (got {json.dumps(raw)})") from None
            if "min" in f and day < self._limit(f["min"], today):
                raise TabError(f"--{name} can't be before {self._limit(f['min'], today).isoformat()}")
            if "max" in f and day > self._limit(f["max"], today):
                raise TabError(f"--{name} can't be after {self._limit(f['max'], today).isoformat()}")
            return day.isoformat()
        if kind == "amount":
            m = re.fullmatch(r"\$?(\d{1,3}(?:,\d{3})+|\d+)(\.\d{1,2})?", raw)
            try:
                value = Decimal(m.group(1).replace(",", "") + (m.group(2) or "")) if m else None
            except InvalidOperation:
                value = None
            if value is None or value <= 0:
                raise TabError(f"--{name} must be an amount in dollars, like 1234.56 (got {json.dumps(raw)})")
            return int(value) if value == value.to_integral_value() else float(value)
        if not raw or len(raw) > MAX_TEXT:
            raise TabError(f"--{name} must be text of 1 to {MAX_TEXT} characters")
        return raw

    @staticmethod
    def _fill(template, values, record) -> str:
        shown = {k: ", ".join(v) if isinstance(v, list) else v for k, v in values.items()}
        shown["confirmation"] = record["confirmation"]
        return re.sub(r"\{(\w+)\}", lambda m: str(shown.get(m.group(1), m.group(0))), template)

    def _receipt(self, spec, values, record, state) -> str:
        day = state.now.date()
        subject = self._fill(spec["subject"], values, record)
        name = self.persona.name
        text = "\n".join([f"# {subject}", "", f"**From:** {spec['from']}  ",
                          f"**To:** {name} <{self.persona.data.get('email', '')}>  ",
                          f"**Date:** {clock.format_email_date(day)}  ", f"**Subject:** {subject}  ", "",
                          self._fill(spec["body"], values, record), ""])
        from .events import unique_path
        path = unique_path(self.run.workspace, "Email/" + Renderer.email_name(f"{day.isoformat()}_portal.md", text))
        deliver(self.run, path, text, state)
        return path
