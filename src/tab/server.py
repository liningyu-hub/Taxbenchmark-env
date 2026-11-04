"""The agent's only way into the environment when it runs isolated (a container): an HTTP endpoint that runs
agent commands and nothing else.

POST /agent  {"token": str, "argv": [str], "stdin": base64}  ->  {"status": int, "stdout": base64, "stderr": base64}

Only `cli.agent` is reachable, so operator commands (init, verify, check-key, log...) and every hidden file stay on
the server's side. Requests are handled one at a time, in order, like a single agent's shell. A shared token keeps
other local processes out.
"""
import base64
import binascii
import io
import json
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer

MAX_BODY = 4 * 1024 * 1024


def make_handler(token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "tab-environment"

        def log_message(self, *args):  # no request log on the operator's console
            pass

        def _reply(self, code: int, payload=None):
            body = json.dumps(payload or {}).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._reply(404, {"error": "not found"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            if self.path != "/agent":
                if 0 < length <= MAX_BODY:
                    self.rfile.read(length)  # read it first: closing on an unread body resets the client's connection
                return self._reply(404, {"error": "not found"})
            if length <= 0 or length > MAX_BODY:
                return self._reply(413 if length > MAX_BODY else 400, {"error": "bad request size"})
            try:
                req = json.loads(self.rfile.read(length))
                argv = req["argv"]
                stdin = base64.b64decode(req.get("stdin") or "", validate=True)
                if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
                    raise ValueError("argv must be a list of strings")
            except (ValueError, KeyError, TypeError, binascii.Error):
                return self._reply(400, {"error": "bad request"})
            if not secrets.compare_digest(str(req.get("token") or ""), token):
                return self._reply(403, {"error": "refused"})
            from . import cli
            out, err = io.BytesIO(), io.BytesIO()
            status = cli.agent(argv, stdin=io.BytesIO(stdin), out=out, err=err)
            self._reply(200, {"status": status, "stdout": base64.b64encode(out.getvalue()).decode("ascii"),
                              "stderr": base64.b64encode(err.getvalue()).decode("ascii")})

    return Handler


def make_server(host: str, port: int, token: str) -> HTTPServer:
    if not token:
        raise ValueError("a token is required")
    return HTTPServer((host, port), make_handler(token))
