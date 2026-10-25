import json

import pytest

from tests.fakes.torrents import random_bytes
from tests.fakes.xtream import PASSWORD, USER, FakeXtream
from vodarr import core, downloads, sources
from vodarr.core import MsgError

EPISODES = [
    {"id": "1101", "num": 1, "title": "Northwind Precinct - S01E01", "ext": "mkv"},
    {"id": "1102", "num": 2, "title": "Northwind Precinct - S01E02", "ext": "mkv"},
    {"id": "1103", "num": 3, "title": "Bad/../Name", "ext": "mkv"},
]


@pytest.fixture
def env(tmp_path):
    with FakeXtream() as srv:
        for i, e in enumerate(EPISODES):
            srv.files[e["id"]] = random_bytes(50_000 + i, i)
        srv.files["500"] = random_bytes(20_000, 9)
        cfg = tmp_path / "config"
        conn = core.open_db(cfg)
        src = sources.add(
            conn, "My IPTV", "xtream", {"server": srv.url, "user": USER, "password": PASSWORD}
        )
        yield srv, conn, cfg, src, tmp_path / "data" / "vod"
        core.close_thread_db()


def enqueue_season(env, episodes=EPISODES, now=100):
    srv, conn, cfg, src, dl = env
    return downloads.enqueue(
        conn, src, "series", "100", "Northwind Precinct", "2021", 1, episodes, dl, now=now
    )


def work(env, n=1):
    _, conn, cfg, _, _ = env
    return [downloads.process_next(conn, cfg, sleep=lambda s: None) for _ in range(n)]


def test_season_creates_release_with_files_in_layout(env):
    """AC-DL-06"""
    srv, conn, cfg, src, dl = env
    rid = enqueue_season(env)
    rel = conn.execute("SELECT * FROM releases WHERE id = ?", (rid,)).fetchone()
    assert rel["dir"] == str(dl / "My IPTV" / "Northwind Precinct" / "S01")
    names = [r["filename"] for r in conn.execute("SELECT filename FROM files ORDER BY id")]
    assert names == [
        "Northwind Precinct - S01E01.mkv",
        "Northwind Precinct - S01E02.mkv",
        "Bad____Name - S01E03.mkv",
    ]
    stages = [
        r["name"]
        for r in conn.execute("SELECT name FROM stages WHERE release_id = ? ORDER BY rowid", (rid,))
    ]
    assert stages == list(downloads.STAGES)


def test_selection_joins_same_release(env):
    """AC-DL-06: a later selection joins the existing release, nothing duplicated."""
    _, conn, *_ = env
    rid = enqueue_season(env, EPISODES[:1])
    assert enqueue_season(env, EPISODES[:2], now=200) == rid
    assert conn.execute("SELECT count(*) FROM files").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM releases").fetchone()[0] == 1


def test_movie_release_has_one_file(env):
    """AC-DL-06"""
    srv, conn, cfg, src, dl = env
    rid = downloads.enqueue(
        conn,
        src,
        "movie",
        "500",
        "Harbour Lights (2020)",
        "2020",
        None,
        [{"id": "500", "num": None, "title": "Harbour Lights (2020)", "ext": "mp4"}],
        dl,
        now=1,
    )
    rel = conn.execute("SELECT * FROM releases WHERE id = ?", (rid,)).fetchone()
    assert rel["dir"] == str(dl / "My IPTV" / "Harbour Lights (2020)")
    work(env)
    assert (
        dl / "My IPTV" / "Harbour Lights (2020)" / "Harbour Lights (2020).mp4"
    ).read_bytes() == srv.files["500"]


def test_fifo_one_at_a_time_across_sources(env):
    """AC-DL-01"""
    srv, conn, cfg, src, dl = env
    other = sources.add(
        conn, "Other", "xtream", {"server": srv.url, "user": USER, "password": PASSWORD}
    )
    enqueue_season(env, EPISODES[:1], now=1)
    downloads.enqueue(
        conn,
        other,
        "movie",
        "500",
        "Harbour Lights",
        "2020",
        None,
        [{"id": "500", "num": None, "title": "Harbour Lights", "ext": "mp4"}],
        dl,
        now=2,
    )
    enqueue_season(env, EPISODES[1:2], now=3)
    done = work(env, 4)
    assert [d and d["stream_id"] for d in done] == ["1101", "500", "1102", None]


def test_download_stage_done_when_all_files_done(env):
    _, conn, *_ = env
    rid = enqueue_season(env, EPISODES[:2])
    work(env, 2)
    stage = conn.execute(
        "SELECT state FROM stages WHERE release_id = ? AND name = 'download'", (rid,)
    ).fetchone()
    assert stage["state"] == "done"


def test_error_is_recorded_with_code(env):
    srv, conn, *_ = env
    del srv.files["1101"]
    rid = enqueue_season(env, EPISODES[:1])
    work(env)
    row = conn.execute("SELECT dl_status, dl_error FROM files").fetchone()
    assert (row["dl_status"], row["dl_error"]) == ("error", "download.http_error")
    [item] = downloads.queue(conn)
    assert item["error_params"] == {"status": 404}  # bug hunt: shown as "HTTP —" before
    stage = conn.execute(
        "SELECT state, msg_code FROM stages WHERE release_id = ? AND name = 'download'", (rid,)
    ).fetchone()
    assert stage["state"] == "error"


def test_restart_requeues_running_and_resumes_part(env):
    """AC-DL-04"""
    srv, conn, cfg, src, dl = env
    enqueue_season(env, EPISODES[:1])
    conn.execute("UPDATE files SET dl_status = 'running'")
    part = dl / "My IPTV" / "Northwind Precinct" / "S01" / "Northwind Precinct - S01E01.mkv.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(srv.files["1101"][:1000])
    downloads.recover(conn)
    assert conn.execute("SELECT dl_status FROM files").fetchone()[0] == "queued"
    work(env)
    assert srv.requests[-1].headers["range"] == "bytes=1000-"
    assert part.with_name("Northwind Precinct - S01E01.mkv").read_bytes() == srv.files["1101"]


def test_cancel_running_download_stops_it(env, monkeypatch):
    """AC-DL-05"""
    srv, conn, *_ = env
    enqueue_season(env, EPISODES[:1])
    fid = conn.execute("SELECT id FROM files").fetchone()[0]
    real = downloads.download

    def cancel_midway(url, path, **kw):
        progress = kw["progress"]

        def wrapped(done, total):
            downloads.cancel(conn, fid)
            progress(done, total)

        return real(url, path, **{**kw, "progress": wrapped, "chunk": 1000})

    monkeypatch.setattr(downloads, "download", cancel_midway)
    monkeypatch.setattr(downloads, "CHECK_EVERY", 0)
    work(env)
    assert conn.execute("SELECT dl_status FROM files").fetchone()[0] == "cancelled"


def test_remove_cancelled_deletes_part_only(env):
    """AC-DL-05"""
    _, conn, cfg, src, dl = env
    enqueue_season(env, EPISODES[:1])
    fid = conn.execute("SELECT id FROM files").fetchone()[0]
    part = dl / "My IPTV" / "Northwind Precinct" / "S01" / "Northwind Precinct - S01E01.mkv.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"x")
    downloads.cancel(conn, fid)
    downloads.remove(conn, fid)
    assert not part.exists()
    assert conn.execute("SELECT count(*) FROM files").fetchone()[0] == 0


def test_running_or_done_items_cannot_be_removed(env):
    _, conn, *_ = env
    enqueue_season(env, EPISODES[:1])
    fid = conn.execute("SELECT id FROM files").fetchone()[0]
    conn.execute("UPDATE files SET dl_status = 'running'")
    with pytest.raises(MsgError) as err:
        downloads.remove(conn, fid)
    assert err.value.msg.code == "queue.not_removable"


def test_clear_finished_keeps_files(env):
    """AC-DL-05"""
    _, conn, cfg, src, dl = env
    enqueue_season(env, EPISODES[:2])
    work(env, 2)
    downloads.clear_finished(conn)
    assert downloads.queue(conn) == []
    assert conn.execute("SELECT count(*) FROM files").fetchone()[0] == 2
    assert len(list((dl / "My IPTV" / "Northwind Precinct" / "S01").iterdir())) == 2


def test_queue_exposes_progress_and_never_urls(env):
    """AC-DL-07, AC-SRC-08"""
    srv, conn, *_ = env
    enqueue_season(env, EPISODES[:2])
    work(env)
    items = downloads.queue(conn)
    assert [i["status"] for i in items] == ["done", "queued"]
    assert items[0]["done"] == items[0]["total"] == len(srv.files["1101"])
    assert set(items[0]) >= {
        "id",
        "release_id",
        "title",
        "filename",
        "status",
        "done",
        "total",
        "rate",
    }
    assert PASSWORD not in json.dumps(items)


def test_requeue_after_error(env):
    """AC-DL-06: downloading the season again re-queues failed files and resets the pipeline."""
    srv, conn, *_ = env
    data = srv.files.pop("1101")
    rid = enqueue_season(env, EPISODES[:1])
    work(env)
    srv.files["1101"] = data
    enqueue_season(env, EPISODES[:1], now=500)
    assert conn.execute("SELECT dl_status FROM files").fetchone()[0] == "queued"
    assert {
        r[0] for r in conn.execute("SELECT state FROM stages WHERE release_id = ?", (rid,))
    } == {"pending"}


def test_same_enqueue_twice_keeps_pipeline_state(env):
    """AC-DL-06: idempotent — nothing new, nothing reset."""
    _, conn, *_ = env
    rid = enqueue_season(env, EPISODES[:1])
    conn.execute("UPDATE stages SET state = 'done' WHERE release_id = ?", (rid,))
    conn.execute("UPDATE files SET dl_status = 'done'")
    enqueue_season(env, EPISODES[:1], now=200)
    assert {r[0] for r in conn.execute("SELECT state FROM stages")} == {"done"}


def test_fifo_is_strict_within_the_same_second(env):
    """AC-DL-01"""
    _, conn, *_ = env
    a = enqueue_season(env, EPISODES[:2], now=5)
    srv, conn, cfg, src, dl = env
    b = downloads.enqueue(
        conn,
        src,
        "movie",
        "500",
        "Harbour Lights",
        "2020",
        None,
        [{"id": "500", "num": None, "title": "Harbour Lights", "ext": "mp4"}],
        dl,
        now=5,
    )
    order = [r[0] for r in conn.execute("SELECT release_id FROM files ORDER BY queued_at")]
    assert order == [a, a, b]


def test_cancel_before_claim_is_not_lost(env, monkeypatch):
    """AC-DL-05"""
    _, conn, *_ = env
    enqueue_season(env, EPISODES[:1])
    fid = conn.execute("SELECT id FROM files").fetchone()[0]
    real_execute = conn.execute
    calls = []

    class Wrapped:
        def __getattr__(self, name):
            return getattr(conn, name)

        def execute(self, sql, *args):
            if sql.startswith("UPDATE files SET dl_status = 'running'") and not calls:
                calls.append(1)
                downloads.cancel(conn, fid)  # the API thread cancels right before the claim
            return real_execute(sql, *args)

    downloads.process_next(Wrapped(), env[2], sleep=lambda s: None)
    assert conn.execute("SELECT dl_status FROM files").fetchone()[0] == "cancelled"


def test_all_cancelled_marks_download_stage(env):
    _, conn, *_ = env
    rid = enqueue_season(env, EPISODES[:1])
    fid = conn.execute("SELECT id FROM files").fetchone()[0]
    downloads.cancel(conn, fid)
    downloads._update_download_stage(conn, rid)
    row = conn.execute(
        "SELECT state, msg_code FROM stages WHERE release_id = ? AND name = 'download'", (rid,)
    ).fetchone()
    assert (row["state"], row["msg_code"]) == ("error", "download.cancelled")


def test_unexpected_error_never_leaves_file_running(env, monkeypatch):
    _, conn, *_ = env
    enqueue_season(env, EPISODES[:1])

    def boom(*a, **k):
        raise KeyError("server")

    monkeypatch.setattr(downloads.sources, "client", boom)
    work(env)
    row = conn.execute("SELECT dl_status, dl_error FROM files").fetchone()
    assert (row["dl_status"], row["dl_error"]) == ("error", "download.failed")


def test_homonymous_titles_get_separate_folders(env):
    srv, conn, cfg, src, dl = env
    a = downloads.enqueue(
        conn,
        src,
        "movie",
        "500",
        "Harbour Lights",
        "2020",
        None,
        [{"id": "500", "num": None, "title": "Harbour Lights", "ext": "mp4"}],
        dl,
        now=1,
    )
    b = downloads.enqueue(
        conn,
        src,
        "movie",
        "501",
        "Harbour Lights",
        "1987",
        None,
        [{"id": "501", "num": None, "title": "Harbour Lights", "ext": "mp4"}],
        dl,
        now=2,
    )
    dirs = [
        conn.execute("SELECT dir FROM releases WHERE id = ?", (r,)).fetchone()[0] for r in (a, b)
    ]
    assert dirs[0] != dirs[1] and dirs[1].endswith("Harbour Lights (1987)")


@pytest.mark.parametrize("title", ["../../etc/x", "/abs/path", ".hidden", "a\x00b", "É" * 300])
def test_provider_names_stay_inside_release_folder(env, title):
    """AC-SRC-07"""
    srv, conn, cfg, src, dl = env
    srv.files["1101"] = b"data"
    enqueue_season(env, [{"id": "1101", "num": 1, "title": title, "ext": "mkv"}])
    work(env)
    folder = dl / "My IPTV" / "Northwind Precinct" / "S01"
    files = list(folder.iterdir())
    assert len(files) == 1 and files[0].parent == folder
    assert len(files[0].name.encode()) <= 255 - len(".part")
    assert not files[0].name.startswith(".")


def test_non_integer_selection_rejected(env):
    _, conn, cfg, src, dl = env
    with pytest.raises(MsgError):
        downloads.enqueue(conn, src, "series", "100", "T", None, "1", EPISODES, dl)
    with pytest.raises(MsgError):
        downloads.enqueue(
            conn,
            src,
            "series",
            "100",
            "T",
            None,
            1,
            [{"id": "1", "num": "2", "title": "t", "ext": "mkv"}],
            dl,
        )


def test_cancel_during_retry_backoff_stops_at_once(env, monkeypatch):
    """Bug hunt: with the provider down, a cancel was only seen once data flowed again."""
    _, conn, cfg, _, _ = env
    rid = enqueue_season(env, EPISODES[:1])
    calls = []

    def down(*args, **kwargs):
        calls.append(1)
        raise ConnectionResetError

    monkeypatch.setattr(downloads, "open_url", down)

    def cancel_while_waiting(seconds):
        fid = conn.execute("SELECT id FROM files WHERE release_id = ?", (rid,)).fetchone()[0]
        downloads.cancel(conn, fid)

    downloads.process_next(conn, cfg, sleep=cancel_while_waiting)
    assert len(calls) == 1
    row = conn.execute("SELECT id, dl_status FROM files WHERE release_id = ?", (rid,)).fetchone()
    assert row["dl_status"] == "cancelled"
    downloads.ACTIVE.add(row["id"])  # as if the worker were still writing it
    try:
        with pytest.raises(MsgError):
            downloads.remove(conn, row["id"])
    finally:
        downloads.ACTIVE.discard(row["id"])
    downloads.remove(conn, row["id"])
