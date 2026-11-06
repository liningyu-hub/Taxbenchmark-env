"""Initialize, archive and reset runs."""
import os
import secrets
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from . import VERSION, TabError, clock, config, manifest
from .persona import Persona
from .renderer import Renderer
from .rubyisms import iso8601, json_pretty
from .run import Run, write_text


def _move_dir(src: Path, dst: Path, timeout: float = 5.0) -> None:
    """Rename a folder into place. On Windows a rename fails while any file inside is open (antivirus, indexer),
    so retry for a few seconds before giving up."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:
            if time.monotonic() > deadline:
                raise TabError(f"Could not move {src.as_posix()} to {dst.as_posix()}: {e}") from e
            time.sleep(0.1)


def init(persona_id, force: bool = False, task_id=None) -> Run:
    persona = Persona.load(persona_id)
    task = persona.task(task_id)
    start = clock.parse_when(task["start"])
    if Run.exists():
        if not force:
            raise TabError(f"An environment is already initialized (run {Run.current().run_id}). "
                           "Use bin/tab-admin reset, or init --force to archive it and start over.")
        archive_current()

    home = config.home()
    home.mkdir(parents=True, exist_ok=True)
    staging = home / f".staging-{secrets.token_hex(4)}"
    try:
        staging.mkdir(parents=True)
        renderer = Renderer(persona, staging / "workspace", staging / "cache", today=start.date(),
                            pending_dir=staging / "pending")
        report = renderer.build()
        if task.get("events"):
            from .events import render_source, updated_pages
            folder = staging / "pending" / "events"
            folder.mkdir(parents=True)
            for event in task["events"]:
                if "source" not in event:
                    continue  # a message from the person: no draft to render
                write_text(folder / f"{event['id']}.md", render_source(persona, event))
                for update in event.get("page_updates") or []:  # a hidden form line's release would undo it
                    if update["path"] in renderer.hidden_pages:
                        raise TabError(f"Event {event['id']} changes {update['path']}, which hides a form's line "
                                       f"until {renderer.hidden_pages[update['path']]}")
                updated_pages(staging / "workspace", event)  # a page an event will change must match the drafts
            leaks = Renderer.leak_scan(folder)
            if leaks:
                raise TabError("Leak scan failed: " + ", ".join(f"events/{l['file']} ({l['rule']})" for l in leaks))
        files = manifest.scan(staging / "workspace")
        fingerprint = manifest.fingerprint(files)
        write_text(staging / "manifest.json", json_pretty({"fingerprint": fingerprint, "files": files}))
        info = {
            "run_id": f"{persona.id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(2)}",
            "persona_id": persona.id,
            "created_at": iso8601(),
            "environment_date": persona.environment_date,
            "tool_version": VERSION,
            "fingerprint": fingerprint,
            "source_dir": persona.source_dir.as_posix(),
            "render": report,
            "task_id": task["id"],
            "task_start": start.strftime("%Y-%m-%dT%H:%M"),
        }
        write_text(staging / "run.json", json_pretty(info))
        state = {"now": info["task_start"], "pending": renderer.pending, "released": {}, "fired": [],
                 "approvals": [], "filings": [], "payments": []}
        write_text(staging / "state.json", json_pretty(state))
        _move_dir(staging, Run.current_dir())
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    run = Run.current()
    run.log(actor="admin", tool="init", args=[persona.id], detail={"files": len(files), "fingerprint": fingerprint})
    return run


def reset() -> dict:
    """Archives the current run and rebuilds the same persona from its pristine sources."""
    old = Run.current()
    persona_id = old.info["persona_id"]
    old_fingerprint = old.info["fingerprint"]
    old_id = old.run_id
    task_id = old.info.get("task_id")
    archived = archive_current()
    run = init(persona_id, task_id=task_id)
    match = run.info["fingerprint"] == old_fingerprint
    run.log(actor="admin", tool="reset", args=[old_id], detail={"archived_to": archived, "fingerprint_match": match})
    return {"run": run, "archived_to": archived, "fingerprint_match": match}


def archive_current() -> str:
    run = Run.current()
    Run.archive_dir().mkdir(parents=True, exist_ok=True)
    dest = Run.archive_dir() / run.run_id
    run.log(actor="admin", tool="archive", args=[dest.as_posix()])
    _move_dir(Run.current_dir(), dest)
    return dest.as_posix()
