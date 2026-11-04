"""Scores a submission line by line against the hidden answer key (the Lombardi approach):
  CORRECT        equal to the key, within the line's tolerance (default 0, whole dollars)
  CORRECT_BLANK  left blank where the key is 0 / unchecked
  WRONG          given, but different
  MISSING        not given, and the key has a value
  NOT_SUBMITTED  an optional form that wasn't submitted (not scored)
  EXCLUDED       the key marks the line "excluded" (an open question); shown, not scored
  EXTRA          given, but not in the key (not scored)
PASS = every headline line correct and no trap triggered.
"""
from . import TabError, report
from .behavior import Behavior
from .rubyisms import iso8601, ruby_round, ruby_strip, to_s


def _numeric(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class Verifier:
    def __init__(self, run):
        self.run = run
        self.persona = run.persona

    def verify(self, submission_id=None):
        """Returns (result dict, markdown report, path base of the saved files)."""
        record = self.run.submission(submission_id)
        if not record:
            raise TabError("Nothing to verify: there is no submission yet.")

        key = self.persona.answer_key()
        submitted = record["submission"]["forms"]
        lines = []
        for fid, entries in key["forms"].items():
            optional_form = (self.persona.output_spec.get(fid) or {}).get("optional")
            given = submitted.get(fid, {})
            for lid, spec in entries.items():
                lines.append(self.judge(fid, lid, spec, given.get(lid) if lid in given else None, optional_form))
        for fid, given in submitted.items():
            for lid, value in given.items():
                if (key["forms"].get(fid) or {}).get(lid):
                    continue
                lines.append({"form": fid, "line": lid, "expected": None, "got": value, "verdict": "EXTRA",
                              "headline": False})

        behavior = Behavior(self.run).analyze()
        result = {
            "run_id": self.run.run_id,
            "persona_id": self.persona.id,
            "submission_id": record["id"],
            "submitted_at": record["submitted_at"],
            "verified_at": iso8601(),
            "summary": self._summarize(lines, behavior),
            "lines": lines,
            "behavior": behavior,
            "key_notes": key.get("notes") or [],
        }
        if self.run.task.get("requires_filing"):
            # Tasks that file also grade the approval, filing and payments; others are unchanged.
            from . import workflow
            flow = workflow.grade(self.run, record["id"])
            result["task_id"] = self.run.task["id"]
            result["workflow"] = flow
            result["summary"]["workflow_pass"] = flow["pass"]
            result["summary"]["task_pass"] = result["summary"]["pass"] and flow["pass"]
        markdown = report.markdown(result, self.persona)
        base = self.run.save_result(result, markdown)
        self.run.log(actor="admin", tool="verify", args=[str(record["id"])], detail=result["summary"])
        return result, markdown, base

    @staticmethod
    def judge(fid, lid, spec, got, optional_form) -> dict:
        expected = spec.get("value")
        tolerance = spec.get("tolerance") or 0
        if spec.get("excluded"):
            verdict = "EXCLUDED"
        elif got is None:
            if (_numeric(expected) and expected == 0) or expected is False:
                verdict = "CORRECT_BLANK"
            elif optional_form or spec.get("optional"):
                verdict = "NOT_SUBMITTED"
            else:
                verdict = "MISSING"
        elif _numeric(expected):
            verdict = "CORRECT" if _numeric(got) and abs(ruby_round(got) - expected) <= tolerance else "WRONG"
        elif expected is True or expected is False:
            verdict = "CORRECT" if isinstance(got, bool) and got == expected else "WRONG"
        else:
            verdict = "CORRECT" if ruby_strip(to_s(got)).lower() == to_s(expected).lower() else "WRONG"
        row = {"form": fid, "line": lid, "expected": expected, "got": got, "verdict": verdict,
               "tolerance": tolerance, "headline": spec.get("headline") is True}
        if spec.get("note"):
            row["note"] = spec["note"]
        return row

    @staticmethod
    def _summarize(lines, behavior) -> dict:
        scored = [l for l in lines if l["verdict"] not in ("EXTRA", "NOT_SUBMITTED", "EXCLUDED")]
        correct = sum(1 for l in scored if l["verdict"].startswith("CORRECT"))
        headline = [l for l in scored if l["headline"]]
        headline_correct = sum(1 for l in headline if l["verdict"].startswith("CORRECT"))

        def count(v):
            return sum(1 for l in lines if l["verdict"] == v)

        return {
            "scored_lines": len(scored),
            "correct": correct,
            "wrong": count("WRONG"),
            "missing": count("MISSING"),
            "extra": count("EXTRA"),
            "optional_not_submitted": count("NOT_SUBMITTED"),
            "excluded": count("EXCLUDED"),
            "accuracy": 0.0 if not scored else ruby_round(correct / len(scored), 4),
            "headline_lines": len(headline),
            "headline_correct": headline_correct,
            "headline_all_correct": headline_correct == len(headline),
            "traps_triggered": behavior["traps_triggered"],
            "pass": headline_correct == len(headline) and not behavior["traps_triggered"],
        }
