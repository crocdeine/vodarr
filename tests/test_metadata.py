"""TMDB details with the user's own key (0.1.1)."""

import pytest
from fastapi.testclient import TestClient

from tests.fakes.tmdb import KEY, TOKEN, FakeTMDB
from tests.fakes.xtream import PASSWORD, USER, FakeXtream
from vodarr import auth, core, metadata, services
from vodarr.app import create_app

ADMIN = "correct horse battery"
H = {"Origin": "http://testserver"}


@pytest.fixture
def tmdb(monkeypatch):
    with FakeTMDB() as srv:
        monkeypatch.setattr(services.TMDB, "BASE", srv.url + "/3")
        yield srv


def configure(conn, key=KEY):
    core.set_setting(conn, "services", {"tmdb": {"apiKey": key}})


def test_without_key_nothing_is_fetched(conn, tmp_path, tmdb):
    assert metadata.lookup(conn, tmp_path, "series", "4242", None, None, "fr") is None
    assert tmdb.requests == []


@pytest.mark.parametrize("key", [KEY, TOKEN])
def test_details_by_provider_id_in_ui_language(conn, tmp_path, tmdb, key):
    configure(conn, key)
    d = metadata.lookup(conn, tmp_path, "series", "4242", None, None, "fr")
    assert d["title"] == "Northwind Precinct" and d["overview"] == "Des policiers du Nord."
    assert (
        d["year"] == "2021"
        and d["seasons"] == 2
        and d["runtime"] == 52
        and d["genres"] == ["Crime"]
    )
    assert d["poster"] == "https://image.tmdb.org/t/p/w342/np.jpg"
    assert d["cast"] == ["Ada Lind", "Bo Strand"]
    assert (
        metadata.lookup(conn, tmp_path, "series", "4242", None, None, "en")["overview"]
        == "Northern cops."
    )


def test_search_by_title_when_no_id_and_cache(conn, tmp_path, tmdb):
    configure(conn)
    d = metadata.lookup(conn, tmp_path, "movie", None, "Harbour Lights", "2020", "en")
    assert d["tmdb_id"] == "777" and d["poster"] is None
    before = len(tmdb.requests)
    metadata.lookup(conn, tmp_path, "movie", "777", None, None, "en")
    assert len(tmdb.requests) == before  # served from the 7-day cache
    assert metadata.lookup(conn, tmp_path, "movie", None, "Nothing Like It", None, "en") is None


def test_unknown_provider_id_is_no_match(conn, tmp_path, tmdb):
    configure(conn)
    assert metadata.lookup(conn, tmp_path, "movie", "999999", None, None, "en") is None


def test_key_never_logged_or_returned(tmp_path, tmdb, caplog):
    with FakeXtream() as xt, TestClient(create_app(tmp_path / "config", hosts={"testserver"})) as c:
        auth.throttle = auth.Throttle()
        c.post(
            "/api/auth/setup",
            json={"setupCode": "test-setup-code", "username": "admin", "password": ADMIN},
            headers=H,
        )
        (tmp_path / "data" / "vod").mkdir(parents=True)
        r = c.put(
            "/api/settings",
            json={
                "services": {"tmdb": {"apiKey": KEY}},
                "paths": {"downloads": str(tmp_path / "data" / "vod")},
            },
            headers=H,
        )
        assert r.json()["services"]["tmdb"]["apiKey"] == "••••"
        assert (
            c.post(
                "/api/settings/test", json={"service": "tmdb", "apiKey": "••••"}, headers=H
            ).json()["ok"]
            is True
        )
        assert (
            c.post(
                "/api/settings/test", json={"service": "tmdb", "apiKey": "wrong"}, headers=H
            ).json()["error"]["code"]
            == "tmdb.auth_failed"
        )
        sid = c.post(
            "/api/sources",
            headers=H,
            json={
                "name": "X",
                "type": "xtream",
                "server": xt.url,
                "user": USER,
                "password": PASSWORD,
            },
        ).json()["id"]
        xt.series_info["100"]["info"]["tmdb"] = "4242"
        detail = c.get(f"/api/sources/{sid}/series/100").json()
        assert detail["tmdbId"] == "4242"
        r = c.get("/api/metadata?kind=series&tmdbId=4242&lang=fr")
        assert r.json()["data"]["overview"] == "Des policiers du Nord."
        rid = c.post(
            "/api/releases",
            headers=H,
            json={
                "sourceId": sid,
                "kind": "series",
                "itemId": "100",
                "season": 1,
                "episodes": ["1101"],
            },
        ).json()
        assert rid["tmdbId"] == "4242"
        assert c.get("/api/metadata?kind=series&tmdbId=abc").status_code == 422
        assert KEY not in r.text and KEY not in caplog.text


def test_provider_id_of_another_film_is_ignored(conn, tmp_path, tmdb):
    """Real run: the provider attached the TMDB id of a 2023 film to a 2020 film of the same
    title; TMDB's year contradicts the source, so the title and year are searched instead."""
    configure(conn)
    d = metadata.lookup(conn, tmp_path, "movie", "778", "Harbour Lights", "2020", "en")
    assert d["tmdb_id"] == "777" and d["year"] == "2020"
    d = metadata.lookup(conn, tmp_path, "movie", "778", "Harbour Lights", "2022", "en")
    assert d["tmdb_id"] == "778"  # one year apart: the provider's id is kept
