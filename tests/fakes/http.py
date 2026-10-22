"""Local HTTP server in a thread with declarative routes, plus a configurable file route."""

import json
import threading
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    cut_after: int | None = None  # close the connection after sending this many body bytes


def json_response(data, status: int = 200) -> Response:
    return Response(status, json.dumps(data).encode(), {"Content-Type": "application/json"})


class FakeServer:
    """`routes[(method, path)] = handler(Request) -> Response`. Every request is recorded."""

    def __init__(self):
        self.routes = {}
        self.requests: list[Request] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _handle(self):
                url = urllib.parse.urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                req = Request(
                    self.command,
                    urllib.parse.unquote(url.path),
                    dict(urllib.parse.parse_qsl(url.query)),
                    {k.lower(): v for k, v in self.headers.items()},
                    self.rfile.read(length) if length else b"",
                )
                fake.requests.append(req)
                handler = fake.routes.get((self.command, req.path)) or fake.routes.get(
                    (self.command, "*")
                )
                resp = handler(req) if handler else Response(404, b"not found")
                self.send_response(resp.status)
                headers = {"Content-Length": str(len(resp.body)), **resp.headers}
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                body = resp.body if resp.cut_after is None else resp.body[: resp.cut_after]
                self.wfile.write(body)
                if resp.cut_after is not None:
                    self.close_connection = True

            do_GET = do_POST = do_PUT = do_DELETE = _handle

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    def route(self, method: str, path: str):
        def register(handler):
            self.routes[(method, path)] = handler
            return handler

        return register


class FileRoute:
    """Serves `content` with optional Range support, connection cuts and 416 on a full .part."""

    def __init__(self, content: bytes, ranges: bool = True, cuts: list[int] | None = None):
        self.content, self.ranges = content, ranges
        self.cuts = list(cuts or [])  # successive requests are cut after these many bytes

    def __call__(self, req: Request) -> Response:
        start = 0
        header = req.headers.get("range", "")
        if self.ranges and header.startswith("bytes="):
            start = int(header[6:].split("-")[0] or 0)
            if start >= len(self.content):
                return Response(416, headers={"Content-Range": f"bytes */{len(self.content)}"})
        body = self.content[start:]
        resp = Response(200, body, {"Content-Type": "video/x-matroska"})
        if self.ranges:
            resp.headers["Accept-Ranges"] = "bytes"
            if header:
                resp.status = 206
                end = len(self.content) - 1
                resp.headers["Content-Range"] = f"bytes {start}-{end}/{len(self.content)}"
        if self.cuts:
            resp.cut_after = self.cuts.pop(0)
        return resp


def redirect(location: str) -> Response:
    return Response(302, headers={"Location": location})
