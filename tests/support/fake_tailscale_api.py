"""A fake Tailscale API, for `deadlift app add` and `app remove`: the Services endpoints only, on
the host's loopback, so no test reaches the real API or needs a real credential. Point deadlift at
it with DEADLIFT_TAILSCALE_API."""

import json
import re
import threading
import urllib.parse
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast, override

FAKE_API_KEY = "tskey-api-fake"
"""The credential the fake accepts, as TAILSCALE_API_KEY."""

_SERVICE_PATH = re.compile(r"/api/v2/tailnet/-/vip-services/(svc:[a-z0-9-]+)")
_ADDRS = ["100.100.0.1"]


@dataclass(frozen=True)
class Service:
    """A Tailscale Service as the tailnet has it."""

    name: str
    ports: list[str]
    tags: list[str]
    comment: str | None = None

    def as_json(self) -> dict[str, object]:
        shown: dict[str, object] = {"name": self.name, "ports": self.ports, "tags": self.tags}
        if self.comment is not None:
            shown["comment"] = self.comment
        return shown


@dataclass(frozen=True)
class ApiRequest:
    method: str
    path: str
    authorization: str | None
    body: object = None


@dataclass
class FakeTailscaleApi:
    url: str
    services: dict[str, Service] = field(default_factory=dict[str, Service])
    """The Services as the tailnet has them now, by name."""
    requests: list[ApiRequest] = field(default_factory=list[ApiRequest])
    """Every request so far, in order."""


def _service(body: object) -> Service | None:
    if not isinstance(body, dict):
        return None
    fields = cast(dict[str, object], body)
    name, ports, tags, comment = (fields.get(k) for k in ("name", "ports", "tags", "comment"))
    if not (isinstance(name, str) and isinstance(ports, list) and isinstance(tags, list)):
        return None
    ports_, tags_ = cast(list[object], ports), cast(list[object], tags)
    if not all(isinstance(p, str) for p in [*ports_, *tags_]):
        return None
    if comment is not None and not isinstance(comment, str):
        return None
    return Service(name, cast(list[str], ports_), cast(list[str], tags_), comment)


def _handler(api: FakeTailscaleApi) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int, body: object = None) -> None:
            data = json.dumps({} if body is None else body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _answer(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            text = self.rfile.read(length).decode() if length else ""
            path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
            body: object = json.loads(text) if text else None
            authorization = self.headers.get("Authorization")
            api.requests.append(ApiRequest(self.command, path, authorization, body))
            if authorization != f"Bearer {FAKE_API_KEY}":
                return self._reply(401, {"message": "API token invalid"})
            match = _SERVICE_PATH.fullmatch(path)
            if not match:
                return self._reply(404, {"message": "not found"})
            name = match[1]
            match self.command:
                case "GET":
                    found = api.services.get(name)
                    if found is None:
                        return self._reply(404, {"message": "not found"})
                    return self._reply(200, {**found.as_json(), "addrs": _ADDRS})
                case "PUT":
                    service = _service(body)
                    if service is None or service.name != name:
                        return self._reply(400, {"message": "name mismatch"})
                    api.services[name] = service
                    return self._reply(200, {**service.as_json(), "addrs": _ADDRS})
                case "DELETE":
                    if api.services.pop(name, None) is None:
                        return self._reply(404, {"message": "not found"})
                    return self._reply(200)
                case _:
                    return self._reply(405, {"message": "method not allowed"})

        do_GET = do_PUT = do_DELETE = do_POST = do_PATCH = _answer

        @override
        def log_message(self, format: str, *args: object) -> None:
            pass

    return Handler


@contextmanager
def fake_tailscale_api() -> Generator[FakeTailscaleApi]:
    """Serves a fake Tailscale API on a free loopback port until the block exits."""
    api = FakeTailscaleApi(url="")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(api))
    api.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield api
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
