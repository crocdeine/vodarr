import pytest

from tests.fakes.http import FakeServer, FileRoute, Response, redirect
from tests.fakes.torrents import random_bytes
from vodarr import downloads
from vodarr.core import MsgError

DATA = random_bytes(300_000, 1)


def run(srv, tmp_path, **kw):
    sleeps = []
    path = tmp_path / "out" / "ep.mkv"
    path.parent.mkdir(exist_ok=True)
    size = downloads.download(srv.url + "/f", path, sleep=sleeps.append, chunk=8192, **kw)
    return path, size, sleeps


def test_resumes_with_range_after_cuts(tmp_path):
    """AC-DL-02"""
    with FakeServer() as srv:
        route = srv.routes[("GET", "/f")] = FileRoute(DATA, cuts=[100_000, 50_000])
        path, size, sleeps = run(srv, tmp_path)
    assert path.read_bytes() == DATA and size == len(DATA)
    assert not path.with_name("ep.mkv.part").exists()
    ranges = [r.headers.get("range") for r in srv.requests]
    assert ranges == ["bytes=0-", "bytes=100000-", "bytes=150000-"]
    assert sleeps == [5, 5]  # attempts that made progress do not count as failures
    assert route.cuts == []


def test_gives_up_after_five_tries(tmp_path):
    """AC-DL-02"""
    with FakeServer() as srv:
        srv.routes[("GET", "/f")] = lambda req: Response(500, b"boom")
        with pytest.raises(MsgError) as err:
            run(srv, tmp_path)
    assert err.value.msg.code == "download.failed"
    assert len(srv.requests) == 5


def test_waits_grow_on_consecutive_failures(tmp_path):
    sleeps = []
    with FakeServer() as srv:
        srv.routes[("GET", "/f")] = lambda req: Response(500, b"boom")
        with pytest.raises(MsgError):
            downloads.download(srv.url + "/f", tmp_path / "x", sleep=sleeps.append)
    assert sleeps == [5, 10, 15, 20]


@pytest.mark.parametrize("status", [401, 403, 404])
def test_client_errors_fail_fast(tmp_path, status):
    with FakeServer() as srv:
        srv.routes[("GET", "/f")] = lambda req: Response(status, b"no")
        with pytest.raises(MsgError) as err:
            run(srv, tmp_path)
    assert err.value.msg.code == "download.http_error"
    assert len(srv.requests) == 1


def test_206_at_wrong_offset_restarts_from_zero(tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "ep.mkv.part").write_bytes(b"\xff" * 1000)

    def wrong(req):
        return Response(206, DATA, {"Content-Range": f"bytes 0-{len(DATA) - 1}/{len(DATA)}"})

    with FakeServer() as srv:
        srv.routes[("GET", "/f")] = wrong
        path, _, _ = run(srv, tmp_path)
    assert path.read_bytes() == DATA


def test_416_with_other_total_discards_stale_part(tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "ep.mkv.part").write_bytes(DATA + b"old tail")
    with FakeServer() as srv:
        srv.routes[("GET", "/f")] = FileRoute(DATA)
        path, _, _ = run(srv, tmp_path)
    assert path.read_bytes() == DATA


def test_unknown_total_in_content_range(tmp_path):
    with FakeServer() as srv:
        srv.routes[("GET", "/f")] = lambda req: Response(
            206, DATA, {"Content-Range": "bytes 0-9/*"}
        )
        path, _, _ = run(srv, tmp_path)
    assert path.read_bytes() == DATA


def test_callback_errors_are_not_retried(tmp_path):
    def broken(done, total):
        raise RuntimeError("database is locked")

    with FakeServer() as srv:
        srv.routes[("GET", "/f")] = FileRoute(DATA)
        with pytest.raises(RuntimeError):
            run(srv, tmp_path, progress=broken)
    assert len(srv.requests) == 1


def test_server_without_range_restarts_from_zero(tmp_path):
    """AC-DL-03"""
    with FakeServer() as srv:
        srv.routes[("GET", "/f")] = FileRoute(DATA, ranges=False, cuts=[120_000])
        path, _, _ = run(srv, tmp_path)
    assert path.read_bytes() == DATA


def test_416_with_complete_part_is_done(tmp_path):
    """AC-DL-03"""
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "ep.mkv.part").write_bytes(DATA)
    with FakeServer() as srv:
        srv.routes[("GET", "/f")] = FileRoute(DATA)
        path, size, _ = run(srv, tmp_path)
    assert size == len(DATA) and path.read_bytes() == DATA


def test_resumes_existing_part(tmp_path):
    """AC-DL-04 base"""
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "ep.mkv.part").write_bytes(DATA[:1000])
    with FakeServer() as srv:
        srv.routes[("GET", "/f")] = FileRoute(DATA)
        path, _, _ = run(srv, tmp_path)
    assert srv.requests[0].headers["range"] == "bytes=1000-"
    assert path.read_bytes() == DATA


def test_follows_redirects(tmp_path):
    """AC-DL-03"""
    with FakeServer() as srv:
        srv.routes[("GET", "/f")] = lambda req: redirect("/real")
        srv.routes[("GET", "/real")] = FileRoute(DATA)
        path, _, _ = run(srv, tmp_path)
    assert path.read_bytes() == DATA


def test_cancel_keeps_part(tmp_path):
    """AC-DL-05 base"""

    def progress(done, total):
        if done > 50_000:
            raise downloads.Cancelled

    with FakeServer() as srv:
        srv.routes[("GET", "/f")] = FileRoute(DATA)
        with pytest.raises(downloads.Cancelled):
            run(srv, tmp_path, progress=progress)
    part = tmp_path / "out" / "ep.mkv.part"
    assert 50_000 < part.stat().st_size < len(DATA)
    assert len(srv.requests) == 1


def test_progress_reports_total(tmp_path):
    """AC-DL-07 base"""
    seen = []
    with FakeServer() as srv:
        srv.routes[("GET", "/f")] = FileRoute(DATA)
        run(srv, tmp_path, progress=lambda d, t: seen.append((d, t)))
    assert seen[-1] == (len(DATA), len(DATA))
