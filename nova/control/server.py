"""A thin HTTP transport for the Control API.

Built on the standard library so the platform layer keeps its zero-dependency surface.
The handlers in :mod:`nova.control.api` hold all the behaviour; this module only moves
bytes, which is what makes swapping in a production server later a contained change
rather than a rewrite.

Two safety properties, both tested:

**Read-only.** Only GET and HEAD are served. Every other method is refused with 405
before any handler runs, so no write path can be reached even by accident.

**No accidental exposure.** Binding to anything other than loopback requires an explicit
token. A control plane that lists a customer's agents and work must not become
world-readable because someone passed ``--host 0.0.0.0`` while debugging.
"""

from __future__ import annotations

import hmac
import json
import logging
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from nova.control.api import ControlAPI
from nova.errors import NovaError

logger = logging.getLogger("nova.control")

STATIC_DIR = Path(__file__).parent / "static"
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

#: Served on every response. The dashboard is same-origin and needs nothing exotic; a
#: strict policy here keeps an injected string in a task title from becoming script.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "Cache-Control": "no-store",
}


class _Handler(BaseHTTPRequestHandler):
    server_version = "nova-control"
    sys_version = ""

    # Injected by :func:`serve`.
    api: ControlAPI
    token: str = ""

    # -- plumbing -------------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 — stdlib signature
        logger.info("%s %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, payload) -> None:
        self._send(status, json.dumps(payload, indent=2).encode("utf-8"), "application/json")

    def _authorized(self) -> bool:
        """Constant-time bearer check. No token configured means loopback-only serving."""
        if not self.token:
            return True
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        return hmac.compare_digest(header[len(prefix) :], self.token)

    # -- methods --------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 — stdlib signature
        self._handle()

    def do_HEAD(self) -> None:  # noqa: N802 — stdlib signature
        self._handle()

    def _reject_write(self) -> None:
        self._send_json(
            405,
            {"error": {"status": 405, "message": "the control API is read-only in this release"}},
        )

    do_POST = do_PUT = do_PATCH = do_DELETE = _reject_write  # noqa: N815 — stdlib names

    def _handle(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if not self._authorized():
            self._send_json(401, {"error": {"status": 401, "message": "missing or invalid token"}})
            return

        if path.startswith("/platform/"):
            query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            try:
                response = self.api.handle(path, query)
            except NovaError as exc:
                self._send_json(500, {"error": {"status": 500, "message": str(exc)}})
                return
            self._send_json(response.status, response.body)
            return

        self._serve_static(path)

    def _serve_static(self, path: str) -> None:
        """Serve the dashboard. Paths are resolved and confined to the static directory."""
        if path == "/favicon.ico":
            # Browsers request this unprompted. A tenant favicon is applied by the page
            # itself from identity; answering 204 keeps the access log honest.
            self._send(204, b"", "image/x-icon")
            return
        relative = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (STATIC_DIR / relative).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._send_json(404, {"error": {"status": 404, "message": "not found"}})
            return
        if not target.is_file():
            self._send_json(404, {"error": {"status": 404, "message": "not found"}})
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type == "application/javascript":
            content_type += "; charset=utf-8"
        self._send(200, target.read_bytes(), content_type)


def build_server(
    api: ControlAPI, *, host: str = "127.0.0.1", port: int = 8787, token: str = ""
) -> ThreadingHTTPServer:
    """Construct the server without starting it. Refuses unsafe binds.

    Raises :class:`~nova.errors.NovaError` when asked to bind a non-loopback interface
    without a token — the failure has to happen here rather than after the socket is
    already accepting connections.
    """
    if host not in LOOPBACK_HOSTS and not token:
        raise NovaError(
            f"refusing to bind {host} without a token: the control API exposes this "
            "tenant's agents and work. Pass a token, or bind 127.0.0.1 and use an SSH "
            "tunnel."
        )

    handler = type("_BoundHandler", (_Handler,), {"api": api, "token": token})
    return ThreadingHTTPServer((host, port), handler)


def serve(
    api: ControlAPI,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    token: str = "",
    ready: Optional[callable] = None,
) -> None:
    """Serve until interrupted. ``ready`` is called with the bound address once listening."""
    server = build_server(api, host=host, port=port, token=token)
    bound_host, bound_port = server.server_address[:2]
    if ready is not None:
        ready(f"http://{bound_host}:{bound_port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
