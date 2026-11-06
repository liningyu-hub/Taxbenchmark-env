"""Two command-line interfaces:
  bin/tab        the agent's tools (logged to the run's action log)
  bin/tab-admin  operator commands: init, reset, verify, log, integrity, check-key, leak-scan

Output is written as UTF-8 bytes with "\\n" line endings on every OS (a Windows console's code page is not used).
"""
import json
import os
import sys
from pathlib import Path

from . import TabError
from .agent_tools import AgentTools
from .rubyisms import puts, ruby_integer
from .run import Run, _locked

AGENT_HELP = """tab: tools for working in the tax environment

  tab task                  show your task
  tab status                show the environment status
  tab ls [PATH]             list a folder
  tab tree                  list every file
  tab read PATH [--page N]  read a file (PDFs come back as text)
  tab search PATTERN        search all files, PDFs included (case-insensitive)
  tab inbox                 list the emails
  tab open URL              open a link
  tab ask "QUESTION"        ask the taxpayer something
  tab wait --days N | --hours N | --until DATE   let time pass; mail and messages dated by then arrive
  tab send --to ADDRESS [--cc ADDRESS] --subject TEXT [--attach PATH]... < MESSAGE   send an email
  tab reply "MESSAGE" | tab reply < FILE   send a message to the person you work for (an answer, a summary)
  tab write PATH < FILE     save standard input to PATH (only under the work folder)
  tab copy PATH DEST        copy a file (PDFs too) into the work folder; DEST ending in / keeps the name
  tab calendar [add --date D [--end D] [--time HH:MM] [--until HH:MM] --title TEXT | remove --id ID [--date D]]
                            show the calendar, or add or remove an entry
  tab text --to NAME "MESSAGE" | tab text --to NAME < FILE   send a text message
  tab portal [SITE [ACTION --field VALUE ...]]   use a website: list sites, show a site's actions, do one
  tab submit PATH           submit your return (a JSON file in the environment)
  tab submit - < FILE       submit your return from standard input
  tab request-approval "SUMMARY"   ask the taxpayer to approve filing and paying the submitted return
  tab efile                 e-file the latest submitted return (it must be approved)
  tab pay --to PAYEE --amount N --from ACCOUNT   pay a bill from a bank account (give its account number)
"""

ADMIN_HELP = """tab-admin: operator commands for the tax environment

  tab-admin personas                  list the personas
  tab-admin tasks [PERSONA]           list a persona's tasks (id, kind, start)
  tab-admin init PERSONA [--task ID] [--force]   build a fresh environment for a task (default: prepare;
                                      --force archives the current one)
  tab-admin reset                     archive the current run and rebuild the same persona
  tab-admin status                    show the current run
  tab-admin verify [N] [--json]       tax task: score submission N (default: the latest) against the
                                      answer key, plus approval, filing and payment where the task files;
                                      consumer task: grade the end state against the task's checks
  tab-admin log [--tail N] [--agent]  show the action log
  tab-admin integrity                 compare the workspace with its initial manifest
  tab-admin check-key [PERSONA]       check the answer key against the expert's return PDF
  tab-admin leak-scan                 scan the workspace for reviewer-only material
  tab-admin serve [--host H] [--port N]   serve the agent commands over HTTP for an isolated agent
                                      (default 127.0.0.1:8765; token from TAB_TOKEN, else generated and printed)
  tab-admin agent-container --token T [--port N] [--name NAME] [--image IMAGE] [--print]
                                      start the agent's Docker container: only the one-file client inside

Environment variables: TAB_HOME (runs; default <project>/runtime),
TAB_DAY1_DIR (the "Day 1 Environment" folder; default ~/Downloads/Day 1 Environment).
"""

ERRORS = (TabError, ValueError, OSError)  # Ruby: Error, ArgumentError, SystemCallError


def agent(argv, stdin, out, err) -> int:
    argv = list(argv)
    cmd = argv.pop(0) if argv else None
    if cmd is None or cmd in ("help", "-h", "--help"):
        puts(out, AGENT_HELP)
        return 0

    run = None
    logged_args = list(argv)
    try:
        run = Run.current()
        # One command at a time per run: a command reads the state, changes it and writes it back, so parallel
        # calls (an agent's concurrent tool calls) would otherwise lose each other's changes.
        with _locked(run.path("agent.lock"), what="the environment"):
            text = _dispatch(run, cmd, argv, stdin, logged_args)
        puts(out, text)
        return 0
    except ERRORS as e:
        if run is not None:
            try:
                run.log(actor="agent", tool=cmd, args=logged_args, ok=False, error=str(e))
            except TabError:
                pass  # the log itself failed (e.g. its lock); the error below still reaches the caller
        puts(err, f"error: {_agent_error(e, run)}")
        return 1


def _dispatch(run, cmd, argv, stdin, logged_args) -> str:
    """Runs one agent command, logs it and lets the event engine react; returns the output text."""
    tools = AgentTools(run)
    if cmd == "task":
        text, detail = tools.task()
    elif cmd == "status":
        text, detail = tools.status()
    elif cmd == "ls":
        text, detail = tools.ls(argv[0] if argv else ".")
    elif cmd == "tree":
        text, detail = tools.tree()
    elif cmd == "read":
        page = take_option(argv, "--page")
        if not argv:
            raise TabError("Usage: tab read PATH [--page N]")
        text, detail = tools.read(argv[0], page=None if page is None else ruby_integer(page))
    elif cmd == "search":
        text, detail = tools.search(" ".join(argv))
    elif cmd == "inbox":
        text, detail = tools.inbox()
    elif cmd == "open":
        text, detail = tools.open_link(argv[0] if argv else None)
    elif cmd == "ask":
        text, detail = tools.ask(" ".join(argv))
    elif cmd == "wait":
        days, hours, until = (take_option(argv, o) for o in ("--days", "--hours", "--until"))
        if argv:
            raise TabError("Usage: tab wait --days N | --hours N | --until YYYY-MM-DD[THH:MM]")
        text, detail = tools.wait(days=days, hours=hours, until=until)
    elif cmd == "send":
        to, cc, attach = (_take_all(argv, o) for o in ("--to", "--cc", "--attach"))
        subject = take_option(argv, "--subject")
        if argv or subject is None:
            raise TabError("Usage: tab send --to ADDRESS [--cc ADDRESS] --subject TEXT [--attach PATH]... "
                           "< MESSAGE")
        text, detail = tools.send(to, cc, subject, attach, stdin.read())
    elif cmd == "calendar":
        from .agenda import Agenda
        text, detail = Agenda(run).handle(argv)
    elif cmd == "text":
        to = take_option(argv, "--to")
        if to is None:
            raise TabError('Usage: tab text --to NAME "MESSAGE"   or   tab text --to NAME < FILE')
        text, detail = tools.text(to, " ".join(argv).encode("utf-8") if argv else stdin.read())
    elif cmd == "portal":
        from .portal import Portal
        text, detail = Portal(run).handle(argv)
    elif cmd == "copy":
        if len(argv) != 2:
            raise TabError("Usage: tab copy PATH DEST   (DEST under the work folder; end it with / to keep the name)")
        text, detail = tools.copy(argv[0], argv[1])
    elif cmd == "reply":
        text, detail = tools.reply(" ".join(argv).encode("utf-8") if argv else stdin.read())
    elif cmd == "write":
        if not argv:
            raise TabError("Usage: tab write PATH < FILE")
        text, detail = tools.write(argv[0], stdin.read())
    elif cmd == "submit":
        source = argv[0] if argv else None
        if source is None or source == "-":
            logged_args[:] = ["-"]  # the caller logs errors with the same list
            text, detail = tools.submit(text=stdin.read().decode("utf-8"))
        else:
            text, detail = tools.submit(path=source)
    elif cmd in ("request-approval", "efile", "pay"):
        from .workflow import Workflow
        flow = Workflow(run)
        if cmd == "request-approval":
            text, detail = flow.request_approval(" ".join(argv))
        elif cmd == "efile":
            if argv:
                raise TabError("Usage: tab efile")
            text, detail = flow.efile()
        else:
            payee, amount, account = (take_option(argv, o) for o in ("--to", "--amount", "--from"))
            if argv or None in (payee, amount, account):
                raise TabError("Usage: tab pay --to PAYEE --amount N --from ACCOUNT")
            text, detail = flow.pay(payee, amount, account)
    else:
        raise TabError(f"Unknown command {json.dumps(cmd)}. Run: tab help")
    run.log(actor="agent", tool=cmd, args=logged_args, ok=True, detail=detail)
    from .events import Engine
    engine = Engine(run)
    arrived = engine.after_command(cmd, logged_args, detail)
    if arrived or engine.messages:
        if cmd == "wait":  # the clock's own deliveries were empty, but a reply or a message did arrive
            text = "\n".join(l for l in text.split("\n") if l != "Nothing new arrived.")
        notes = [f"New email: {p}" for p in arrived] + [f'{run.persona.name} writes: "{m}"' for m in engine.messages]
        text = text.rstrip("\n") + "\n" + "\n".join(notes)
    return text


def _agent_error(e, run) -> str:
    """What the agent sees of an error. An OS error names absolute host paths (the user's folders, the run folder):
    show its reason and, for a file in the workspace, the workspace path only. The log keeps the full text."""
    if not isinstance(e, OSError):
        return str(e)
    reason = e.strerror or "the operation failed"
    if run is not None and e.filename:
        try:
            rel = os.path.relpath(os.fsdecode(e.filename), run.workspace)
        except ValueError:  # another drive (Windows)
            rel = ""
        if rel and not rel.startswith(".."):
            return f"{reason}: {Path(rel).as_posix()}"
    return reason


def _remove_flag(argv: list, name: str) -> bool:
    if name in argv:
        argv.remove(name)
        return True
    return False


def admin(argv, out, err) -> int:
    from . import environment, key_check
    from .persona import Persona
    from .renderer import Renderer
    from .rubyisms import json_compact, json_pretty, to_s
    from .verifier import Verifier

    argv = list(argv)
    cmd = argv.pop(0) if argv else None
    try:
        if cmd in (None, "help", "-h", "--help"):
            puts(out, ADMIN_HELP)
        elif cmd == "personas":
            for id in Persona.ids():
                p = Persona.load(id)
                puts(out, f"{id}  {p.name}  ({' + '.join(p.data['returns'])}, tax year {p.tax_year})")
        elif cmd == "tasks":
            persona = Persona.load(argv[0] if argv else Run.current().info["persona_id"])
            for tid, t in persona.tasks().items():
                puts(out, f"{tid:<8} {t['kind']:<9} starts {t['start'].replace('T', ' ')}")
        elif cmd == "init":
            force = _remove_flag(argv, "--force")
            task_id = take_option(argv, "--task")
            if not argv:
                raise TabError("Usage: tab-admin init PERSONA [--task ID] [--force]")
            run = environment.init(argv[0], force=force, task_id=task_id)
            puts(out, f"Initialized {run.run_id}")
            puts(out, f"Workspace: {run.workspace.as_posix()}")
            puts(out, f"Files: {len(run.manifest()['files'])}, "
                      f"tax documents placed: {len(run.info['render']['tax_documents'])}")
            puts(out, f"Fingerprint: {run.info['fingerprint']}")
        elif cmd == "reset":
            result = environment.reset()
            puts(out, f"Archived the previous run to {result['archived_to']}")
            puts(out, f"Initialized {result['run'].run_id}")
            puts(out, f"Identical to the previous initial state: {'yes' if result['fingerprint_match'] else 'NO'}")
        elif cmd == "status":
            run = Run.current()
            actions = run.actions()
            puts(out, f"Run: {run.run_id} (persona {run.info['persona_id']}, created {run.info['created_at']})")
            puts(out, f"Workspace: {run.workspace.as_posix()}")
            puts(out, f"Agent actions: {sum(1 for a in actions if a['actor'] == 'agent')}, "
                      f"errors: {sum(1 for a in actions if not a['ok'])}")
            puts(out, f"Submissions: {len(run.submission_ids())}")
            puts(out, f"Workspace integrity: {'clean' if run.integrity()['clean'] else 'CHANGED'}")
        elif cmd == "verify":
            as_json = _remove_flag(argv, "--json")
            run = Run.current()
            if run.task.get("kind") != "tax":  # consumer, advice, documents: graded by the task's checks
                from .consumer import Grader
                if argv:
                    raise TabError("A consumer task has no submissions to choose from. Usage: tab-admin verify [--json]")
                result, markdown, base = Grader(run).grade()
            else:
                result, markdown, base = Verifier(run).verify(argv[0] if argv else None)
            if as_json:
                puts(out, json_pretty(result))
            else:
                puts(out, markdown)
                puts(out, f"Saved: {base}.json and {base}.md")
            return 0 if result["summary"].get("task_pass", result["summary"]["pass"]) else 2
        elif cmd == "log":
            tail = take_option(argv, "--tail")
            agent_only = _remove_flag(argv, "--agent")
            actions = Run.current().actions()
            if agent_only:
                actions = [a for a in actions if a["actor"] == "agent"]
            if tail is not None:
                n = ruby_integer(tail)
                if n < 0:
                    raise ValueError("negative array size")
                actions = actions[-n:] if n else []
            for a in actions:
                puts(out, json_compact(a))
        elif cmd == "integrity":
            puts(out, json_pretty(Run.current().integrity()))
        elif cmd == "check-key":
            id = argv[0] if argv else Run.current().info["persona_id"]
            report = key_check.run(Persona.load(id))
            for r in report["rows"]:
                puts(out, f"{r['line']:<14} {to_s(r['value']):>10}  page {to_s(r['page']):<3} {r['status']}")
            ok = not report["not_found"] and not report["in_key_not_in_task"] and not report["in_task_not_in_key"]
            puts(out, f"Not found on the cited page: {', '.join(report['not_found']) or 'none'}")
            puts(out, f"In the key but not asked for: {', '.join(report['in_key_not_in_task']) or 'none'}")
            puts(out, f"Asked for but not in the key: {', '.join(report['in_task_not_in_key']) or 'none'}")
            return 0 if ok else 1
        elif cmd == "leak-scan":
            hits = Renderer.leak_scan(Run.current().workspace)
            puts(out, "Leak scan: clean" if not hits else "\n".join(f"{h['file']}: {h['rule']}" for h in hits))
            return 0 if not hits else 1
        elif cmd == "serve":
            return _serve(argv, out)
        elif cmd == "agent-container":
            return _agent_container(argv, out)
        else:
            raise TabError(f"Unknown command {json.dumps(cmd)}. Run: tab-admin help")
        return 0
    except ERRORS as e:
        puts(err, f"error: {e}")
        return 1


def _serve(argv, out) -> int:
    """Serve the agent commands of the current run over HTTP (see server.py) until interrupted."""
    import os
    import secrets
    from . import server
    host = take_option(argv, "--host") or "127.0.0.1"
    port = ruby_integer(take_option(argv, "--port") or 8765)
    token = os.environ.get("TAB_TOKEN") or secrets.token_urlsafe(24)
    run = Run.current()
    httpd = server.make_server(host, port, token)
    puts(out, f"Serving the agent tools of run {run.run_id} at http://{host}:{httpd.server_address[1]}/agent")
    puts(out, f"TAB_TOKEN={token}")
    puts(out, "Start the agent's container with: tab-admin agent-container --port "
              f"{httpd.server_address[1]} --token {token}")
    if hasattr(out, "flush"):
        out.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def _agent_container(argv, out) -> int:
    """A container for the agent holding only the one-file client: `docker exec <name> tab task` then works."""
    import os
    import shlex
    import subprocess
    from . import ROOT
    show_only = _remove_flag(argv, "--print")
    port = ruby_integer(take_option(argv, "--port") or 8765)
    token = take_option(argv, "--token") or os.environ.get("TAB_TOKEN")
    name = take_option(argv, "--name") or "tab-agent"
    image = take_option(argv, "--image") or "python:3.12-slim"
    if not token:
        raise TabError("Give the token `tab-admin serve` printed: --token TOKEN (or set TAB_TOKEN)")
    client = (ROOT / "client" / "tab_client.py").as_posix()
    setup = ("printf '#!/bin/sh\\nexec python3 /opt/tab/tab_client.py \"$@\"\\n' > /usr/local/bin/tab "
             "&& chmod +x /usr/local/bin/tab && exec sleep infinity")
    command = ["docker", "run", "-d", "--name", name, "--add-host", "host.docker.internal:host-gateway",
               "-e", f"TAB_SERVER=http://host.docker.internal:{port}", "-e", f"TAB_TOKEN={token}",
               "-v", f"{client}:/opt/tab/tab_client.py:ro", image, "sh", "-c", setup]
    if show_only:
        puts(out, " ".join(shlex.quote(c) for c in command))
        return 0
    result = subprocess.run(command, capture_output=True, text=True,
                            env=dict(os.environ, MSYS_NO_PATHCONV="1"))
    if result.returncode != 0:
        raise TabError(f"docker run failed: {result.stderr.strip()}")
    puts(out, f"Started container {name} ({result.stdout.strip()[:12]}).")
    puts(out, f"The agent works with: docker exec -i {name} tab task   (or: docker exec -it {name} bash)")
    return 0


def admin_main() -> None:
    status = admin(sys.argv[1:], out=sys.stdout.buffer, err=sys.stderr.buffer)
    sys.stdout.flush()
    sys.exit(status)


def _take_all(argv: list, name: str) -> list:
    """Every value of a repeatable option (--to A --to B), removed from argv."""
    values = []
    while name in argv:
        values.append(take_option(argv, name))
    return values


def take_option(argv: list, name: str):
    if name not in argv:
        return None
    i = argv.index(name)
    if i + 1 >= len(argv):
        raise TabError(f"{name} needs a value")
    value = argv[i + 1]
    del argv[i:i + 2]
    return value


def agent_main() -> None:
    status = agent(sys.argv[1:], stdin=sys.stdin.buffer, out=sys.stdout.buffer, err=sys.stderr.buffer)
    sys.stdout.flush()
    sys.exit(status)
