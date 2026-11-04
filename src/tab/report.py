"""Markdown report of a verification result."""
from .rubyisms import ruby_round, to_s


def markdown(result: dict, persona) -> str:
    s = result["summary"]
    b = result["behavior"]
    traps = "none" if not s["traps_triggered"] else ", ".join(s["traps_triggered"])
    out = [f"# Verification: {persona.id} {persona.name}, submission {result['submission_id']}",
           "",
           (f"Run `{result['run_id']}`, submitted {result['submitted_at']}, verified {result['verified_at']}."
            if not s.get("no_submission") else
            f"Run `{result['run_id']}`, verified {result['verified_at']}. No return was submitted."),
           "",
           f"**Result: {'PASS' if s['pass'] else 'FAIL'}.** Headline lines {s['headline_correct']}/{s['headline_lines']} "
           f"correct; all scored lines {s['correct']}/{s['scored_lines']} ({to_s(ruby_round(s['accuracy'] * 100, 1))}%); "
           f"{s['wrong']} wrong, {s['missing']} missing, {s['extra']} extra, {to_s(s.get('excluded'))} excluded; "
           f"traps triggered: {traps}.",
           ""]
    flow = result.get("workflow")
    if flow:
        failed = [c["id"] for c in flow["checks"] if not c["ok"]]
        out += [f"**Task result: {'PASS' if s['task_pass'] else 'FAIL'}.** Return: {'PASS' if s['pass'] else 'FAIL'}; "
                f"approval, filing and payment: {'PASS' if flow['pass'] else 'FAIL (' + ', '.join(failed) + ')'}.",
                ""]
    out += ["## Headline lines", ""]
    out += table([l for l in result["lines"] if l["headline"]])
    out.append("")
    differ = [l for l in result["lines"]
              if not (l["verdict"].startswith("CORRECT") or l["verdict"] in ("NOT_SUBMITTED", "EXCLUDED"))]
    out += ["## Lines that differ", ""]
    out += ["None."] if not differ else table(differ)
    out.append("")
    excluded = [l for l in result["lines"] if l["verdict"] == "EXCLUDED"]
    if excluded:
        out += ["## Excluded from scoring (open questions)", ""]
        out += table(excluded)
        out.append("")
    if flow:
        out += ["## Workflow", "", "| Check | Result | Detail |", "|---|---|---|"]
        out += [f"| {c['id']} | {'ok' if c['ok'] else 'FAIL'} | {c['detail'].replace('|', '/')} |"
                for c in flow["checks"]]
        out += ["", f"Approval requests: {flow['approvals']}; refused e-file or payment attempts: "
                    f"{flow['refused_attempts']}.", ""]
    out += ["## Behaviour", ""]
    counts = ", ".join(f"{k} {v}" for k, v in b["tool_counts"].items())
    out.append(f"- Agent actions: {b['agent_actions']} ({counts}); tool errors: {b['tool_errors']}.")
    out.append(f"- Tax documents read: {b['tax_documents_read']}{_unread(b)}.")
    questions = "none" if not b["questions_asked"] else "; ".join(f'"{q}"' for q in b["questions_asked"])
    out.append(f"- Questions asked: {questions}.")
    out.append(f"- Submissions: {b['submissions']}.")
    for t in b["traps"]:
        state = f"TRIGGERED (actions {', '.join(str(n) for n in t['action_seqs'])})" if t["triggered"] else "not triggered"
        out.append(f"- Trap `{t['id']}` ({t['description']}): {state}.")
    for t in b["traps_not_instrumented"]:
        out.append(f"- Not instrumented: {t}")
    integrity = b["workspace_integrity"]
    saved = "" if not integrity["added_in_work_folder"] else \
        f"; files saved in the work folder: {', '.join(integrity['added_in_work_folder'])}"
    changes = "" if integrity["clean"] else \
        (f"; modified {to_s(integrity['modified'])}, missing {to_s(integrity['missing'])}, "
         f"added elsewhere {to_s(integrity['added_elsewhere'])}")
    out.append(f"- Workspace integrity: {'clean' if integrity['clean'] else 'CHANGED'}{saved}{changes}.")
    if result["key_notes"]:
        out += ["", "## Notes on the key", ""]
        out += [f"- {n}" for n in result["key_notes"]]
    out += ["", "## All lines", ""]
    out += table(result["lines"])
    return "\n".join(out) + "\n"


def table(lines) -> list:
    rows = ["| Form | Line | Expected | Submitted | Verdict | Note |", "|---|---|---|---|---|---|"]
    for l in lines:
        note = (l.get("note") or "").replace("|", "/")
        rows.append(f"| {l['form']} | {l['line']} | {to_s(l['expected'])} | {to_s(l['got'])} | {l['verdict']} | {note} |")
    return rows


def _unread(behavior) -> str:
    missed = [d["doc"] for d in behavior["tax_documents"] if not d["read"]]
    return "" if not missed else f" (not read: {', '.join(missed)})"
