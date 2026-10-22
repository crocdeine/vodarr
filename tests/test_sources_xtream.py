import os
import stat
import time

import pytest

from tests.fakes.xtream import PASSWORD, USER, FakeXtream
from vodarr import sources
from vodarr.core import MsgError


@pytest.fixture
def xtream(tmp_path):
    with FakeXtream() as srv:
        src = sources.Source(
            "src1", "My IPTV", "xtream", {"server": srv.url, "user": USER, "password": PASSWORD}
        )
        yield srv, sources.client(src, tmp_path)


def test_categories_and_items(xtream):
    """AC-SRC-01"""
    _, x = xtream
    cats = x.categories("series")
    assert [c["name"] for c in cats] == ["NORDIC | Crime", "Documentaries"]
    items = x.items("series")
    assert items[0] == {
        "id": "100",
        "name": "Northwind Precinct",
        "category_id": "1",
        "cover": "https://img.invalid/n.jpg",
        "year": "2021",
    }
    assert [m["id"] for m in x.items("movie")] == ["500"]


def test_split_prefix_groups_by_provider(tmp_path):
    """AC-SRC-02"""
    with FakeXtream() as srv:
        src = sources.Source(
            "s",
            "My IPTV",
            "xtream",
            {"server": srv.url, "user": USER, "password": PASSWORD},
            split_prefix=True,
        )
        cats = sources.client(src, tmp_path).categories("series")
    assert [(c["provider"], c["label"]) for c in cats] == [
        ("NORDIC", "Crime"),
        ("My IPTV", "Documentaries"),
    ]


def test_no_split_uses_source_name(xtream):
    _, x = xtream
    assert x.categories("series")[0]["provider"] == "My IPTV"
    assert x.categories("series")[0]["label"] == "NORDIC | Crime"


def test_series_detail_sorted_with_release_names(xtream):
    """AC-SRC-01, AC-SRC-07"""
    _, x = xtream
    s = x.series("100")
    assert s["name"] == "Northwind Precinct" and s["year"] == "2021"
    assert [season["season"] for season in s["seasons"]] == [1, 2]
    eps = s["seasons"][0]["episodes"]
    assert [e["num"] for e in eps] == [1, 2, 3]
    assert eps[0]["release"] == "Northwind.Precinct.S01E01.1080p.WEB-DL-GRP"
    assert eps[2]["release"] is None
    assert eps[2]["ext"] == "mkv"  # exe is not whitelisted
    assert "url" not in eps[0]


def test_movie_detail(xtream):
    """AC-SRC-01"""
    _, x = xtream
    m = x.movie("500")
    assert m["name"] == "Harbour Lights (2020)" and m["year"] == "2020" and m["ext"] == "mp4"
    assert m["release"] == "Harbour.Lights.2020.1080p.BluRay.x264-GRP"


def test_unknown_item_is_reported(xtream):
    _, x = xtream
    with pytest.raises(MsgError) as err:
        x.series("999")
    assert err.value.msg.code == "source.not_found"


def test_stream_url_escapes_credentials(tmp_path):
    """AC-SRC-08"""
    src = sources.Source(
        "s", "n", "xtream", {"server": "http://h:8080/", "user": "a b", "password": "p/w?#"}
    )
    url = sources.client(src, tmp_path).stream_url("series", "1101", "../exe")
    assert url == "http://h:8080/series/a%20b/p%2Fw%3F%23/1101.mkv"


def test_catalogue_cached_24h_private(xtream, tmp_path):
    """AC-SRC-06"""
    srv, x = xtream
    x.items("series")
    x.items("series")
    assert sum(r.query.get("action") == "get_series" for r in srv.requests) == 1
    cached = tmp_path / "cache" / "src1" / "get_series.json"
    assert stat.S_IMODE(cached.stat().st_mode) == 0o600
    assert PASSWORD not in cached.read_text()
    old = time.time() - 86401
    os.utime(cached, (old, old))
    x.items("series")
    assert sum(r.query.get("action") == "get_series" for r in srv.requests) == 2


def test_refresh_drops_cache(xtream):
    """AC-SRC-06"""
    srv, x = xtream
    x.items("series")
    x.refresh()
    x.items("series")
    assert sum(r.query.get("action") == "get_series" for r in srv.requests) == 2


@pytest.mark.parametrize(
    "mode, code",
    [
        ("auth_fail", "source.auth_failed"),
        ("html", "source.invalid_response"),
        ("not_list", "source.invalid_response"),
    ],
)
def test_invalid_response_never_cached(xtream, tmp_path, mode, code):
    """AC-SRC-06"""
    srv, x = xtream
    srv.mode = mode
    with pytest.raises(MsgError) as err:
        x.items("series")
    assert err.value.msg.code == code
    assert not (tmp_path / "cache" / "src1" / "get_series.json").exists()
    srv.mode = "ok"
    assert x.items("series")


def test_unreachable_server(tmp_path):
    src = sources.Source(
        "s", "n", "xtream", {"server": "http://127.0.0.1:9", "user": "u", "password": "p"}
    )
    with pytest.raises(MsgError) as err:
        sources.client(src, tmp_path).items("series")
    assert err.value.msg.code == "source.unreachable"


@pytest.mark.parametrize(
    "name, expected",
    [
        ("a/b\\c", "a_b_c"),
        ("..", "__"),
        ("../x", "___x"),
        ("Show: S01E01?", "Show_ S01E01_"),
        ("  ", "_"),
    ],
)
def test_safe_filename(name, expected):
    """AC-SRC-07"""
    assert sources.safe_filename(name) == expected


@pytest.mark.parametrize(
    "ext, expected",
    [("MKV", "mkv"), ("mp4", "mp4"), ("ts", "ts"), ("exe", "mkv"), ("../x", "mkv"), (None, "mkv")],
)
def test_safe_ext(ext, expected):
    """AC-SRC-07"""
    assert sources.safe_ext(ext) == expected


def test_password_registered_for_log_redaction(xtream):
    from vodarr.core import redact

    assert PASSWORD not in redact(f"GET /series/{USER}/{PASSWORD}/1.mkv")


def test_test_connection(xtream):
    """Base of AC-SET-07"""
    srv, x = xtream
    assert x.test() is None
    srv.mode = "auth_fail"
    assert x.test().code == "source.auth_failed"


@pytest.mark.parametrize(
    "payload",
    [
        {"info": {"name": "x"}, "episodes": {"one": [{}]}},
        {"info": {"name": "x"}, "episodes": {"1": [{"id": 1}]}},
        {"info": "not a dict", "episodes": []},
    ],
)
def test_malformed_series_detail_is_invalid_response(xtream, payload):
    """AC-SRC-01: provider JSON is untrusted."""
    srv, x = xtream
    srv.series_info["666"] = payload
    with pytest.raises(MsgError) as err:
        x.series("666")
    assert err.value.msg.code == "source.invalid_response"


def test_malformed_catalogue_entries_are_skipped(xtream, monkeypatch):
    from tests.fakes import xtream as fx

    srv, x = xtream
    monkeypatch.setitem(
        fx.CATALOGUE, "get_series", [{"name": "no id"}, "junk", {"series_id": 7, "name": "ok"}]
    )
    assert [i["id"] for i in x.items("series")] == ["7"]


def test_string_auth_zero_is_auth_failure(xtream, monkeypatch):
    srv, x = xtream
    from tests.fakes.http import json_response

    monkeypatch.setitem(
        srv.routes,
        ("GET", "/player_api.php"),
        lambda req: json_response({"user_info": {"auth": "0"}}),
    )
    with pytest.raises(MsgError) as err:
        x.items("series")
    assert err.value.msg.code == "source.auth_failed"


def test_corrupt_cache_is_refetched(xtream, tmp_path):
    srv, x = xtream
    x.items("series")
    (tmp_path / "cache" / "src1" / "get_series.json").write_text("{broken")
    assert x.items("series")


def test_invalid_url_error_does_not_leak_credentials(tmp_path, caplog):
    """AC-CORE-04: http.client.InvalidURL carries the URL in its message."""
    src = sources.Source(
        "s", "n", "xtream", {"server": "http://h\x01", "user": "u", "password": "pw-secret-1"}
    )
    with pytest.raises(MsgError) as err:
        sources.client(src, tmp_path).items("series")
    assert err.value.msg.code == "source.unreachable"
    assert (
        err.value.__cause__ is None
        and err.value.__context__ is None
        or "pw-secret-1" not in repr(err.value.__context__)
    )
    assert "pw-secret-1" not in caplog.text


@pytest.mark.parametrize("target", ["file:///etc/passwd", "ftp://127.0.0.1/x"])
def test_redirect_to_other_scheme_is_refused(tmp_path, target):
    from tests.fakes.http import FakeServer, redirect

    with FakeServer() as srv:
        srv.routes[("GET", "/player_api.php")] = lambda req: redirect(target)
        src = sources.Source("s", "n", "xtream", {"server": srv.url, "user": "u", "password": "p"})
        with pytest.raises(MsgError) as err:
            sources.client(src, tmp_path).items("series")
    assert err.value.msg.code in ("source.unreachable", "source.http_error")
    assert len(srv.requests) == 1
