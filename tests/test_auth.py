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
    app = create_app(tmp_path / "config", hosts={"testserver"})
    with TestClient(app) as c:
        yield c


def setup_admin(client):
    r = client.post(
        "/api/auth/setup",
        json={"setupCode": "test-setup-code", "username": "admin", "password": PASSWORD},
        headers=ORIGIN,
    )
    assert r.status_code == 201, r.text
    return r


def test_before_setup_everything_is_401(client):
    """AC-AUTH-01"""
    assert client.get("/api/auth/status").json() == {"setupRequired": True, "authenticated": False}
    r = client.get("/api/auth/api-key")
    assert r.status_code == 401
    assert r.json() == {"error": {"code": "auth.setup_required", "params": {}}}
    assert client.get("/api/health").status_code == 200


def test_setup_once_then_409(client):
    """AC-AUTH-02"""
    setup_admin(client)
    r = client.post(
        "/api/auth/setup",
        json={"setupCode": "test-setup-code", "username": "x", "password": PASSWORD},
        headers=ORIGIN,
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "auth.already_setup"


def test_password_min_length_and_scrypt_hash(client, tmp_path):
    """AC-AUTH-03"""
    r = client.post(
        "/api/auth/setup",
        json={"setupCode": "test-setup-code", "username": "admin", "password": "short"},
        headers=ORIGIN,
    )
    assert r.status_code == 422
    assert r.json()["error"] == {"code": "auth.password_too_short", "params": {"min": 12}}
    setup_admin(client)
    from vodarr.core import open_db

    stored = open_db(tmp_path / "config").execute("SELECT password_hash FROM admin").fetchone()[0]
    assert stored.startswith("scrypt$16384$8$1$") and PASSWORD not in stored
    assert auth.verify_password(PASSWORD, stored) and not auth.verify_password("nope", stored)


def test_login_cookie_flags_and_logout(client):
    """AC-AUTH-04"""
    setup_admin(client)
    client.cookies.clear()
    r = client.post(
        "/api/auth/login", json={"username": "admin", "password": PASSWORD}, headers=ORIGIN
    )
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert "vodarr_session=" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert client.get("/api/auth/api-key").status_code == 200
    token = client.cookies.get("vodarr_session")
    assert client.post("/api/auth/logout", headers=ORIGIN, json={}).status_code == 200
    client.cookies.set("vodarr_session", token)  # replaying the old cookie
    assert client.get("/api/auth/api-key").status_code == 401


def test_wrong_credentials_then_lockout(client):
    """AC-AUTH-05"""
    setup_admin(client)
    client.cookies.clear()
    bad = {"username": "admin", "password": "wrong password!"}
    for _ in range(5):
        assert client.post("/api/auth/login", json=bad, headers=ORIGIN).status_code == 401
    r = client.post(
        "/api/auth/login", json={"username": "admin", "password": PASSWORD}, headers=ORIGIN
    )
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "auth.locked"


def test_throttle_window_and_unlock():
    t = auth.Throttle()
    for i in range(4):
        t.fail("a", now=i)
    t.fail("a", now=400)  # the first four fell out of the 5-minute window
    t.check("a", now=401)
    for _ in range(4):
        t.fail("a", now=402)
    with pytest.raises(Exception):  # noqa: B017
        t.check("a", now=403)
    t.check("a", now=402 + 301)


def test_throttle_is_per_address_with_a_global_ceiling():
    """Decision 2026-10-22: one client cannot lock the others out; many clients still stop."""
    t = auth.Throttle(global_limit=12)
    for _ in range(5):
        t.fail("10.0.0.9", now=1)
    with pytest.raises(Exception):  # noqa: B017
        t.check("10.0.0.9", now=2)
    t.check("10.0.0.1", now=2)  # another address still gets in
    for i in range(7):
        t.fail(f"10.0.1.{i}", now=3)
    with pytest.raises(Exception):  # noqa: B017
        t.check("10.0.0.1", now=4)  # global ceiling reached


def test_api_key_access_mask_reveal_regenerate(client):
    """AC-AUTH-06"""
    setup_admin(client)
    assert client.get("/api/auth/api-key").json() == {"apiKey": "••••"}
    r = client.post(
        "/api/auth/api-key/reveal", json={"password": "wrong password!"}, headers=ORIGIN
    )
    assert r.status_code == 401
    key = client.post(
        "/api/auth/api-key/reveal", json={"password": PASSWORD}, headers=ORIGIN
    ).json()["apiKey"]
    assert len(key) == 64
    client.cookies.clear()
    assert client.get("/api/auth/api-key", headers={"X-Api-Key": key}).status_code == 200
    assert client.post("/api/auth/api-key", headers={"X-Api-Key": key}).status_code == 200
    assert client.get("/api/auth/api-key", headers={"X-Api-Key": key}).status_code == 401


def test_reveal_is_throttled(client):
    """AC-AUTH-06: same anti brute-force as login."""
    setup_admin(client)
    for _ in range(5):
        client.post(
            "/api/auth/api-key/reveal", json={"password": "wrong password!"}, headers=ORIGIN
        )
    r = client.post("/api/auth/api-key/reveal", json={"password": PASSWORD}, headers=ORIGIN)
    assert r.status_code == 429


def test_no_local_address_exemption(client):
    """AC-AUTH-07: TestClient connects from a local address, yet nothing passes unauthenticated."""
    setup_admin(client)
    client.cookies.clear()
    assert client.get("/api/auth/api-key").status_code == 401


def test_sessions_slide(tmp_path):
    from vodarr.core import open_db

    conn = open_db(tmp_path / "c")
    auth.setup(conn, "admin", PASSWORD)
    token = auth.login(conn, "admin", PASSWORD, now=0)
    assert auth.session_valid(conn, token, now=auth.SESSION_TTL - 10)
    assert auth.session_valid(conn, token, now=2 * auth.SESSION_TTL - 20)  # slid forward
    assert not auth.session_valid(conn, token, now=4 * auth.SESSION_TTL)
    assert not auth.session_valid(conn, "forged", now=0)


def test_accented_username(client):
    """str compare_digest raised on non-ASCII."""
    r = client.post(
        "/api/auth/setup",
        json={"setupCode": "test-setup-code", "username": "Renée", "password": PASSWORD},
        headers=ORIGIN,
    )
    assert r.status_code == 201
    client.cookies.clear()
    r = client.post(
        "/api/auth/login", json={"username": "Renée", "password": PASSWORD}, headers=ORIGIN
    )
    assert r.status_code == 200


def test_non_ascii_api_key_is_401(client):
    setup_admin(client)
    client.cookies.clear()
    assert client.get("/api/auth/api-key", headers={"X-Api-Key": b"cl\xe9"}).status_code == 401


def test_concurrent_wrong_passwords_cannot_beat_the_lock(client):
    """the throttle is checked and updated under one lock."""
    import threading

    setup_admin(client)
    client.cookies.clear()
    statuses = []

    def attempt():
        r = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "wrong password!"},
            headers=ORIGIN,
        )
        statuses.append(r.status_code)

    threads = [threading.Thread(target=attempt) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert statuses.count(401) == 5 and statuses.count(429) == 15


def test_setup_after_setup_skips_scrypt(client, monkeypatch):
    setup_admin(client)
    monkeypatch.setattr(
        auth, "hash_password", lambda p: (_ for _ in ()).throw(AssertionError("scrypt ran"))
    )
    r = client.post(
        "/api/auth/setup",
        json={"setupCode": "test-setup-code", "username": "x", "password": PASSWORD},
        headers=ORIGIN,
    )
    assert r.status_code == 409


def test_setup_needs_the_code_from_the_log(tmp_path, monkeypatch, caplog):
    """Decision 2026-10-22: only someone who can read the logs creates the admin."""
    import logging

    monkeypatch.delenv("VODARR_SETUP_TOKEN")
    caplog.set_level(logging.WARNING)
    app = create_app(tmp_path / "fresh", hosts={"testserver"})
    code = app.state.setup_code
    assert code and code in caplog.text
    with TestClient(app) as c:
        body = {"username": "admin", "password": PASSWORD}
        r = c.post("/api/auth/setup", json={**body, "setupCode": "0000-0000-0000"}, headers=ORIGIN)
        assert r.status_code == 422 and r.json()["error"]["code"] == "auth.setup_code_invalid"
        assert c.post("/api/auth/setup", json=body, headers=ORIGIN).status_code == 422
        assert (
            c.post(
                "/api/auth/setup", json={**body, "setupCode": code.upper()}, headers=ORIGIN
            ).status_code
            == 201
        )
        r = c.post("/api/auth/setup", json={**body, "setupCode": code}, headers=ORIGIN)
        assert r.status_code == 409


def test_throttle_forgets_expired_addresses():
    """memory stays bounded with many source addresses."""
    t = auth.Throttle()
    for i in range(1000):
        t.fail(f"10.1.{i // 250}.{i % 250}", now=0)
    t.fail("10.9.9.9", now=1000)  # later failure triggers the pruning
    assert set(t.failures) <= {"10.9.9.9", auth.Throttle.GLOBAL}
