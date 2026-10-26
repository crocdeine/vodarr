import pytest
from fastapi.testclient import TestClient

from vodarr import auth
from vodarr.app import create_app

PASSWORD = "correct horse battery"
ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture(autouse=True)
def fresh_throttle(monkeypatch):
    monkeypatch.setattr(auth, "throttle", auth.Throttle())


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "config", hosts={"testserver"})) as c:
        c.post(
            "/api/auth/setup",
            json={"setupCode": "test-setup-code", "username": "admin", "password": PASSWORD},
            headers=ORIGIN,
        )
        yield c


def test_unknown_host_is_421(client):
    """AC-AUTH-08"""
    r = client.get("/api/health", headers={"Host": "evil.example"})
    assert r.status_code == 421
    assert r.json()["error"]["code"] == "http.host_not_allowed"
    assert client.get("/api/health", headers={"Host": "testserver:8863"}).status_code == 200


def test_default_hosts_and_env(monkeypatch):
    """AC-AUTH-08"""
    from vodarr import app as app_module

    monkeypatch.setenv("VODARR_ALLOWED_HOSTS", "vodarr, Vodarr.LAN ")
    hosts = app_module.allowed_hosts()
    assert {"localhost", "127.0.0.1", "vodarr", "vodarr.lan"} <= hosts


def test_cookie_mutation_needs_same_origin_and_json(client):
    """AC-AUTH-09"""
    assert client.post("/api/auth/api-key").status_code == 403  # no Origin
    assert (
        client.post("/api/auth/api-key", headers={"Origin": "http://evil.example"}).status_code
        == 403
    )
    r = client.post(
        "/api/auth/api-key/reveal",
        content=b"password=x",
        headers={**ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "http.csrf"
    assert client.post("/api/auth/api-key", headers=ORIGIN, json={}).status_code == 200


def test_api_key_calls_skip_csrf(client):
    """AC-AUTH-09: API key calls carry no cookie."""
    key = client.post(
        "/api/auth/api-key/reveal", json={"password": PASSWORD}, headers=ORIGIN
    ).json()["apiKey"]
    client.cookies.clear()
    assert client.post("/api/auth/api-key", headers={"X-Api-Key": key}).status_code == 200


def test_body_over_1_mib_is_413(client):
    """AC-AUTH-10"""
    r = client.post(
        "/api/auth/login",
        content=b"x" * (1024 * 1024 + 1),
        headers={**ORIGIN, "Content-Type": "application/json"},
    )
    assert r.status_code == 413


def test_chunked_body_refused(client):
    def gen():
        yield b'{"username": "a", '
        yield b'"password": "b"}'

    r = client.post(
        "/api/auth/login", content=gen(), headers={**ORIGIN, "Content-Type": "application/json"}
    )
    assert r.status_code == 411


def test_health_is_public_and_bare(client):
    """AC-AUTH-11"""
    client.cookies.clear()
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_security_headers_and_docs_disabled(client):
    r = client.get("/api/health")
    csp = r.headers["content-security-policy"]
    assert (
        csp.startswith("default-src 'self'; img-src 'self' https: http: data:")
        and "base-uri 'none'" in csp
    )
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_errors_have_one_shape(client):
    r = client.post("/api/auth/login", json={"username": "admin"}, headers=ORIGIN)
    assert r.status_code == 422
    assert r.json() == {"error": {"code": "http.invalid_request", "params": {"fields": "password"}}}


def test_internal_error_hides_details(tmp_path):
    app = create_app(tmp_path / "config", hosts={"testserver"})

    @app.get("/boom")
    def boom():
        raise RuntimeError("http://user:secret@h")

    app.router.routes.insert(0, app.router.routes.pop())  # before the static mount on "/"

    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/boom")
    assert r.status_code == 500
    assert r.json() == {"error": {"code": "internal", "params": {}}}


def test_404_405_and_guard_errors_use_the_error_shape(client):
    """Starlette's own errors and early guard answers."""
    r = client.get("/api/nope")
    assert r.status_code == 404 and r.json() == {"error": {"code": "http.not_found", "params": {}}}
    r = client.post("/api/health", headers=ORIGIN, json={})
    assert r.status_code == 405 and r.json()["error"]["code"] == "http.method_not_allowed"
    assert "allow" in r.headers
    r = client.get("/api/health", headers={"Host": "evil.example"})
    assert r.headers["x-frame-options"] == "DENY"


def test_interface_is_served_with_security_headers(client):
    """AC-UI-01 base: the static interface, same origin, same headers."""
    r = client.get("/")
    assert r.status_code == 200 and '<script type="module" src="/app.js">' in r.text
    assert r.headers["x-frame-options"] == "DENY"
    assert (
        client.get("/app.js")
        .headers["content-type"]
        .startswith(("text/javascript", "application/javascript"))
    )
    assert client.get("/i18n/fr.json").json()["nav.settings"] == "Réglages"


def test_dotfiles_are_never_served(client):
    assert client.get("/.DS_Store").status_code == 404
    assert client.get("/styles/.hidden").status_code == 404


def test_message_params_named_like_error_arguments_are_kept():
    """A provider's HTTP status travels as the `status` param (source.http_error)."""
    import json

    from vodarr.app import error

    r = error(502, "source.http_error", None, status=503, code="x", headers="y")
    assert r.status_code == 502
    assert json.loads(r.body)["error"]["params"] == {"status": 503, "code": "x", "headers": "y"}


def test_unknown_host_gets_a_readable_page_in_a_browser(client):
    """0.2: the 421 JSON confused users reaching VODarr through a new name (Tailscale...)."""
    browser = {"Host": "<b>nas.example</b>", "Accept": "text/html,application/xhtml+xml"}
    r = client.get("/", headers=browser)
    assert r.status_code == 421 and r.headers["content-type"].startswith("text/html")
    assert "VODARR_ALLOWED_HOSTS" in r.text and "&lt;b&gt;nas.example&lt;/b&gt;" in r.text
    assert "<b>nas" not in r.text  # the Host header is escaped
    assert "content-security-policy" in r.headers
    api = client.get("/api/health", headers=browser)
    assert api.json()["error"]["code"] == "http.host_not_allowed"  # the API keeps its JSON
