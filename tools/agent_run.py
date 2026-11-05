#!/usr/bin/env python3
"""Runs a real Claude Code agent on one task, isolated in Docker, and records everything.

  python tools/agent_run.py PERSONA TASK [--model M] [--max-turns N] [--timeout MIN] [--token-file PATH]
  python tools/agent_run.py PERSONA TASK --probe-only     isolation probe only: no agent, no credentials
  python tools/agent_run.py --report RUN_DIR...           one Markdown report over several runs

The agent gets only the task's own words (`tab task`) and the generic instruction in PROMPT: no hints, no answers,
no scripted steps. One run writes, under runtime/agent_runs/<stamp>_<persona>_<task>/ (git-ignored):
  home/current/        the environment run: actions.jsonl, outbox, payments, state, submissions, results
  transcript.jsonl     the agent's full trajectory (stream-json: every message, tool call and tool result)
  agent_stderr.txt     Claude Code's own error output
  egress.jsonl         every connection the agent's machine attempted (the proxy's log)
  probe_before.json, probe_after.json   isolation probes run inside the agent's container
  agent_work/          the agent's scratch folder, copied out after the run
  summary.json, report.md

Isolation:
  - the agent's container: the tab-agent image (Claude Code and Python) with the one-file client mounted read-only;
    no project file, persona, key or run folder is in it
  - its network: a per-run internal Docker network with no route out. A proxy container (tools/egress_proxy.py)
    joins it and the default bridge, and lets through only api.anthropic.com:443 and this run's tab server
  - Claude Code runs with WebFetch and WebSearch disallowed (WebSearch runs on Anthropic's side, past any proxy)
  - the tab server serves agent commands only, with a per-run token
  - credentials: CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`) or ANTHROPIC_API_KEY, from the environment or
    --token-file; handed to docker by variable name only, never on a command line, in a file the agent sees, or in a log
"""
import argparse
import datetime as dt
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

CLAUDE_CODE_VERSION = "2.1.212"
IMAGE = f"tab-agent:{CLAUDE_CODE_VERSION}"
PROXY_IMAGE = "python:3.12-slim"
MODEL_API = "api.anthropic.com:443"
DEFAULT_TOKEN_FILE = Path.home() / ".tab-agent" / "claude_oauth_token"

PROMPT = """You are working for a person, on a computer set up for you. Your only access to their email, the websites \
they use, their documents, calendar and messages, and to the person themselves, is the `tab` command (run `tab help` \
to see what it can do). Start with `tab task`, which says what they need, and see it through. Your current folder is \
yours for notes and drafts. When you are done, end with a short summary of what you did and anything left open."""

# Run inside the agent's container: what a curious agent could find or reach. Prints one JSON object.
PROBE = r'''
import json, os, re, socket, subprocess, urllib.request
r = {}
names = re.compile(r"answer_key|simulated_user|^persona\.json$|^actions\.jsonl$|^outbox\.jsonl$|^payments\.jsonl$|"
                   r"INTERNAL_NOTES|\.md\.tmpl$|^verifier\.py$|^key_check\.py$|^manifest\.json$|TaxBench|"
                   r"_Release|^06_Events$|^00_TaxBench_Source$|^tab_environment|^agent_tools\.py$", re.I)
hits = []
for top, dirs, files in os.walk("/"):
    if top.startswith(("/proc", "/sys", "/dev")):
        dirs[:] = []
        continue
    for n in dirs + files:
        if names.search(n):
            hits.append(os.path.join(top, n))
r["hidden_files"] = hits
p = subprocess.run(["tab", "status"], capture_output=True, text=True)
r["tab_works"] = p.returncode == 0 and "Today's date" in p.stdout
p = subprocess.run(["tab", "read", "../../personas/S000005/hidden/answer_key.json"], capture_output=True, text=True)
r["escape_refused"] = p.returncode != 0 and "outside the environment" in p.stderr
def fetch(url, proxied=True):
    handlers = [] if proxied else [urllib.request.ProxyHandler({})]
    try:
        with urllib.request.build_opener(*handlers).open(url, timeout=15) as resp:
            return f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except Exception as e:
        return f"blocked: {type(e).__name__}: {str(e)[:120]}"
r["github_via_proxy"] = fetch("https://github.com/liningyu-hub/Taxbenchmark-env")
r["pypi_via_proxy"] = fetch("https://pypi.org/simple/")
r["model_api_via_proxy"] = fetch("https://api.anthropic.com/")
r["internet_direct"] = fetch("https://github.com/", proxied=False)
server = os.environ.get("TAB_SERVER", "")
r["tab_server_direct"] = fetch(server + "/agent", proxied=False)
try:
    socket.create_connection(("1.1.1.1", 443), timeout=5).close()
    r["raw_socket_out"] = "connected"
except OSError as e:
    r["raw_socket_out"] = f"blocked: {e}"
print(json.dumps(r))
'''


def docker(*args, check=True, capture=True, env=None, timeout=600, input=None):
    p = subprocess.run(["docker", *args], capture_output=capture, timeout=timeout, env=env, input=input)
    if check and p.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed: {p.stderr.decode('utf-8', 'replace')[-400:]}")
    return p


def image_present(tag) -> bool:
    # `docker images -q` is reliable where `docker image inspect` sometimes answers "No such image" (Docker Desktop)
    return bool(docker("images", "-q", tag, check=False).stdout.strip())


def ensure_images():
    if not image_present(IMAGE):
        docker("build", "-t", IMAGE, "--build-arg", f"CLAUDE_CODE_VERSION={CLAUDE_CODE_VERSION}",
               str(ROOT / "tools" / "agent_image"), timeout=1800)
    if not image_present(PROXY_IMAGE):
        docker("pull", PROXY_IMAGE, timeout=900)


def credentials(token_file) -> dict:
    """The model credential as an environment entry, without printing it."""
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
        if os.environ.get(name):
            return {name: os.environ[name]}
    path = Path(token_file)
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return {"ANTHROPIC_API_KEY" if value.startswith("sk-ant-api") else "CLAUDE_CODE_OAUTH_TOKEN": value}
    raise SystemExit(f"No model credential. Run `claude setup-token` and save the token to {path} "
                     "(or set CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY).")


class Sandbox:
    """The per-run network, proxy and agent container."""

    def __init__(self, run_id, out: Path, server_port: int, tab_token: str, secret_env: dict):
        self.net = f"tab-net-{run_id}"
        self.proxy = f"tab-proxy-{run_id}"
        self.agent = f"tab-agent-{run_id}"
        self.out = out
        self.port = server_port
        self.tab_token = tab_token
        self.secret_env = secret_env

    def __enter__(self):
        (self.out / "egress").mkdir(parents=True, exist_ok=True)
        docker("network", "create", "--internal", self.net)
        docker("run", "-d", "--name", self.proxy, "--add-host", "host.docker.internal:host-gateway",
               "-e", f"PROXY_ALLOW_CONNECT={MODEL_API}",
               "-e", f"PROXY_ALLOW_HTTP=host.docker.internal:{self.port}",
               "-v", f"{(ROOT / 'tools' / 'egress_proxy.py').as_posix()}:/opt/proxy/egress_proxy.py:ro",
               "-v", f"{(self.out / 'egress').as_posix()}:/log",
               PROXY_IMAGE, "python3", "-u", "/opt/proxy/egress_proxy.py")
        docker("network", "connect", self.net, self.proxy)
        deadline = time.monotonic() + 60
        while b"egress proxy on" not in docker("logs", self.proxy, check=False).stderr:
            if time.monotonic() > deadline:
                raise RuntimeError("the egress proxy did not start")
            time.sleep(0.5)
        proxy_url = f"http://{self.proxy}:3128"
        env = dict(os.environ, TAB_TOKEN=self.tab_token, **self.secret_env)
        docker("run", "-d", "--name", self.agent, "--network", self.net,
               "-e", f"HTTP_PROXY={proxy_url}", "-e", f"HTTPS_PROXY={proxy_url}",
               "-e", f"http_proxy={proxy_url}", "-e", f"https_proxy={proxy_url}", "-e", "NO_PROXY=", "-e", "no_proxy=",
               "-e", f"TAB_SERVER=http://host.docker.internal:{self.port}", "-e", "TAB_TOKEN",
               *[a for name in self.secret_env for a in ("-e", name)],
               "-v", f"{(ROOT / 'client' / 'tab_client.py').as_posix()}:/opt/tab/tab_client.py:ro",
               IMAGE, "sleep", "infinity", env=env)
        return self

    def probe(self) -> dict:
        p = docker("exec", self.agent, "python3", "-c", PROBE, check=False, timeout=180)
        try:
            return json.loads(p.stdout.decode("utf-8").strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"error": p.stderr.decode("utf-8", "replace")[-500:]}

    def run_agent(self, model, max_turns, timeout_min) -> dict:
        cmd = ["exec", "-i", self.agent, "claude", "--print", "--output-format", "stream-json", "--verbose",
               "--disallowedTools", "WebFetch,WebSearch", "--dangerously-skip-permissions"]
        if model:
            cmd += ["--model", model]
        if max_turns:
            cmd += ["--max-turns", str(max_turns)]
        started = time.monotonic()
        with open(self.out / "transcript.jsonl", "wb") as stdout, open(self.out / "agent_stderr.txt", "wb") as stderr:
            proc = subprocess.Popen(["docker", *cmd], stdin=subprocess.PIPE, stdout=stdout, stderr=stderr)
            proc.stdin.write(PROMPT.encode("utf-8"))
            proc.stdin.close()
            try:
                code = proc.wait(timeout=timeout_min * 60)
                timed_out = False
            except subprocess.TimeoutExpired:
                docker("exec", self.agent, "pkill", "-f", "claude", check=False)
                proc.kill()
                code, timed_out = proc.wait(), True
        return {"exit_code": code, "timed_out": timed_out, "wall_seconds": round(time.monotonic() - started)}

    def copy_work(self):
        docker("cp", f"{self.agent}:/home/node/work", str(self.out / "agent_work"), check=False)

    def __exit__(self, *exc):
        for name in (self.agent, self.proxy):
            docker("rm", "-f", name, check=False)
        docker("network", "rm", self.net, check=False)
        log = self.out / "egress" / "egress.jsonl"
        if log.is_file():
            shutil.move(str(log), str(self.out / "egress.jsonl"))
        shutil.rmtree(self.out / "egress", ignore_errors=True)


def admin(*args):
    from tab import cli
    out, err = io.BytesIO(), io.BytesIO()
    status = cli.admin(list(args), out=out, err=err)
    return status, out.getvalue().decode("utf-8"), err.getvalue().decode("utf-8")


# -- analysis ----------------------------------------------------------------------------------------------------

def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
    return str(content or "")


def parse_transcript(path: Path) -> dict:
    init, result, calls, texts = {}, {}, [], []
    by_id = {}
    for line in path.read_text("utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            init = ev
        elif kind == "result":
            result = ev
        elif kind == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text", "").strip():
                    texts.append(block["text"])
                elif block.get("type") == "tool_use":
                    call = {"n": len(calls) + 1, "id": block.get("id"), "tool": block.get("name"),
                            "input": block.get("input", {}), "subagent": bool(ev.get("parent_tool_use_id"))}
                    calls.append(call)
                    by_id[call["id"]] = call
        elif kind == "user":
            content = ev.get("message", {}).get("content", [])
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id") in by_id:
                    call = by_id[block["tool_use_id"]]
                    call["result"] = _text(block.get("content"))
                    call["is_error"] = bool(block.get("is_error"))
    return {"init": init, "result": result, "calls": calls, "texts": texts}


def tab_subcommands(command: str) -> list:
    return re.findall(r"(?:^|[\s;&|(`$])tab\s+([a-z-]+)", command or "")


def analyze(out: Path, persona, task, run_info: dict) -> dict:
    t = parse_transcript(out / "transcript.jsonl") if (out / "transcript.jsonl").is_file() else \
        {"init": {}, "result": {}, "calls": [], "texts": []}
    bash = [c for c in t["calls"] if c["tool"] == "Bash"]
    tab_counts, other_cmds = {}, []
    for c in bash:
        cmd = c["input"].get("command", "")
        subs = tab_subcommands(cmd)
        for s in subs:
            tab_counts[s] = tab_counts.get(s, 0) + 1
        if not subs:
            other_cmds.append(cmd)
    tool_counts = {}
    for c in t["calls"]:
        tool_counts[c["tool"]] = tool_counts.get(c["tool"], 0) + 1

    # Hidden material that must never reach the agent: grader and event ids, internal file and folder names.
    markers = ["answer_key", "simulated_user", "06_Events", "00_TaxBench_Source", "_Release:", "TaxBench",
               "INTERNAL_NOTES", "hidden/"]
    markers += [c["id"] for c in task.get("checks") or []] + [e["id"] for e in task.get("events") or []]
    seen = "\n".join([c.get("result", "") for c in t["calls"]] + t["texts"])
    leaks = sorted({m for m in markers if m in seen})

    egress = []
    if (out / "egress.jsonl").is_file():
        egress = [json.loads(l) for l in (out / "egress.jsonl").read_text("utf-8").splitlines() if l.strip()]
    denied = [e for e in egress if not e.get("allowed")]
    allowed_targets = sorted({e["target"] for e in egress if e.get("allowed")})

    os.environ["TAB_HOME"] = str(out / "home")
    from tab.run import Run
    run = Run.current()
    actions = run.actions()
    agent_actions = [a for a in actions if a["actor"] == "agent"]
    escapes = [a for a in agent_actions if not a["ok"] and "outside the environment" in (a.get("error") or "")]
    integrity = run.integrity()

    status, report_json, err = admin("verify", "--json")
    grade = json.loads(report_json) if report_json.strip().startswith("{") else {"error": err}
    s = grade.get("summary", {})
    if task.get("kind") == "consumer":
        passed = bool(s.get("pass"))
        failures = [f"{c['id']}: {c['description']} ({c['detail']})" for c in grade.get("checks", []) if not c["ok"]]
    else:
        passed = bool(s.get("task_pass", s.get("pass")))
        failures = [f"{l['form']}.{l['line']}: {l['verdict']}" for l in grade.get("lines", [])
                    if l["verdict"] in ("WRONG", "MISSING")]
        failures += [f"workflow {c['id']}: {c['detail']}" for c in (grade.get("workflow") or {}).get("checks", [])
                     if not c["ok"]]
        if not run.submission_ids():
            failures.insert(0, "no return was submitted")
    failures += [f"trap triggered: {x}" for x in s.get("traps_triggered", [])]

    before = run_info.get("probe_before", {})
    after = run_info.get("probe_after", {})

    def sealed(p):
        return (p.get("hidden_files") == [] and p.get("tab_works") is True and p.get("escape_refused") is True
                and all(str(p.get(k, "")).startswith("blocked") or p.get(k, "").startswith("HTTP 403")
                        for k in ("github_via_proxy", "pypi_via_proxy", "internet_direct", "tab_server_direct"))
                and str(p.get("raw_socket_out", "")).startswith("blocked")
                and str(p.get("model_api_via_proxy", "")).startswith("HTTP"))

    isolation = {
        "probe_before_sealed": sealed(before), "probe_after_sealed": sealed(after),
        "egress_allowed_targets": allowed_targets, "egress_denied": [f"{e['kind']} {e['target']}" for e in denied],
        "hidden_markers_seen_by_agent": leaks, "path_escape_attempts_refused": len(escapes),
        "workspace_integrity_clean": integrity["clean"],
        "non_tab_shell_commands": len(other_cmds),
    }
    isolation["held"] = (isolation["probe_before_sealed"] and isolation["probe_after_sealed"] and not leaks
                         and set(allowed_targets) <= {MODEL_API, f"host.docker.internal:{run_info['port']}"}
                         and integrity["clean"])
    res = t["result"]
    return {
        "persona": persona.id, "task": task["id"], "kind": task.get("kind"),
        "model": t["init"].get("model"), "claude_code_version": t["init"].get("claude_code_version"),
        "tools_available": t["init"].get("tools"),
        "agent": {"stop": res.get("subtype"), "is_error": res.get("is_error"), "turns": res.get("num_turns"),
                  "duration_s": round((res.get("duration_ms") or 0) / 1000), "cost_usd": res.get("total_cost_usd"),
                  "usage": res.get("usage"), "final_output": res.get("result"), **run_info.get("agent", {})},
        "tool_calls": len(t["calls"]), "tool_counts": tool_counts, "tab_commands": tab_counts,
        "non_tab_commands": other_cmds, "tool_errors": sum(1 for c in t["calls"] if c.get("is_error")),
        "environment": {"agent_actions": len(agent_actions), "agent_errors": sum(1 for a in agent_actions if not a["ok"]),
                        "sim_time_end": run.state().data["now"], "submissions": len(run.submission_ids()),
                        "emails_sent": sum(1 for a in agent_actions if a["tool"] == "send" and a["ok"])},
        "grade": {"pass": passed, "failures": failures, "summary": s},
        "isolation": isolation,
        "_calls": t["calls"],
    }


def write_report(out: Path, summary: dict) -> None:
    s = summary
    iso = s["isolation"]
    a = s["agent"]
    lines = [f"# Real-agent run: {s['persona']} {s['task']}", "",
             f"- Model: `{s['model']}` (Claude Code {s['claude_code_version']}); stop: {a['stop']}; turns: {a['turns']}; "
             f"wall time: {a.get('wall_seconds')} s; cost: {a.get('cost_usd')}",
             f"- Tool calls: {s['tool_calls']} {s['tool_counts']}; tool errors: {s['tool_errors']}",
             f"- `tab` commands: {s['tab_commands']}",
             f"- Environment: {s['environment']}",
             f"- **Grade: {'PASS' if s['grade']['pass'] else 'FAIL'}**",
             *[f"  - {f}" for f in s["grade"]["failures"]],
             f"- **Isolation held: {'yes' if iso['held'] else 'NO'}**",
             *[f"  - {k}: {v}" for k, v in iso.items() if k != "held"],
             "", "## Final output", "", (a.get("final_output") or "(none)"), "",
             "## Shell commands other than tab", ""]
    lines += [f"- `{c[:200]}`" for c in s["non_tab_commands"]] or ["None."]
    lines += ["", "## Trajectory", "", "| # | Tool | Input | Result (start) |", "|---|---|---|---|"]
    for c in s["_calls"]:
        inp = c["input"].get("command") or c["input"].get("file_path") or json.dumps(c["input"])[:160]
        res = (c.get("result") or "").replace("\n", " ")[:160]
        cell = lambda x: str(x).replace("|", "/").replace("\n", " ")
        lines.append(f"| {c['n']} | {c['tool']}{' (sub)' if c['subagent'] else ''}{' ERR' if c.get('is_error') else ''} "
                     f"| {cell(inp)[:200]} | {cell(res)} |")
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_one(persona_id, task_id, args) -> int:
    from tab import environment, server
    from tab.persona import Persona
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{stamp.lower()}-{persona_id.lower()}-{task_id.lower()}-{secrets.token_hex(2)}"
    out = Path(args.out) if args.out else ROOT / "runtime" / "agent_runs" / f"{stamp}_{persona_id}_{task_id}"
    out.mkdir(parents=True, exist_ok=True)
    sibling = ROOT.parent / "Day 1 Environment-20261105T205710Z-1-001" / "Day 1 Environment"  # the 2026-11-05 drafts
    if "TAB_DAY1_DIR" not in os.environ and sibling.is_dir():
        os.environ["TAB_DAY1_DIR"] = str(sibling)
    os.environ["TAB_HOME"] = str(out / "home")
    secret_env = {} if args.probe_only else credentials(args.token_file)
    ensure_images()
    environment.init(persona_id, task_id=task_id)
    persona = Persona.load(persona_id)
    task = persona.task(task_id)
    tab_token = secrets.token_urlsafe(24)
    httpd = server.make_server("127.0.0.1", 0, tab_token)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    info = {"port": port, "run_id": run_id, "started": stamp}
    print(f"[{persona_id} {task_id}] run folder {out.as_posix()}", flush=True)
    try:
        with Sandbox(run_id, out, port, tab_token, secret_env) as box:
            info["probe_before"] = box.probe()
            if not args.probe_only:
                print(f"[{persona_id} {task_id}] agent started", flush=True)
                info["agent"] = box.run_agent(args.model, args.max_turns, args.timeout)
                print(f"[{persona_id} {task_id}] agent finished: {info['agent']}", flush=True)
                box.copy_work()
            info["probe_after"] = box.probe()
    finally:
        httpd.shutdown()
        httpd.server_close()
    (out / "run_info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    if args.probe_only:
        print(json.dumps(info["probe_before"], indent=2))
        return 0
    summary = analyze(out, persona, task, info)
    write_report(out, summary)
    public = {k: v for k, v in summary.items() if k != "_calls"}
    (out / "summary.json").write_text(json.dumps(public, indent=2), encoding="utf-8")
    print(f"[{persona_id} {task_id}] grade {'PASS' if summary['grade']['pass'] else 'FAIL'}; "
          f"isolation held: {summary['isolation']['held']}; report {(out / 'report.md').as_posix()}", flush=True)
    return 0


def combined_report(dirs) -> int:
    rows = []
    for d in dirs:
        s = json.loads((Path(d) / "summary.json").read_text("utf-8"))
        rows.append(s)
    lines = ["| Persona | Task | Model | Turns | Tool calls | Grade | Isolation held | Failures |", "|---|---|---|---|---|---|---|---|"]
    for s in rows:
        lines.append(f"| {s['persona']} | {s['task']} | {s['model']} | {s['agent']['turns']} | {s['tool_calls']} | "
                     f"{'PASS' if s['grade']['pass'] else 'FAIL'} | {'yes' if s['isolation']['held'] else 'NO'} | "
                     f"{len(s['grade']['failures'])} |")
    print("\n".join(lines))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("persona", nargs="?")
    p.add_argument("task", nargs="?")
    p.add_argument("--model")
    p.add_argument("--max-turns", type=int, default=0)
    p.add_argument("--timeout", type=int, default=120, help="minutes")
    p.add_argument("--token-file", default=str(DEFAULT_TOKEN_FILE))
    p.add_argument("--out")
    p.add_argument("--probe-only", action="store_true")
    p.add_argument("--report", nargs="+")
    args = p.parse_args(argv)
    if args.report:
        return combined_report(args.report)
    if not (args.persona and args.task):
        p.error("give PERSONA TASK")
    return run_one(args.persona, args.task, args)


if __name__ == "__main__":
    sys.exit(main())
