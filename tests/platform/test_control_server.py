"""The HTTP transport's safety properties, and the dashboard's boundary."""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from nova.control import ControlAPI
from nova.control.server import STATIC_DIR, build_server
from nova.errors import NovaError

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def api(bundle, runtime):
    return ControlAPI(bundle, runtime)


@pytest.fixture
def live(api):
    """A running server on an ephemeral port, torn down after the test."""
    server = build_server(api, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(url: str, *, method: str = "GET", token: str = "") -> tuple[int, bytes, dict]:
    req = urllib.request.Request(url, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


# -- bind safety -------------------------------------------------------------


def test_refuses_non_loopback_bind_without_a_token(api):
    """The control plane lists a customer's agents and work. It must not leak by default."""
    with pytest.raises(NovaError, match="refusing to bind"):
        build_server(api, host="0.0.0.0", port=0)


def test_a_loopback_bind_needs_no_credentials(api):
    """Superseded the old single-token contract: a loopback caller already has shell
    access to everything the control plane reports. See test_control_auth.py."""
    build_server(api, host="127.0.0.1", port=0).server_close()


# -- the method surface ------------------------------------------------------
#
# Writes arrived in Phase 8, so "no method but GET" is no longer the invariant. What
# replaces it is narrower and worth more: POST reaches only the routes declared in
# WRITE_ROUTES, and the other write verbs are still refused before a handler runs.
# The write path itself is tested in test_control_write.py.


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_unused_write_methods_are_refused(live, method):
    status, body, _ = request(f"{live}/platform/v1/agents", method=method)
    assert status == 405
    assert "only GET, HEAD and POST" in json.loads(body)["error"]["message"]


def test_a_read_route_cannot_be_posted_to(live):
    """`/agents` is a read. POSTing to it must not find a handler by accident — an
    undeclared write path is unroutable, not merely admin-only."""
    status, _, _ = request(f"{live}/platform/v1/agents", method="POST")
    assert status in (404, 415)


# -- auth --------------------------------------------------------------------


def test_a_token_is_required_for_a_remote_caller(api, tmp_path):
    """The single shared ``--token`` is gone; callers are named principals with roles.
    ``behind_tls_proxy`` is what makes this loopback socket behave as a remote one —
    see test_control_auth.py for why that is the right switch."""
    from nova.control.auth import PrincipalStore, hash_token, new_token

    secret = new_token()
    path = tmp_path / "p.yaml"
    path.write_text(
        "principals:\n  - name: ops\n    role: admin\n"
        f"    token_sha256: {hash_token(secret)}\n",
        encoding="utf-8",
    )
    server = build_server(
        api, host="127.0.0.1", port=0,
        principals=PrincipalStore.load(path), behind_tls_proxy=True,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    base = f"http://{host}:{port}"
    try:
        assert request(f"{base}/platform/v1/health")[0] == 401
        assert request(f"{base}/platform/v1/health", token="wrong")[0] == 401
        assert request(f"{base}/platform/v1/health", token=secret)[0] == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# -- serving -----------------------------------------------------------------


def test_serves_the_api(live):
    status, body, headers = request(f"{live}/platform/v1/health")
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body)["platform"]["tenant_id"] == "acme"


def test_serves_the_dashboard(live):
    status, body, headers = request(f"{live}/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"<title>" in body


def test_security_headers_are_present(live):
    _, _, headers = request(f"{live}/")
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in headers["Content-Security-Policy"]


def test_favicon_is_answered_quietly(live):
    """Browsers request it unprompted; a 404 on every page load is noise, not signal."""
    assert request(f"{live}/favicon.ico")[0] == 204


@pytest.mark.parametrize("attempt", ["/../nova/cli.py", "/%2e%2e/%2e%2e/etc/passwd", "/../../errors.py"])
def test_static_serving_cannot_escape_its_directory(live, attempt):
    status, _, _ = request(f"{live}{attempt}")
    assert status in (400, 404)


def test_unknown_static_path_is_404(live):
    assert request(f"{live}/nope.js")[0] == 404


# -- the control-plane boundary ----------------------------------------------


def test_dashboard_only_ever_calls_the_control_api():
    """The dashboard must never reach a runtime endpoint. This is the boundary in §5."""
    source = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    fetches = re.findall(r"fetch\(([^)]*)\)", source)
    assert fetches, "expected the dashboard to call the API"
    for call in fetches:
        assert "API +" in call, f"dashboard fetch does not go through the API prefix: {call}"
    assert 'const API = "/platform/v1"' in source


def _js_without_comments(source: str) -> str:
    """Strip /* */ and // comments. Prose may mention a footgun; code may not use it."""
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*//.*$", "", source)


def test_dashboard_never_injects_api_data_as_markup():
    """Task titles and agent names are customer-controlled strings, never markup."""
    code = _js_without_comments((STATIC_DIR / "app.js").read_text(encoding="utf-8"))
    assert "innerHTML" not in code
    assert "insertAdjacentHTML" not in code
    assert "document.write" not in code


def test_dashboard_has_no_external_dependencies():
    """No build step, no CDN: the control plane must work in an isolated network."""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert not re.search(r'(src|href)\s*=\s*["\']https?://', html), "external resource referenced"
    assert "cdn" not in html.lower()
