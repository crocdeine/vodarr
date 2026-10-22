import pytest

from tests.fakes.http import FakeServer, Response
from vodarr import sources
from vodarr.core import MsgError, redact

PLAYLIST = """#EXTM3U
#EXTINF:-1 tvg-name="Northwind Precinct S01E01" tvg-logo="https://img.invalid/n.jpg" group-title="Crime, Nordic",Northwind Precinct S01E01
http://iptv.invalid/series/u/p/1101.mkv
#EXTINF:-1 tvg-name="Northwind Precinct S01E02" group-title="Crime, Nordic",Northwind Precinct S01E02
http://iptv.invalid/series/u/p/1102.mkv
#EXTINF:-1 tvg-name="Northwind Precinct S02E01" group-title="Crime, Nordic",Northwind Precinct S02E01
http://iptv.invalid/series/u/p/1201.mkv
#EXTINF:-1 tvg-name="Harbour Lights (2020)" group-title="Films",Harbour Lights (2020)
http://iptv.invalid/movie/u/p/500.mp4
#EXTINF:-1 tvg-name="News 24" group-title="Live",News 24
http://iptv.invalid/live/u/p/9.ts
#EXTINF:-1,Weird.Ext.Movie
http://iptv.invalid/movie/u/p/501.exe
"""


@pytest.fixture
def m3u(tmp_path):
    with FakeServer() as srv:
        srv.routes[("GET", "/get.php")] = lambda req: Response(200, PLAYLIST.encode())
        src = sources.Source(
            "m1", "My M3U", "m3u", {"url": srv.url + "/get.php?username=u&password=p"}
        )
        yield srv, sources.client(src, tmp_path)


def test_extinf_with_comma_in_quoted_attribute():
    """AC-SRC-03"""
    entries = sources.parse_m3u(PLAYLIST)
    attrs, title, url = entries[0]
    assert attrs["group-title"] == "Crime, Nordic"
    assert title == "Northwind Precinct S01E01"
    assert url.endswith("/1101.mkv")


def test_classification_and_grouping(m3u):
    """AC-SRC-04: live ignored, episodes grouped by normalized title."""
    _, c = m3u
    series = c.items("series")
    assert [s["name"] for s in series] == ["Northwind Precinct"]
    assert sorted(m["name"] for m in c.items("movie")) == [
        "Harbour Lights (2020)",
        "Weird.Ext.Movie",
    ]
    assert {cat["name"] for cat in c.categories("series")} == {"Crime, Nordic"}
    detail = c.series(series[0]["id"])
    assert [(s["season"], [e["num"] for e in s["episodes"]]) for s in detail["seasons"]] == [
        (1, [1, 2]),
        (2, [1]),
    ]


def test_movie_detail_and_stream_url_stay_server_side(m3u):
    """AC-SRC-07, AC-SRC-08"""
    _, c = m3u
    movies = {m["name"]: m for m in c.items("movie")}
    assert all("url" not in m for m in movies.values())
    weird = c.movie(movies["Weird.Ext.Movie"]["id"])
    assert weird["ext"] == "mkv" and weird["year"] == ""
    harbour = c.movie(movies["Harbour Lights (2020)"]["id"])
    assert harbour["year"] == "2020"
    url = c.stream_url("movie", movies["Harbour Lights (2020)"]["id"], "mp4")
    assert url == "http://iptv.invalid/movie/u/p/500.mp4"


def test_ids_are_stable_and_hide_urls(m3u, tmp_path):
    """AC-SET-01 base: same playlist, same ids."""
    _, c = m3u
    first = [m["id"] for m in c.items("movie")]
    c.refresh()
    assert [m["id"] for m in c.items("movie")] == first
    assert all("500" not in i for i in first)


def test_playlist_url_is_a_registered_secret(m3u):
    _, c = m3u
    url = c.source.config["url"]
    assert url not in redact(f"fetching {url}")


def test_playlist_too_large_stops_reading(tmp_path, monkeypatch):
    """AC-SRC-05"""
    monkeypatch.setattr(sources, "M3U_LIMIT", 1000)
    sent = []
    with FakeServer() as srv:

        def big(req):
            sent.append(1)
            return Response(200, b"#EXTM3U\n" + b"x" * 5000)

        srv.routes[("GET", "/big.m3u")] = big
        src = sources.Source("m2", "n", "m3u", {"url": srv.url + "/big.m3u"})
        with pytest.raises(MsgError) as err:
            sources.client(src, tmp_path).items("movie")
    assert err.value.msg.code == "source.playlist_too_large"
    assert not (tmp_path / "cache" / "m2").exists()


def test_playlist_file_under_config_playlists(tmp_path):
    """AC-SET-09"""
    (tmp_path / "playlists").mkdir()
    (tmp_path / "playlists" / "list.m3u").write_text(PLAYLIST)
    src = sources.Source("m3", "n", "m3u", {"path": "list.m3u"})
    assert len(sources.client(src, tmp_path).items("movie")) == 2


@pytest.mark.parametrize(
    "config",
    [
        {"path": "../vodarr.db"},
        {"path": "/etc/passwd"},
        {"url": "file:///etc/passwd"},
        {"url": "ftp://h/x.m3u"},
        {},
    ],
)
def test_other_schemes_and_paths_refused(tmp_path, config):
    """AC-SET-09"""
    (tmp_path / "playlists").mkdir()
    with pytest.raises(MsgError) as err:
        sources.client(sources.Source("m4", "n", "m3u", config), tmp_path).items("movie")
    assert err.value.msg.code == "source.bad_url"


def test_html_answer_is_invalid_and_not_cached(tmp_path):
    """AC-SRC-06"""
    with FakeServer() as srv:
        srv.routes[("GET", "/x")] = lambda req: Response(200, b"<html>login</html>")
        src = sources.Source("m5", "n", "m3u", {"url": srv.url + "/x"})
        with pytest.raises(MsgError) as err:
            sources.client(src, tmp_path).items("movie")
    assert err.value.msg.code == "source.invalid_response"
    assert not (tmp_path / "cache" / "m5").exists()


def test_search_all_words_case_and_accent_insensitive():
    """AC-SRC-09"""
    items = [{"name": "Élan Vital"}, {"name": "Vital Signs"}, {"name": "ELAN"}]
    assert sources.search(items, "elan vital") == [{"name": "Élan Vital"}]
    assert sources.search(items, "VITAL") == items[:2]
    assert sources.search(items, "") == items
