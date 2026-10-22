"""Single admin account, sessions, API key and brute-force throttling (ADR-0005)."""

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from collections import deque

from vodarr.core import MsgError, register_secret

MIN_PASSWORD = 12
SESSION_TTL = 30 * 86400
SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT, dklen=32)
    return f"scrypt${SCRYPT['n']}${SCRYPT['r']}${SCRYPT['p']}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        computed = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32
        )
    except ValueError:
        return False
    return hmac.compare_digest(computed.hex(), digest)


class Throttle:
    """Per client address: 5 failures in 5 minutes lock that address for 5 minutes. A global
    ceiling (50 failures in 5 minutes, all addresses) still stops a distributed guesser.
    Known limit: in memory, reset on restart; persist it if real attacks get around that."""

    GLOBAL = "*"

    def __init__(
        self, limit: int = 5, window: float = 300, lock: float = 300, global_limit: int = 50
    ):
        self.limits = {self.GLOBAL: global_limit}
        self.limit, self.window, self.lock = limit, window, lock
        self.failures: dict[str, deque[float]] = {}
        self.locked_until: dict[str, float] = {}
        self._mutex = threading.Lock()

    def check(self, client: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._mutex:
            until = max(self.locked_until.get(k, 0.0) for k in (client, self.GLOBAL))
            if now < until:
                raise MsgError("auth.locked", seconds=int(until - now) + 1)

    def fail(self, client: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._mutex:
            for key in (client, self.GLOBAL):
                events = self.failures.setdefault(key, deque())
                events.append(now)
                while events and events[0] < now - self.window:
                    events.popleft()
                if len(events) >= self.limits.get(key, self.limit):
                    self.locked_until[key] = now + self.lock
                    events.clear()
            self._prune(now)

    def _prune(self, now: float) -> None:
        """Forget addresses with no recent failure and no running lock (bounded memory)."""
        for key in [k for k, ev in self.failures.items() if k != self.GLOBAL]:
            events = self.failures[key]
            while events and events[0] < now - self.window:
                events.popleft()
            if not events and self.locked_until.get(key, 0.0) <= now:
                del self.failures[key]
                self.locked_until.pop(key, None)

    def reset(self, client: str) -> None:
        with self._mutex:
            self.failures.pop(client, None)


throttle = Throttle()


def new_setup_code() -> str:
    """One-time code printed in the container log: only someone who can read the logs can
    create the admin of a fresh instance."""
    raw = secrets.token_hex(6)
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def check_setup_code(expected: str, given: str, client: str = "?") -> None:
    with _checking:
        throttle.check(client)
        if not hmac.compare_digest(expected.encode(), given.strip().lower().encode()):
            throttle.fail(client)
            raise MsgError("auth.setup_code_invalid")


def is_setup(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM admin WHERE id = 1").fetchone() is not None


def setup(conn: sqlite3.Connection, username: str, password: str, now: int | None = None) -> str:
    """Create the admin and return a session token for it."""
    if is_setup(conn):
        raise MsgError("auth.already_setup")  # before scrypt: no unauthenticated CPU burn
    if len(password) < MIN_PASSWORD:
        raise MsgError("auth.password_too_short", min=MIN_PASSWORD)
    if not username.strip():
        raise MsgError("auth.username_required")
    try:
        conn.execute(
            "INSERT INTO admin (id, username, password_hash, api_key) VALUES (1, ?, ?, ?)",
            (username.strip(), hash_password(password), secrets.token_hex(32)),
        )
    except sqlite3.IntegrityError:
        raise MsgError("auth.already_setup") from None
    return new_session(conn, now)


def _admin(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM admin WHERE id = 1").fetchone()
    if row is None:
        raise MsgError("auth.setup_required")
    return row


_checking = threading.Lock()


def check_password(
    conn: sqlite3.Connection, password: str, username: str | None = None, client: str = "?"
) -> None:
    """Throttled password check (login and API key reveal). Serialised: concurrent attempts
    cannot all slip past the counter before the first failure is recorded."""
    with _checking:
        throttle.check(client)
        admin = _admin(conn)
        user_ok = username is None or hmac.compare_digest(
            username.strip().encode(), admin["username"].encode()
        )
        if not (verify_password(password, admin["password_hash"]) and user_ok):
            throttle.fail(client)
            raise MsgError("auth.invalid_credentials")
        throttle.reset(client)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def login(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    now: int | None = None,
    client: str = "?",
) -> str:
    check_password(conn, password, username, client)
    return new_session(conn, now)


def new_session(conn: sqlite3.Connection, now: int | None = None) -> str:
    now = int(time.time()) if now is None else now
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions (token_hash, created_at, expires_at) VALUES (?, ?, ?)",
        (_hash(token), now, now + SESSION_TTL),
    )
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    return token


def session_valid(conn: sqlite3.Connection, token: str | None, now: int | None = None) -> bool:
    """Valid sessions slide: each use pushes the expiry 30 days ahead."""
    if not token:
        return False
    now = int(time.time()) if now is None else now
    row = conn.execute(
        "SELECT expires_at FROM sessions WHERE token_hash = ? AND expires_at >= ?",
        (_hash(token), now),
    ).fetchone()
    if row is None:
        return False
    if row["expires_at"] < now + SESSION_TTL - 86400:  # write at most once a day per session
        conn.execute(
            "UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
            (now + SESSION_TTL, _hash(token)),
        )
    return True


def logout(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash(token),))


def api_key_valid(conn: sqlite3.Connection, key: str | None) -> bool:
    row = conn.execute("SELECT api_key FROM admin WHERE id = 1").fetchone()
    return bool(key and row and hmac.compare_digest(key.encode(), row["api_key"].encode()))


def regenerate_api_key(conn: sqlite3.Connection) -> None:
    _admin(conn)
    conn.execute("UPDATE admin SET api_key = ? WHERE id = 1", (secrets.token_hex(32),))


def reveal_api_key(conn: sqlite3.Connection, password: str, client: str = "?") -> str:
    check_password(conn, password, client=client)
    key = _admin(conn)["api_key"]
    register_secret(key)
    return key
