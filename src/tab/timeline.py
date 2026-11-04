"""Moving the simulated clock and delivering what becomes due.

Withheld items sit in <run>/pending/ (outside the workspace). When the clock reaches an item's date it is written into
the workspace, its hash is recorded as released (so integrity expects it), and the delivery is logged with the actor
"environment".
"""
import datetime as dt
import hashlib

from . import TabError, clock
from .run import read_text, write_text


def advance(run, to: dt.datetime) -> list:
    """Moves the clock forward to `to` and delivers everything due; returns the delivered workspace paths."""
    state = run.state()
    if to < state.now:
        raise TabError(f"Time only moves forward: it is already {clock.format_sim(state.now)}")
    state.now = to
    released = release_due(run, state)
    state.save()
    return released


def release_due(run, state) -> list:
    """Delivers pending items dated up to the clock's day, in date order. The caller saves the state."""
    today = state.now.date().isoformat()
    due = sorted((p for p in state["pending"] if p["release"] <= today), key=lambda p: (p["release"], p["path"]))
    delivered = []
    for item in due:
        state["pending"].remove(item)
        if item["path"] in delivered:
            continue  # several text blocks of one file released together: one re-render covers them
        target = run.workspace / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        if item["kind"] == "texts":
            data = clock.render_texts(read_text(run.path("pending", item["source"])), state.now.date()).encode("utf-8")
        else:
            source = run.path("pending", item["source"])
            data = source.read_bytes()
            source.unlink()
        target.write_bytes(data)
        state["released"][item["path"]] = hashlib.sha256(data).hexdigest()
        run.log(actor="environment", tool="deliver", args=[item["path"]])
        delivered.append(item["path"])
    return delivered


def deliver(run, path: str, text: str, state=None, log: bool = True) -> None:
    """Writes an environment-made file into the workspace now (an event's email, a sent copy), recorded like a
    release so integrity expects it. `log=False` for files that record the agent's own action."""
    own = state is None
    state = state or run.state()
    target = run.workspace / path
    target.parent.mkdir(parents=True, exist_ok=True)
    write_text(target, text)
    state["released"][path] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if log:
        run.log(actor="environment", tool="deliver", args=[path])
    if own:
        state.save()
