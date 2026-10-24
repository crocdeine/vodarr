import logging
import stat
import threading

import pytest

from vodarr import core


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def cfg(tmp_path):
    return tmp_path / "config"


def test_db_created_with_schema_and_private_modes(cfg):
    """AC-CORE-01, AC-CORE-02"""
    conn = core.open_db(cfg)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "settings",
        "admin",
        "sessions",
        "sources",
        "releases",
        "files",
        "stages",
        "candidates",
    } <= tables
    assert mode(cfg) == 0o700
    assert mode(cfg / "vodarr.db") == 0o600
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_db_survives_reopen(cfg):
    """AC-CORE-01"""
    conn = core.open_db(cfg)
    core.set_setting(conn, "paths", {"downloads": "/data/vod"})
    conn.close()
    core.close_thread_db()
    conn = core.open_db(cfg)
    assert core.get_setting(conn, "paths") == {"downloads": "/data/vod"}
    assert core.get_setting(conn, "missing", {"x": 1}) == {"x": 1}


def test_migrations_are_idempotent(cfg):
    conn = core.open_db(cfg)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == len(core.MIGRATIONS)
    core.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == version


def test_one_connection_per_thread(cfg):
    main = core.open_db(cfg)
    assert core.open_db(cfg) is main
    other = []
    t = threading.Thread(target=lambda: other.append(core.open_db(cfg)))
    t.start()
    t.join()
    assert other[0] is not main


def test_atomic_write_creates_private_file(tmp_path):
    """AC-CORE-02, AC-CORE-03"""
    target = tmp_path / "cache" / "x.json"
    core.atomic_write(target, b"hello")
    assert target.read_bytes() == b"hello"
    assert mode(target) == 0o600
    assert mode(target.parent) == 0o700


def test_atomic_write_interrupted_leaves_no_partial_file(tmp_path, monkeypatch):
    """AC-CORE-03"""
    target = tmp_path / "x.json"
    target.write_bytes(b"old")

    def boom(*_):
        raise OSError("disk full")

    monkeypatch.setattr(core.os, "replace", boom)
    with pytest.raises(OSError):
        core.atomic_write(target, b"new")
    assert target.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]


def test_msg_carries_code_and_params():
    msg = core.Msg("verify.partial", ok=18, total=19)
    assert msg.code == "verify.partial"
    assert msg.as_dict() == {"code": "verify.partial", "params": {"ok": 18, "total": 19}}


@pytest.mark.parametrize(
    "line, leaked",
    [
        ("GET http://user:hunter2@host/x", "hunter2"),
        ("fetch https://idx.example/dl?apikey=abc123def&file=x", "abc123def"),
        ("login password=s3cret&u=a", "s3cret"),
        ("passkey=deadbeefcafe in announce", "deadbeefcafe"),
    ],
)
def test_log_filter_masks_url_credentials(line, leaked):
    """Base of AC-CORE-04"""
    assert leaked not in core.redact(line)


def test_log_filter_masks_registered_secrets(caplog):
    """Base of AC-CORE-04: Xtream credentials sit in the URL path."""
    core.register_secret("xtreamPass99")
    core.register_secret("")  # ignored
    logger = logging.getLogger("vodarr.test")
    handler_filter = core.SecretFilter()
    caplog.handler.addFilter(handler_filter)
    try:
        logger.warning("download %s", "http://h/series/bob/xtreamPass99/1.mkv")
    finally:
        caplog.handler.removeFilter(handler_filter)
    assert "xtreamPass99" not in caplog.text
    assert "••••" in caplog.text


def test_movie_releases_are_unique(cfg):
    import sqlite3

    conn = core.open_db(cfg)
    conn.execute("INSERT INTO sources VALUES ('s', 'n', 'xtream', '{}', 0, 0)")
    row = (
        "INSERT INTO releases (id, source_id, kind, item_id, season, title, year, dir, created_at)"
        " VALUES (?, 's', 'movie', '500', NULL, 't', NULL, 'd', 0)"
    )
    conn.execute(row, ("r1",))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(row, ("r2",))


def test_concurrent_first_start_migrates_once(cfg):
    errors = []

    def start():
        try:
            core.open_db(cfg)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            core.close_thread_db()

    threads = [threading.Thread(target=start) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_existing_db_is_private(cfg):
    """AC-CORE-02"""
    cfg.mkdir(mode=0o755)
    (cfg / "vodarr.db").touch(mode=0o644)
    conn = core.open_db(cfg)
    core.set_setting(conn, "x", 1)
    assert mode(cfg) == 0o700
    assert mode(cfg / "vodarr.db") == 0o600


def test_atomic_write_leaves_existing_dirs_alone(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    core.atomic_write(shared / "x", b"1")
    assert mode(shared) == 0o755


def test_wal_database_from_an_earlier_version_is_converted(cfg):
    """WAL broke on folders shared with a VM (Docker on macOS): older databases switch back."""
    import sqlite3

    cfg.mkdir(mode=0o700)
    old = sqlite3.connect(cfg / "vodarr.db")
    old.execute("PRAGMA journal_mode = WAL")
    old.close()
    conn = core.open_db(cfg)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert not (cfg / "vodarr.db-wal").exists()


def test_database_from_0_1_0_is_upgraded(cfg):
    """0.1.1 adds files.media_info and releases.tmdb_id to existing databases."""
    import sqlite3

    cfg.mkdir(mode=0o700)
    old = sqlite3.connect(cfg / "vodarr.db", isolation_level=None)
    old.executescript(core.MIGRATIONS[0] + "; PRAGMA user_version = 1;")
    old.close()
    conn = core.open_db(cfg)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(core.MIGRATIONS)
    assert "media_info" in {r[1] for r in conn.execute("PRAGMA table_info(files)")}
    assert "tmdb_id" in {r[1] for r in conn.execute("PRAGMA table_info(releases)")}
