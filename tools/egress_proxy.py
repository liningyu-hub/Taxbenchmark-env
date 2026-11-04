#!/usr/bin/env python3
"""Egress proxy for a real agent's container: the only way out of its internal Docker network.

  CONNECT host:port   tunnelled only to PROXY_ALLOW_CONNECT entries (the model API, e.g. api.anthropic.com:443)
  plain HTTP          forwarded only to PROXY_ALLOW_HTTP entries (the run's tab server)
Everything else gets 403. One JSON line per request goes to PROXY_LOG, so the run keeps a record of every connection
the agent's machine tried to make. Standard library only; it runs in a stock python image.

  PROXY_ALLOW_CONNECT  comma-separated host:port, e.g. "api.anthropic.com:443"
  PROXY_ALLOW_HTTP     comma-separated host:port, e.g. "host.docker.internal:8765"
  PROXY_PORT           listen port (default 3128)
  PROXY_LOG            log file (default /log/egress.jsonl)
"""
import http.client
import json
import os
import select
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

HOP_BY_HOP = {"connection", "keep-alive", "proxy-connection", "proxy-authorization", "proxy-authenticate", "te",
              "trailer", "transfer-encoding", "upgrade"}
TUNNEL_IDLE = 600  # seconds a tunnel may sit idle (a model response can stream for minutes)


def parse_allow(text: str) -> set:
    out = set()
    for item in (text or "").split(","):
        item = item.strip()
        if item:
            host, _, port = item.rpartition(":")
            out.add((host.lower(), int(port)))
    return out


def make_proxy(host: str, port: int, allow_connect: set, allow_http: set, log_path) -> ThreadingHTTPServer:
    lock = threading.Lock()

    def log(entry: dict) -> None:
        line = json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **entry})
        with lock, open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "sandbox-egress"

        def log_message(self, *args):
            pass

        def _refuse(self, code: int, kind: str, target: str, reason: str) -> None:
            log({"kind": kind, "target": target, "allowed": False, "reason": reason})
            body = f"{reason}\n".encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def do_CONNECT(self):
            host, _, port = self.path.rpartition(":")
            try:
                key = (host.strip("[]").lower(), int(port))
            except ValueError:
                return self._refuse(403, "connect", self.path, "blocked by the sandbox egress proxy")
            if key not in allow_connect:
                return self._refuse(403, "connect", self.path, "blocked by the sandbox egress proxy")
            try:
                upstream = socket.create_connection(key, timeout=30)
            except OSError as e:
                return self._refuse(502, "connect", self.path, f"upstream unreachable: {e}")
            log({"kind": "connect", "target": self.path, "allowed": True})
            self.send_response(200, "Connection Established")
            self.end_headers()
            self.close_connection = True
            client = self.connection
            try:
                while True:
                    ready, _, failed = select.select([client, upstream], [], [client, upstream], TUNNEL_IDLE)
                    if failed or not ready:
                        return
                    for s in ready:
                        data = s.recv(65536)
                        if not data:
                            return
                        (upstream if s is client else client).sendall(data)
            except OSError:
                return
            finally:
                upstream.close()

        def _forward(self):
            url = urlsplit(self.path)
            if url.scheme != "http" or not url.hostname:
                return self._refuse(403, "http", self.path[:200], "blocked by the sandbox egress proxy")
            key = (url.hostname.lower(), url.port or 80)
            target = f"{key[0]}:{key[1]}"
            if key not in allow_http:
                return self._refuse(403, "http", target, "blocked by the sandbox egress proxy")
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
            conn = http.client.HTTPConnection(*key, timeout=900)
            try:
                conn.request(self.command, (url.path or "/") + (f"?{url.query}" if url.query else ""), body=body,
                             headers=headers)
                resp = conn.getresponse()
                data = resp.read()
            except OSError as e:
                return self._refuse(502, "http", target, f"upstream unreachable: {e}")
            finally:
                conn.close()
            log({"kind": "http", "target": target, "method": self.command, "path": url.path, "allowed": True,
                 "status": resp.status})
            self.send_response(resp.status, resp.reason)
            for k, v in resp.getheaders():
                if k.lower() not in HOP_BY_HOP and k.lower() != "content-length":
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _forward

    return ThreadingHTTPServer((host, port), Handler)


def main() -> int:
    srv = make_proxy("0.0.0.0", int(os.environ.get("PROXY_PORT", "3128")),
                     parse_allow(os.environ.get("PROXY_ALLOW_CONNECT", "")),
                     parse_allow(os.environ.get("PROXY_ALLOW_HTTP", "")),
                     os.environ.get("PROXY_LOG", "/log/egress.jsonl"))
    print(f"egress proxy on :{srv.server_address[1]}", file=sys.stderr, flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
