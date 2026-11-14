"""The example App's web server, on port 3000.

`/health` passes unless the image was built with HEALTH=fail, `/version` names the image, so a
test can tell which version is live, and `/env/<NAME>` answers the container's variable NAME, 404
when it has none. `/note` keeps a note in the App's named volume: PUT one, then GET it back, so a
test can tell whether data written earlier is still there.
"""

import os
import signal
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import FrameType

HEALTHY = os.environ.get("EXAMPLE_HEALTH") != "fail"
VERSION = os.environ.get("EXAMPLE_VERSION", "")
NOTE = Path("/data/note")


class Handler(BaseHTTPRequestHandler):
    def answer(self, status: int, body: str = "") -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/health":
            if HEALTHY:
                self.answer(200, "ok")
            else:
                self.answer(503, "failing on purpose")
        elif self.path == "/version":
            self.answer(200, VERSION)
        elif self.path == "/note":
            try:
                self.answer(200, NOTE.read_text())
            except FileNotFoundError:
                # No note yet.
                self.answer(200)
        elif self.path.startswith("/env/"):
            value = os.environ.get(self.path.removeprefix("/env/"))
            if value is None:
                self.answer(404)
            else:
                self.answer(200, value)
        else:
            self.answer(404)

    def do_PUT(self) -> None:
        if self.path == "/note":
            length = int(self.headers.get("Content-Length", "0"))
            NOTE.write_bytes(self.rfile.read(length))
            self.answer(200)
        else:
            self.answer(404)

    def log_message(self, format: str, *args: object) -> None:
        """Logs nothing: the tests read the App only through its answers."""


def stop(_signum: int, _frame: FrameType | None) -> None:
    sys.exit(0)


# As PID 1, Python ignores SIGTERM unless it has a handler, so without this Docker would wait and
# then kill it.
signal.signal(signal.SIGTERM, stop)
ThreadingHTTPServer(("0.0.0.0", 3000), Handler).serve_forever()  # noqa: S104
