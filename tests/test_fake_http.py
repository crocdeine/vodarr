import urllib.error
import urllib.request

import pytest

from tests.fakes.http import FakeServer, FileRoute, json_response, redirect

DATA = bytes(range(256)) * 40


def get(url, headers=None):
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=5)


def test_routes_and_recorded_requests():
    with FakeServer() as srv:
        srv.routes[("GET", "/api")] = lambda req: json_response({"q": req.query})
        with get(srv.url + "/api?a=1") as r:
            assert r.read() == b'{"q": {"a": "1"}}'
        assert srv.requests[0].query == {"a": "1"}
        with pytest.raises(urllib.error.HTTPError) as err:
            get(srv.url + "/nope")
        assert err.value.code == 404


def test_file_route_ranges_cut_and_416():
    with FakeServer() as srv:
        srv.routes[("GET", "/f")] = FileRoute(DATA, cuts=[100])
        with get(srv.url + "/f", {"Range": "bytes=0-"}) as r:
            assert r.status == 206
            with pytest.raises(Exception):  # noqa: B017 - connection cut mid-body
                r.read()
        with get(srv.url + "/f", {"Range": "bytes=10-"}) as r:
            assert r.read() == DATA[10:]
            assert r.headers["Content-Range"] == f"bytes 10-{len(DATA) - 1}/{len(DATA)}"
        with pytest.raises(urllib.error.HTTPError) as err:
            get(srv.url + "/f", {"Range": f"bytes={len(DATA)}-"})
        assert err.value.code == 416


def test_file_route_without_ranges_and_redirect():
    with FakeServer() as srv:
        srv.routes[("GET", "/f")] = FileRoute(DATA, ranges=False)
        srv.routes[("GET", "/r")] = lambda req: redirect("/f")
        with get(srv.url + "/r", {"Range": "bytes=10-"}) as r:
            assert r.status == 200
            assert r.read() == DATA
