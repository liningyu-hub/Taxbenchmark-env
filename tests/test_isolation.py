"""M1: agent isolation. The agent talks to the environment only through an HTTP endpoint that runs agent commands;
its own machine (a container) holds nothing but the one-file client."""
import base64
import io
import json
import os
import shutil
import subprocess
import threading
import urllib.error
import urllib.request

import pytest

from conftest import agent, needs_sources
from tab import ROOT, server

CLIENT = ROOT / "client" / "tab_client.py"
TOKEN = "test-token-123"


@pytest.fixture
def endpoint(initialized_env):
    httpd = server.make_server("127.0.0.1", 0, TOKEN)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def call(url, argv, stdin=b"", token=TOKEN):
    body = json.dumps({"token": token, "argv": argv, "stdin": base64.b64encode(stdin).decode()}).encode()
    req = urllib.request.Request(f"{url}/agent", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    return data["status"], base64.b64decode(data["stdout"]).decode(), base64.b64decode(data["stderr"]).decode()


def client(url, *argv, stdin=b"", token=TOKEN):
    """Runs the real client file in a subprocess, as an agent's shell would."""
    env = dict(os.environ, TAB_SERVER=url, TAB_TOKEN=token)
    p = subprocess.run([os.sys.executable, str(CLIENT), *argv], env=env, input=stdin, capture_output=True, timeout=60)
    return p.returncode, p.stdout.decode("utf-8"), p.stderr.decode("utf-8")


@needs_sources
def test_client_server_matches_in_process_output(endpoint):
    for argv in (["task"], ["ls", "Portals"], ["read", "Portals/IRS_Online_Account/Account_Summary.md"],
                 ["read", "no/such/file.md"]):
        assert client(endpoint, *argv) == agent(*argv), argv
    status, out, _ = client(endpoint, "write", "Documents/Taxes/2025/n.md", stdin="± ok\n".encode())
    assert status == 0 and "Saved Documents/Taxes/2025/n.md" in out


@needs_sources
def test_server_requires_the_token(endpoint):
    with pytest.raises(urllib.error.HTTPError) as e:
        call(endpoint, ["task"], token="wrong")
    assert e.value.code == 403
    status, _, err = client(endpoint, "task", token="")
    assert status == 1 and "refused" in err


@needs_sources
def test_server_refuses_admin_commands(endpoint):
    for argv in (["init", "S000005", "--force"], ["verify"], ["check-key"], ["log"], ["reset"]):
        status, out, err = call(endpoint, argv)
        assert status == 1 and "Unknown command" in err and out == "", argv
    for path in ("/", "/admin", "/agent/../admin"):
        req = urllib.request.Request(endpoint + path, data=b"{}")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=10)
        assert e.value.code == 404


@needs_sources
def test_server_keeps_path_confinement(endpoint):
    for path in ("../run.json", "../../personas/S000005/hidden/answer_key.json", "..\\..\\INTERNAL_NOTES.md",
                 str(ROOT / "personas" / "S000005" / "hidden" / "answer_key.json")):
        status, _, err = call(endpoint, ["read", path])
        assert status == 1 and re_outside(err), (path, err)


def re_outside(err):
    return "outside the environment" in err or "inside the environment" in err


def test_agent_container_command_mounts_only_the_client(home):
    from conftest import admin
    status, out, _ = admin("agent-container", "--port", "8800", "--token", "abc", "--print")
    assert status == 0
    mounts = [line for line in out.split() if ":/opt/tab/" in line or line.startswith(("-v", "--volume"))]
    assert any(str(CLIENT.as_posix()) in m for m in mounts)
    assert "personas" not in out and "runtime" not in out and str(ROOT.as_posix()) + "/src" not in out
    assert "TAB_SERVER=http://host.docker.internal:8800" in out and "TAB_TOKEN=abc" in out


def test_client_reports_an_unreachable_server():
    status, out, err = client("http://127.0.0.1:9", "task")
    assert status == 1 and out == "" and "cannot reach the tax environment" in err


def _docker_ready():
    if shutil.which("docker") is None:
        return False
    try:
        info = subprocess.run(["docker", "image", "inspect", "python:3.12-slim"], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return info.returncode == 0


@needs_sources
@pytest.mark.skipif(not _docker_ready(), reason="Docker engine or the python:3.12-slim image not available")
def test_container_has_no_hidden_files(endpoint):
    port = endpoint.rsplit(":", 1)[1]
    script = ("python3 /opt/tab/tab_client.py task | head -1; echo ---; "
              "find / -xdev \\( -name 'answer_key*' -o -name 'INTERNAL_NOTES*' -o -name 'simulated_user*' "
              "-o -name 'actions.jsonl' -o -name 'persona.json' \\) 2>/dev/null; echo ---; "
              "python3 /opt/tab/tab_client.py read ../../personas/S000005/hidden/answer_key.json; echo \"exit=$?\"")
    cmd = ["docker", "run", "--rm", "--add-host", "host.docker.internal:host-gateway",
           "-e", f"TAB_SERVER=http://host.docker.internal:{port}", "-e", f"TAB_TOKEN={TOKEN}",
           "-v", f"{CLIENT}:/opt/tab/tab_client.py:ro", "python:3.12-slim", "sh", "-c", script]
    p = subprocess.run(cmd, capture_output=True, timeout=120, env=dict(os.environ, MSYS_NO_PATHCONV="1"))
    first, found, refused = p.stdout.decode("utf-8").split("---\n")
    assert first.startswith("# Task: prepare Thomas Gonzalez's 2025 tax returns"), p.stderr.decode()
    assert found.strip() == "", "no hidden or run files exist inside the agent's container"
    assert "exit=1" in refused and "outside the environment" in p.stderr.decode("utf-8")


@needs_sources
def test_client_reads_stdin_only_for_commands_that_take_it(endpoint):
    """A harness that drives the agent's shell through a pipe leaves stdin open; `tab status` must not wait on it."""
    env = dict(os.environ, TAB_SERVER=endpoint, TAB_TOKEN=TOKEN)
    p = subprocess.Popen([os.sys.executable, str(CLIENT), "status"], env=env, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        p.wait(timeout=15)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
        pytest.fail("the client waited for standard input")
    finally:
        p.stdin.close()
    assert p.returncode == 0 and b"Today's date" in p.stdout.read()
    p.stdout.close()
    p.stderr.close()
    assert client(endpoint, "send", "--to", "a@example.com", "--subject", "s", stdin=b"hello\n")[0] == 0


@needs_sources
def test_server_answers_a_wrong_path_even_with_a_body(endpoint):
    """The server reads the request before refusing it; otherwise the client may get a connection reset, not a 404."""
    for _ in range(3):
        req = urllib.request.Request(endpoint + "/admin", data=b"x" * 2_000_000)
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=30)
        assert e.value.code == 404


@needs_sources
def test_reply_goes_through_the_client(endpoint):
    assert client(endpoint, "reply", stdin=b"An answer, from standard input.\n")[0] == 0
    env = dict(os.environ, TAB_SERVER=endpoint, TAB_TOKEN=TOKEN)
    p = subprocess.Popen([os.sys.executable, str(CLIENT), "reply", "A one-line answer."], env=env,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        p.wait(timeout=15)  # with the message on the command line, the client must not wait for stdin
    finally:
        p.stdin.close()
    assert p.returncode == 0
    p.stdout.close()
    p.stderr.close()
