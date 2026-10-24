"""Database, settings, atomic writes, translatable messages and secret-free logging."""

import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

MASK = "••••"

MIGRATIONS = [
    """
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE admin (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        username TEXT NOT NULL, password_hash TEXT NOT NULL, api_key TEXT NOT NULL);
    CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, created_at INTEGER, expires_at INTEGER);
    CREATE TABLE sources (
        id TEXT PRIMARY KEY, name TEXT NOT NULL,
        type TEXT NOT NULL CHECK (type IN ('xtream', 'm3u')),
        config TEXT NOT NULL,
        split_prefix INTEGER NOT NULL DEFAULT 0, position INTEGER NOT NULL);
    CREATE TABLE releases (
        id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL REFERENCES sources(id),
        kind TEXT NOT NULL CHECK (kind IN ('series', 'movie')),
        item_id TEXT NOT NULL, season INTEGER,
        title TEXT NOT NULL, year TEXT, dir TEXT NOT NULL,
        created_at INTEGER NOT NULL);
    -- NULL seasons (movies) are distinct for UNIQUE, hence the expression index.
    CREATE UNIQUE INDEX releases_item ON releases (source_id, kind, item_id, coalesce(season, -1));
    CREATE TABLE files (
        id INTEGER PRIMARY KEY,
        release_id TEXT NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
        stream_id TEXT NOT NULL, ext TEXT NOT NULL,
        season INTEGER, episode INTEGER, filename TEXT NOT NULL,
        size INTEGER, release_name TEXT,
        dl_status TEXT NOT NULL DEFAULT 'queued',
        dl_done INTEGER NOT NULL DEFAULT 0, dl_total INTEGER,
        dl_error TEXT, queued_at INTEGER NOT NULL,
        in_queue INTEGER NOT NULL DEFAULT 1,
        UNIQUE (release_id, stream_id));
    CREATE TABLE stages (
        release_id TEXT NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending',
        pct INTEGER NOT NULL DEFAULT 0,
        msg_code TEXT, msg_params TEXT,
        PRIMARY KEY (release_id, name));
    CREATE TABLE candidates (
        id INTEGER PRIMARY KEY,
        release_id TEXT NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
        indexer TEXT NOT NULL, title TEXT NOT NULL, size INTEGER,
        scope TEXT NOT NULL,
        is_candidate INTEGER NOT NULL,
        download_url TEXT,
        info_url TEXT, seeders INTEGER,
        status TEXT NOT NULL DEFAULT 'pending',
        problem_code TEXT, file_report TEXT,
        torrent_path TEXT, infohash TEXT,
        link_root TEXT,
        complete_requested INTEGER NOT NULL DEFAULT 0,
        added_to_qbit INTEGER NOT NULL DEFAULT 0);
    CREATE INDEX files_queue ON files (dl_status, queued_at);
    """,
    # 0.1.1: technical metadata read by ffprobe, TMDB id of the title.
    """
    ALTER TABLE files ADD COLUMN media_info TEXT;
    ALTER TABLE releases ADD COLUMN tmdb_id TEXT;
    """,
]


def private_dir(path: Path, force: bool = False) -> Path:
    """Create `path` as 700. Existing directories are only tightened when `force` (config root):
    others may be shared with other services."""
    if not path.is_dir():
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)  # mkdir's mode is filtered by the umask
    elif force:
        try:
            path.chmod(0o700)
        except PermissionError:
            logging.getLogger(__name__).warning("cannot restrict permissions of %s", path)
    return path


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")  # read the version under the write lock: concurrent starts
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
            for statement in _statements(script):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {number}")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _statements(script: str) -> list[str]:
    out, buf = [], ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf)
            buf = ""
    return out


_local = threading.local()


def open_db(config_dir: Path) -> sqlite3.Connection:
    """Return this thread's connection to <config_dir>/vodarr.db, creating it if needed."""
    db_path = private_dir(Path(config_dir), force=True) / "vodarr.db"
    conns = _local.__dict__.setdefault("conns", {})
    conn = conns.get(db_path)
    if conn is not None:
        return conn
    # Create the file ourselves so it is 600 from the start; SQLite gives its journal the same mode.
    os.close(os.open(db_path, os.O_CREAT | os.O_WRONLY, 0o600))
    db_path.chmod(0o600)
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    # Rollback journal, not WAL: WAL's memory-mapped -shm breaks on folders shared with a VM
    # (Docker on macOS lost committed rows, "disk I/O error"). One user: readers waiting a few
    # ms on a write is fine. Also converts databases created in WAL by earlier versions.
    for _ in range(50):  # changing the journal mode ignores busy_timeout when a lock is held
        try:
            conn.execute("PRAGMA journal_mode = DELETE")
            break
        except sqlite3.OperationalError:
            time.sleep(0.1)
    else:
        raise sqlite3.OperationalError("database is locked")
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    conns[db_path] = conn
    return conn


def close_thread_db() -> None:
    for conn in _local.__dict__.pop("conns", {}).values():
        conn.close()


def get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)"
        " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )


def atomic_write(path: Path, data: bytes) -> None:
    """Write `data` to `path` (mode 600) so that readers never see a partial file."""
    private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Msg:
    """A user-facing message: a translation code plus named parameters (ADR-0007)."""

    def __init__(self, code: str, **params: Any):
        self.code = code
        self.params = params

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "params": self.params}


class MsgError(Exception):
    """An error carrying a translatable message."""

    def __init__(self, code: str, **params: Any):
        super().__init__(code)
        self.msg = Msg(code, **params)


_secrets: set[str] = set()
_SECRET_PATTERNS = [
    re.compile(r"(?<=://)[^/@\s]+(?=@)"),  # user:password@host
    re.compile(r"(?i)\b(?:apikey|api_key|password|passkey|token|username|user)=([^&\s]+)"),
]


def register_secret(value: str | None) -> None:
    """Mask `value` in every log line from now on (Xtream credentials live in URL paths)."""
    if value and len(value) >= 3:
        _secrets.add(value)


def redact(text: str) -> str:
    for secret in sorted(_secrets, key=len, reverse=True):
        text = text.replace(secret, MASK)
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda m: m.group(0).replace(m.group(1), MASK), text)
        else:
            text = pattern.sub(MASK, text)
    return text


class SecretFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(SecretFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True
