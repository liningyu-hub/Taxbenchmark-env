"""One environment run: <TAB_HOME>/current/
  run.json        run id, persona, fingerprint of the initial workspace
  manifest.json   SHA-256 of every initial workspace file
  workspace/      what the agent sees
  cache/          PDF text, extracted at init
  actions.jsonl   execution log: one line per tool call (agent and admin), errors included
  submissions/    every submitted return, numbered
  results/        verifier output per submission (.json + .md)
Archived runs move to <TAB_HOME>/archive/<run_id>/.
"""
import errno
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from . import TabError, config, manifest as manifest_mod
from .rubyisms import iso8601, json_compact, json_pretty


LOCK_TIMEOUT = 30.0  # seconds a Windows tab call waits for another one to finish logging
_CONTENDED = {errno.EACCES, errno.EDEADLK, getattr(errno, "EDEADLOCK", errno.EDEADLK)}


@contextmanager
def _locked(path: Path, what: str = "the action log"):
    """An exclusive lock while one process numbers and appends a log line (Ruby: File#flock on the log).
    POSIX locks the log itself. Windows locks are mandatory, so a lock on the log would block readers; it locks
    byte 0 of a sidecar file, <log>.lock, instead, retrying while another process holds it, for LOCK_TIMEOUT."""
    if os.name == "nt":
        import msvcrt
        with open(path.with_name(path.name + ".lock"), "a+b") as lock:
            deadline = time.monotonic() + LOCK_TIMEOUT
            while True:
                try:
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as e:
                    if e.errno not in _CONTENDED or time.monotonic() > deadline:
                        raise TabError(f"Could not lock {what}: {e}") from e
                    time.sleep(0.05)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        with open(path, "ab") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield


def write_text(path: Path, text: str) -> None:
    """UTF-8 bytes exactly as given: no newline translation on Windows."""
    Path(path).write_bytes(text.encode("utf-8"))


def read_text(path: Path) -> str:
    """File.read(path, encoding: 'UTF-8'): the bytes as they are. Path.read_text would turn \\r\\n into \\n."""
    return Path(path).read_bytes().decode("utf-8")


def _count_lines(path: Path) -> int:
    """File.foreach(path).count: lines, counting a last line without a newline."""
    if not path.is_file():
        return 0
    data = path.read_bytes()
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


class Run:
    def __init__(self, dir):
        self.dir = Path(dir)
        self._info = None
        self._persona = None

    @staticmethod
    def current_dir() -> Path:
        return config.home() / "current"

    @staticmethod
    def archive_dir() -> Path:
        return config.home() / "archive"

    @classmethod
    def exists(cls) -> bool:
        return (cls.current_dir() / "run.json").is_file()

    @classmethod
    def current(cls) -> "Run":
        if not cls.exists():
            raise TabError("No environment is initialized. Run: bin/tab-admin init S000005")
        return cls(cls.current_dir())

    def path(self, *parts) -> Path:
        return self.dir.joinpath(*parts)

    @property
    def info(self) -> dict:
        if self._info is None:
            self._info = json.loads(read_text(self.path("run.json")))
        return self._info

    @property
    def run_id(self) -> str:
        return self.info["run_id"]

    @property
    def persona(self):
        if self._persona is None:
            from .persona import Persona
            self._persona = Persona.load(self.info["persona_id"])
        return self._persona

    @property
    def task(self) -> dict:
        """The run's task; runs created before tasks existed are the default "prepare" task."""
        return self.persona.task(self.info.get("task_id"))

    def task_start(self) -> str:
        return self.info.get("task_start") or self.task["start"]

    def state(self):
        from .state import State
        return State(self)

    @property
    def workspace(self) -> Path:
        return self.path("workspace")

    @property
    def cache_dir(self) -> Path:
        return self.path("cache")

    def manifest(self) -> dict:
        return json.loads(read_text(self.path("manifest.json")))

    def log(self, actor, tool, args=(), ok=True, error=None, detail=None) -> dict:
        path = self.path("actions.jsonl")
        with _locked(path), open(path, "ab") as f:
            entry = {"seq": _count_lines(path) + 1, "ts": iso8601(ms=True), "actor": actor, "tool": tool,
                     "args": list(args), "ok": ok}
            if error:
                entry["error"] = error
            if detail:
                entry["detail"] = detail
            f.write((json_compact(entry) + "\n").encode("utf-8"))
        return entry

    def actions(self) -> list:
        path = self.path("actions.jsonl")
        if not path.is_file():
            return []
        return [json.loads(line) for line in read_text(path).split("\n") if line]

    def submission_ids(self) -> list:
        folder = self.path("submissions")
        if not folder.is_dir():
            return []
        return sorted(int(p.stem) for p in folder.glob("*.json"))

    def save_submission(self, normalized: dict, raw: str) -> int:
        self.path("submissions").mkdir(parents=True, exist_ok=True)
        ids = self.submission_ids()
        id = (ids[-1] if ids else 0) + 1
        record = {"id": id, "submitted_at": iso8601(), "submission": normalized, "raw": raw}
        write_text(self.path("submissions", f"{id:03d}.json"), json_pretty(record))
        return id

    def submission(self, id=None):
        """The given submission, or the latest one; None when there is none."""
        if id is None:
            ids = self.submission_ids()
            if not ids:
                return None
            id = ids[-1]
        from .rubyisms import ruby_integer
        file = self.path("submissions", f"{ruby_integer(id):03d}.json")
        if not file.is_file():
            raise TabError(f"No submission {id}")
        return json.loads(read_text(file))

    def save_result(self, result: dict, markdown: str) -> str:
        """Saves results/verify_NNN.{json,md}; verifying the same submission again (say, after a key change)
        writes verify_NNN_2, _3, ... so earlier results are kept."""
        self.path("results").mkdir(parents=True, exist_ok=True)
        first = self.path("results", f"verify_{result['submission_id']:03d}").as_posix()
        base = first
        n = 1
        while Path(f"{base}.json").exists():
            n += 1
            base = f"{first}_{n}"
        write_text(Path(f"{base}.json"), json_pretty(result))
        write_text(Path(f"{base}.md"), markdown)
        return base

    def placed_sha(self, rel: str):
        """SHA-256 of the file the environment put at `rel` (at init or by a later release), or None."""
        released = self.state()["released"] if self.path("state.json").is_file() else {}
        return released.get(rel) or self.manifest()["files"].get(rel)

    def integrity(self) -> dict:
        """Compares the workspace with the initial manifest plus what the environment released since (emails and
        texts that arrived with the clock or an event). New files are allowed only in the writable folder."""
        expected = dict(self.manifest()["files"])
        if self.path("state.json").is_file():
            expected.update(self.state()["released"])
        actual = manifest_mod.scan(self.workspace)
        prefix = f"{self.persona.writable_folder}/"
        added = [rel for rel in actual if rel not in expected]
        outside = [rel for rel in added if not rel.startswith(prefix)]
        modified = [rel for rel in expected if rel in actual and actual[rel] != expected[rel]]
        missing = [rel for rel in expected if rel not in actual]
        return {"clean": not modified and not missing and not outside,
                "modified": modified, "missing": missing,
                "added_in_work_folder": [rel for rel in added if rel not in outside],
                "added_elsewhere": outside}
