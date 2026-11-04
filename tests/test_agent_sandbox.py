"""Phase 1: the sandbox for a real agent. The egress proxy is the agent container's only way out of its internal
Docker network: it tunnels to the model API, forwards plain HTTP to the tab server, refuses everything else, and logs
every request."""
import importlib.util
import json
import socket
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tab import ROOT

_spec = importlib.util.spec_from_file_location("egress_proxy", ROOT / "tools" / "egress_proxy.py")
egress_proxy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(egress_proxy)


@pytest.fixture
def upstream():
    """Stands in for the tab server: echoes what it is sent."""
    class Echo(BaseHTTPRequestHandler):
        def do_POST(self):
            body = b"echo:" + self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    httpd = HTTPServer(("127.0.0.1", 0), Echo)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def proxy(tmp_path, upstream):
    log = tmp_path / "egress.jsonl"
    allowed = {("127.0.0.1", upstream)}
    srv = egress_proxy.make_proxy("127.0.0.1", 0, allow_connect=allowed, allow_http=allowed, log_path=log)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], log
    srv.shutdown()
    srv.server_close()


def through(proxy_port, url, data=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{proxy_port}"}))
    with opener.open(urllib.request.Request(url, data=data), timeout=10) as r:
        return r.status, r.read()


def connect(proxy_port, target):
    s = socket.create_connection(("127.0.0.1", proxy_port), timeout=10)
    s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = s.recv(4096)
        if not chunk:
            break
        head += chunk
    return s, head.split(b"\r\n", 1)[0]


def test_parse_allow_list():
    assert egress_proxy.parse_allow("api.anthropic.com:443, Host.Docker.Internal:8765") == {
        ("api.anthropic.com", 443), ("host.docker.internal", 8765)}
    assert egress_proxy.parse_allow("") == set()


def test_proxy_forwards_allowed_http(proxy, upstream):
    port, _ = proxy
    assert through(port, f"http://127.0.0.1:{upstream}/agent", data=b"hi") == (200, b"echo:hi")


def test_proxy_refuses_other_http(proxy, upstream):
    port, _ = proxy
    for url in ("http://example.com/", f"http://127.0.0.1:{upstream + 1}/agent", "http://169.254.169.254/latest/"):
        with pytest.raises(urllib.error.HTTPError) as e:
            through(port, url, data=b"x")
        assert e.value.code == 403, url


def test_proxy_tunnels_only_allowed_connect(proxy, upstream):
    port, _ = proxy
    s, status = connect(port, f"127.0.0.1:{upstream}")
    assert b" 200 " in status + b" "
    s.sendall(f"POST /agent HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok".encode())
    reply = b""
    while True:
        chunk = s.recv(4096)
        if not chunk:
            break
        reply += chunk
    s.close()
    assert reply.endswith(b"echo:ok")
    for target in ("github.com:443", "api.anthropic.com:443", f"127.0.0.1:{upstream + 1}", "nonsense"):
        s, status = connect(port, target)
        s.close()
        assert b" 403 " in status + b" ", target


def test_proxy_logs_every_request(proxy, upstream):
    port, log = proxy
    through(port, f"http://127.0.0.1:{upstream}/agent", data=b"a")
    with pytest.raises(urllib.error.HTTPError):
        through(port, "http://example.com/", data=b"b")
    s, _ = connect(port, "github.com:443")
    s.close()
    entries = [json.loads(l) for l in log.read_text("utf-8").splitlines()]
    assert [(e["kind"], e["allowed"]) for e in entries] == [("http", True), ("http", False), ("connect", False)]
    assert entries[2]["target"] == "github.com:443" and all("at" in e for e in entries)


_spec2 = importlib.util.spec_from_file_location("agent_run", ROOT / "tools" / "agent_run.py")
agent_run = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(agent_run)


def test_tab_subcommands_are_read_from_shell_commands():
    assert agent_run.tab_subcommands("tab task") == ["task"]
    assert agent_run.tab_subcommands("tab read Email/a.md | head -40; tab inbox") == ["read", "inbox"]
    assert agent_run.tab_subcommands("cat draft.txt | tab send --to a@b.c --subject s") == ["send"]
    assert agent_run.tab_subcommands("python3 make_table.py --tab x") == []


def test_transcript_is_parsed_into_tool_calls(tmp_path):
    events = [
        {"type": "system", "subtype": "init", "model": "m", "claude_code_version": "v", "tools": ["Bash"]},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Looking."},
                                                      {"type": "tool_use", "id": "t1", "name": "Bash",
                                                       "input": {"command": "tab task"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                                  "content": [{"type": "text", "text": "# Task"}]}]}},
        {"type": "result", "subtype": "success", "num_turns": 2, "result": "Done."},
    ]
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\nnot json\n", encoding="utf-8")
    t = agent_run.parse_transcript(path)
    assert t["init"]["model"] == "m" and t["result"]["result"] == "Done." and t["texts"] == ["Looking."]
    assert [(c["tool"], c["input"]["command"], c["result"]) for c in t["calls"]] == [("Bash", "tab task", "# Task")]


def _sandbox_ready():
    import shutil
    import subprocess
    if shutil.which("docker") is None:
        return False
    try:
        out = subprocess.run(["docker", "images", "-q", agent_run.IMAGE], capture_output=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return bool(out.strip())


@pytest.mark.skipif(not _sandbox_ready(), reason=f"needs Docker and the {agent_run.IMAGE} image")
def test_agent_container_is_sealed(tmp_path):
    """No agent and no credentials: the probe a curious agent would run, inside the real sandbox."""
    import os
    import subprocess
    import sys
    out = tmp_path / "probe"
    env = dict(os.environ)
    env.pop("TAB_HOME", None)
    p = subprocess.run([sys.executable, str(ROOT / "tools" / "agent_run.py"), "S000005", "C3", "--probe-only",
                        "--out", str(out)], capture_output=True, timeout=600, env=env)
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")[-800:]
    info = json.loads((out / "run_info.json").read_text("utf-8"))
    for probe in (info["probe_before"], info["probe_after"]):
        assert probe["hidden_files"] == [] and probe["tab_works"] and probe["escape_refused"]
        assert "403" in probe["github_via_proxy"] and "403" in probe["pypi_via_proxy"]
        assert probe["model_api_via_proxy"].startswith("HTTP")
        for key in ("internet_direct", "tab_server_direct", "raw_socket_out"):
            assert probe[key].startswith("blocked"), (key, probe[key])
    egress = [json.loads(l) for l in (out / "egress.jsonl").read_text("utf-8").splitlines()]
    assert {e["target"] for e in egress if e["allowed"]} == {"api.anthropic.com:443",
                                                             f"host.docker.internal:{info['port']}"}
    assert {e["target"] for e in egress if not e["allowed"]} == {"github.com:443", "pypi.org:443"}
