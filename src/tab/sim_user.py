"""Rule-based stand-in for the simulated user (a mock, not a language model).

Each fact has regex patterns; a question gets every fact whose pattern matches, or the default answer.
A specific fact can list "suppresses": the generic facts it replaces when both match.
A fact can be limited to some tasks: "kinds" (e.g. ["tax"]) and/or "tasks" (task ids); without them it applies to
every task. The approval answer, for example, is about filing a return and must not reach a consumer task.
"""
from .rubyisms import ruby_regex


class SimUser:
    def __init__(self, data: dict, task: dict = None):
        self.facts = [f for f in data["facts"] if task is None or self._applies(f, task)]
        self.default = data["default"]

    @staticmethod
    def _applies(fact: dict, task: dict) -> bool:
        kind_ok = "kinds" not in fact or task.get("kind") in fact["kinds"]
        task_ok = "tasks" not in fact or task.get("id") in fact["tasks"]
        return kind_ok and task_ok

    def answer(self, question: str):
        """Returns (answer text, matched fact ids)."""
        matched = [fact for fact in self.facts
                   if any(ruby_regex(p, ignorecase=True).search(question) for p in fact["patterns"])]
        suppressed = {s for fact in matched for s in fact.get("suppresses") or []}
        matched = [fact for fact in matched if fact["id"] not in suppressed]
        if not matched:
            return self.default, []
        return " ".join(f["answer"] for f in matched), [f["id"] for f in matched]
