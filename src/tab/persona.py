"""One persona = personas/<id>/persona.json (public config) + hidden/ (answer key, simulated user).

The persona's documents themselves stay in the Day 1 Environment folder and are only read.
"""
import json
import re
from pathlib import Path

from . import ROOT, TabError, config


NO_FACTS_ANSWER = "I'm not sure. Everything I have is in my email, on the websites I use, and in my documents."


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _merge(base: dict, extra: dict) -> dict:
    """extra layered over base: nested objects merge key by key, anything else replaces."""
    out = dict(base)
    for k, v in extra.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


class Persona:
    def __init__(self, id: str, dir: Path, data: dict):
        self.id = id
        self.dir = dir
        self.data = data

    @staticmethod
    def ids() -> list:
        root = config.personas_dir()
        if not root.is_dir():
            return []
        return sorted(d.name for d in root.iterdir() if (d / "persona.json").is_file())

    @classmethod
    def load(cls, id) -> "Persona":
        """The public environment config (persona.json), merged with the persona's evaluation data
        (hidden/evaluation.json: checks, event triggers, approval behaviour, traps, filing rules) when that file is
        present. Only server-side code loads personas; no agent tool returns this data."""
        dir = config.personas_dir() / str(id)
        file = dir / "persona.json"
        if not file.is_file():
            raise TabError(f"Unknown persona {json.dumps(str(id))}. Available: {', '.join(cls.ids())}")
        data = _read_json(file)
        hidden = dir / data.get("evaluation", "hidden/evaluation.json")
        if hidden.is_file():
            evaluation = _read_json(hidden)
            evaluation.pop("about", None)
            data = _merge(data, evaluation)
        return cls(str(id), dir, data)

    def has_evaluation(self) -> bool:
        return (self.dir / self.data.get("evaluation", "hidden/evaluation.json")).is_file()

    @property
    def name(self):
        return self.data["name"]

    @property
    def tax_year(self):
        return self.data["tax_year"]

    @property
    def environment_date(self):
        return self.data["environment_date"]

    @property
    def visible_folders(self) -> dict:
        return self.data["visible_folders"]

    @property
    def writable_folder(self) -> str:
        return self.data["writable_folder"]

    @property
    def tax_documents(self) -> list:
        return self.data["tax_documents"]

    @property
    def traps(self) -> list:
        return self.data.get("traps") or []

    @property
    def output_spec(self) -> dict:
        return self.data["output_spec"]

    # Filing and payment (tasks with requires_filing; see workflow.py)
    @property
    def accounts(self) -> dict:
        return self.data.get("accounts") or {}

    @property
    def payees(self) -> dict:
        return self.data.get("payees") or {}

    @property
    def portals(self) -> dict:
        """Website actions the agent can take (tab portal), by site folder name. Public: it is what the agent sees."""
        return self.data.get("portals") or {}

    @property
    def filing(self) -> dict:
        """Return id -> {"name", "form", "balance_due" (line id), "payee"}."""
        return self.data.get("filing") or {}

    @property
    def source_dir(self) -> Path:
        return config.day1_dir() / self.data["source_folder"]

    @property
    def expert_return_path(self) -> Path:
        return self.source_dir / self.data["expert_return"]

    # Hidden material. Only the verifier, check-key and the simulated user read these; no agent tool returns them.
    @property
    def answer_key_path(self) -> Path:
        return self.dir / self.data["answer_key"]

    def has_answer_key(self) -> bool:
        return self.answer_key_path.is_file()

    def answer_key(self) -> dict:
        """The answer key is left out of public copies of the repository; verify and check-key need it."""
        if not self.has_answer_key():
            rel = self.answer_key_path.relative_to(ROOT).as_posix()
            raise TabError(f"Answer key not found: {rel}. "
                           "It is kept out of public copies of this repository; put the file there to verify.")
        return _read_json(self.answer_key_path)

    def simulated_user(self) -> dict:
        """The simulated user's facts (private). Without them, every question gets the neutral default answer."""
        path = self.dir / self.data["simulated_user"]
        if not path.is_file():
            return {"default": NO_FACTS_ANSWER, "facts": []}
        return _read_json(path)

    def tasks(self) -> dict:
        """The persona's tasks by id. "prepare" (prepare the return, dated environment_date) always exists: it is the
        original MVP task, so a persona.json without "tasks" behaves exactly as before."""
        tasks = dict(self.data.get("tasks") or {})
        tasks.setdefault("prepare", {"kind": "tax", "start": f"{self.environment_date}T09:00",
                                     "template": "task.md.tmpl"})
        return tasks

    def task(self, task_id=None) -> dict:
        task_id = task_id or "prepare"
        tasks = self.tasks()
        if task_id not in tasks:
            raise TabError(f"Unknown task {json.dumps(task_id)} for {self.id}. Available: {', '.join(tasks)}")
        return {"id": task_id, **tasks[task_id]}

    def task_text(self, task=None) -> str:
        """The task's template with its {{placeholders}} filled; {{output_spec}} expands the way the Ruby ERB loop
        did. {{environment_date}} is the task's start date."""
        task = task or self.task()
        template = (self.dir / task.get("template", "task.md.tmpl")).read_bytes().decode("utf-8")
        values = {
            "name": str(self.name),
            "tax_year": str(self.tax_year),
            "environment_date": task["start"][:10],
            "writable_folder": str(self.writable_folder),
            "id": self.id,
            "output_spec": "".join(
                f"### `{fid}`: {form['title']}\n\n"
                + "".join(f"- `{line}`: {label}\n" for line, label in form["lines"].items())
                + "\n"
                for fid, form in self.output_spec.items()),
        }
        return re.sub(r"\{\{(\w+)\}\}", lambda m: values[m.group(1)], template)
