#!/usr/bin/env python3
"""tab: the agent's command, when the agent runs isolated from the environment (for example in a container).

It forwards the command line and standard input to the tax environment server and prints what comes back. It is
one file using only the standard library, and it holds nothing about the environment itself.

  TAB_SERVER  the server, e.g. http://host.docker.internal:8765 (default http://127.0.0.1:8765)
  TAB_TOKEN   the token `tab-admin serve` printed
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.request


def _error(message: str) -> int:
    """Errors are UTF-8 like everything else the environment prints, whatever the console code page."""
    sys.stderr.buffer.write(f"error: {message}\n".encode("utf-8", "replace"))
    sys.stderr.flush()
    return 1


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    url = os.environ.get("TAB_SERVER", "http://127.0.0.1:8765").rstrip("/") + "/agent"
    token = os.environ.get("TAB_TOKEN", "")
    # Only the commands that take standard input read it, as `tab` does: a shell driven through a pipe keeps stdin
    # open, and reading it for `tab status` would wait forever.
    takes_stdin = bool(argv) and (argv[0] in ("send", "write") or (argv[0] == "submit" and argv[1:2] in ([], ["-"]))
                                  or argv == ["reply"] or (argv[0] == "text" and len(argv) == 3))
    stdin = sys.stdin.buffer.read() if takes_stdin and sys.stdin is not None and not sys.stdin.isatty() else b""
    body = json.dumps({"token": token, "argv": argv, "stdin": base64.b64encode(stdin).decode("ascii")}).encode()
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as e:
        return _error(f"the tax environment refused the request (HTTP {e.code})")
    except (urllib.error.URLError, OSError) as e:
        return _error(f"cannot reach the tax environment at {url}: {getattr(e, 'reason', e)}")
    sys.stdout.buffer.write(base64.b64decode(data["stdout"]))
    sys.stderr.buffer.write(base64.b64decode(data["stderr"]))
    sys.stdout.flush()
    return int(data["status"])


if __name__ == "__main__":
    sys.exit(main())
