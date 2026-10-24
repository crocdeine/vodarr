import json
import logging

import pytest

from tests.fakes.prowlarr import API_KEY, FakeProwlarr
from vodarr import core, pipeline, stages
from vodarr.core import MsgError

SIZES = [40_000, 35_000, 30_001]


@pytest.fixture
def prowl(conn):
    with FakeProwlarr() as srv:
        core.set_setting(conn, "services", {"prowlarr": {"url": srv.url, "apiKey": API_KEY}})
        yield srv


def run_search(conn, tmp_path, rid):
    release = conn.execute("SELECT * FROM releases WHERE id = ?", (rid,)).fetchone()
    return stages.stage_search(pipeline.Ctx(conn, tmp_path / "config", release, lambda d, t: None))


def analysed_season(conn, make_season, episodes=(1, 2, 3)):
    contents = [b"x" * SIZES[i - 1] for i in episodes]
    rid, paths = make_season(contents)
    for ep, path in zip(episodes, paths, strict=True):
        conn.execute(
            "UPDATE files SET size = ?, episode = ? WHERE release_id = ? AND filename = ?",
            (path.stat().st_size, ep, rid, path.name),
        )
    return rid


def candidates(conn, rid):
    rows = conn.execute(
        "SELECT title, scope, is_candidate FROM candidates WHERE release_id = ? ORDER BY id", (rid,)
    )
    return [(r["title"], r["scope"], bool(r["is_candidate"])) for r in rows]


def test_prowlarr_absent_is_skipped(conn, tmp_path, make_season):
    """AC-PIPE-02"""
    rid = analysed_season(conn, make_season)
    with pytest.raises(pipeline.Skip) as skip:
        run_search(conn, tmp_path, rid)
    assert skip.value.msg.code == "search.no_prowlarr"


def test_search_all_indexers_with_key(conn, tmp_path, make_season, prowl):
    """AC-SRCH-01"""
    rid = analysed_season(conn, make_season)
    run_search(conn, tmp_path, rid)
    req = prowl.requests[0]
    assert req.query == {"query": "northwind precinct", "type": "search", "limit": "1000"}
    assert req.headers["x-api-key"] == API_KEY


def test_season_filters(conn, tmp_path, make_season, prowl):
    """AC-SRCH-02, AC-SRCH-03, AC-SRCH-06, E8"""
    total = sum(SIZES)
    rid = analysed_season(conn, make_season)
    prowl.add("Nimbus", "Northwind.Precinct.S01.1080p.WEB-DL-GRP", total)
    prowl.add("Nimbus", "Northwind Precinct Saison 1 FRENCH 1080p", int(total * 1.14))
    prowl.add("Aurora", "Northwind.Precinct.Season.1.720p-X", int(total * 0.5))  # listed only
    prowl.add("Aurora", "Northwind.Precinct.S12.1080p-GRP", total)  # E8
    prowl.add("Aurora", "Northwind Precinct saison 10", total)  # E8
    prowl.add("Aurora", "Northwind.Precinct.S02E01.1080p-GRP", SIZES[0])  # E8
    prowl.add("Aurora", "Northwind.Precinct.S01E02.1080p-GRP", SIZES[1])
    prowl.add("Aurora", "Northwind.Precinct.S01E02-E03.1080p-GRP", SIZES[1] + SIZES[2])
    prowl.add("Aurora", "Northwind.Precinct.S01E07.1080p-GRP", SIZES[1])  # not present locally
    prowl.add("Aurora", "Southwind.Precinct.S01.1080p-GRP", total)  # word missing
    msg = run_search(conn, tmp_path, rid)
    assert candidates(conn, rid) == [
        ("Northwind.Precinct.S01.1080p.WEB-DL-GRP", "pack", True),
        ("Northwind Precinct Saison 1 FRENCH 1080p", "pack", True),
        ("Northwind.Precinct.Season.1.720p-X", "pack", False),
        ("Northwind.Precinct.S01E02.1080p-GRP", "S01E02", True),
        ("Northwind.Precinct.S01E02-E03.1080p-GRP", "S01E02-E03", True),
        ("Northwind.Precinct.S01E07.1080p-GRP", "S01E07", False),
    ]
    assert msg.params == {"results": 10, "candidates": 4, "trackers": "Aurora, Nimbus"}


def test_selection_only_keeps_present_episodes(conn, tmp_path, make_season, prowl):
    """AC-SRCH-02, E10: only E01 and E03 downloaded."""
    rid = analysed_season(conn, make_season, episodes=(1, 3))
    prowl.add("Nimbus", "Northwind.Precinct.S01.1080p-GRP", sum(SIZES))  # size off
    prowl.add("Nimbus", "Northwind.Precinct.S01E01.1080p-GRP", SIZES[0])
    prowl.add("Nimbus", "Northwind.Precinct.S01E03.1080p-GRP", SIZES[2])
    prowl.add("Nimbus", "Northwind.Precinct.S01E02.1080p-GRP", SIZES[1])
    run_search(conn, tmp_path, rid)
    assert [c for c in candidates(conn, rid) if c[2]] == [
        ("Northwind.Precinct.S01E01.1080p-GRP", "S01E01", True),
        ("Northwind.Precinct.S01E03.1080p-GRP", "S01E03", True),
    ]


def test_movie_needs_year_and_size(conn, tmp_path, make_season, prowl):
    """AC-SRCH-02"""
    rid, paths = make_season(
        [b"m" * 50_000], kind="movie", title="Harbour Lights (2020)", year="2020"
    )
    conn.execute("UPDATE files SET size = 50000")
    prowl.add("Nimbus", "Harbour.Lights.2020.1080p.BluRay-GRP", 52_000)
    prowl.add("Nimbus", "Harbour.Lights.1987.1080p.BluRay-GRP", 50_000)
    prowl.add("Nimbus", "Harbour.Lights.2020.2160p-GRP", 90_000)
    run_search(conn, tmp_path, rid)
    assert candidates(conn, rid) == [
        ("Harbour.Lights.2020.1080p.BluRay-GRP", "movie", True),
        ("Harbour.Lights.2020.2160p-GRP", "movie", False),
    ]


def test_download_url_stays_server_side(conn, tmp_path, make_season, prowl, caplog):
    """AC-SRCH-04: stored for the verify stage, never in the stage message or logs."""
    caplog.set_level(logging.DEBUG)
    rid = analysed_season(conn, make_season)
    prowl.add("Nimbus", "Northwind.Precinct.S01.1080p-GRP", sum(SIZES))
    msg = run_search(conn, tmp_path, rid)
    assert API_KEY not in json.dumps(msg.as_dict())
    assert API_KEY not in caplog.text
    assert API_KEY in conn.execute("SELECT download_url FROM candidates").fetchone()[0]


def test_rate_limit_fails_cleanly(conn, tmp_path, make_season, prowl):
    """AC-SRCH-05"""
    rid = analysed_season(conn, make_season)
    prowl.mode = "rate_limited"
    with pytest.raises(MsgError) as err:
        run_search(conn, tmp_path, rid)
    assert err.value.msg.code == "search.rate_limited"
    assert len(prowl.requests) == 1


def test_bad_key(conn, tmp_path, make_season, prowl):
    rid = analysed_season(conn, make_season)
    prowl.mode = "bad_key"
    with pytest.raises(MsgError) as err:
        run_search(conn, tmp_path, rid)
    assert err.value.msg.code == "search.auth_failed"


@pytest.mark.parametrize(
    "title, season, expected",
    [
        ("Show.S01E05.1080p", 1, (5, 5)),
        ("Show.S01E05-E06.1080p", 1, (5, 6)),
        ("Show.S01E05E06.1080p", 1, (5, 6)),
        ("Show S1E5", 1, (5, 5)),
        ("Show.S11E05", 1, None),
        ("Show.S01.1080p", 1, None),
    ],
)
def test_episode_range(title, season, expected):
    assert stages.episode_range(title, season) == expected


@pytest.mark.parametrize(
    "title, ok",
    [
        ("Show.S01.1080p", True),
        ("Show S01 FRENCH", True),
        ("Show Saison 1", True),
        ("Show.Season.01", True),
        ("Show.S12", False),
        ("Show saison 10", False),
        ("Show.S01E01", False),
        ("Show.S02", False),
    ],
)
def test_season_pack(title, ok):
    assert stages.is_season_pack(title, 1) is ok


def test_year_must_be_a_whole_number(conn, tmp_path, make_season, prowl):
    rid, _ = make_season([b"m" * 50_000], kind="movie", title="Harbour Lights (2020)", year="2020")
    conn.execute("UPDATE files SET size = 50000")
    prowl.add("Nimbus", "Harbour.Lights.120201.1080p-GRP", 50_000)
    run_search(conn, tmp_path, rid)
    assert candidates(conn, rid) == []


@pytest.mark.parametrize(
    "raw, clean",
    [
        ("┃FR┃ Embrassez qui vous voudrez", "Embrassez qui vous voudrez"),
        ("TOP - Let Her Out", "Let Her Out"),
        ("NF - Octonauts & the Great Barrier Reef (2020)", "Octonauts & the Great Barrier Reef"),
        ("[4K] Dune", "Dune"),
        ("FR | Les Misérables", "Les Misérables"),
        ("|EN| Heat", "Heat"),
        ("M - A Film", "M - A Film"),  # one-letter title kept
        ("Mission: Impossible", "Mission: Impossible"),  # a real colon is not a tag
    ],
)
def test_provider_prefixes_are_removed(raw, clean):
    """Real run: provider tags made every search miss (all title words must match)."""
    assert stages.clean_title(raw) == clean


def test_prefixed_catalogue_title_still_matches(conn, tmp_path, make_season, prowl):
    rid, _ = make_season([b"m" * 50_000], kind="movie", title="┃FR┃ Harbour Lights", year="2020")
    conn.execute("UPDATE files SET size = 50000")
    prowl.add("Nimbus", "Harbour.Lights.2020.1080p.BluRay-GRP", 50_000)
    run_search(conn, tmp_path, rid)
    assert candidates(conn, rid) == [("Harbour.Lights.2020.1080p.BluRay-GRP", "movie", True)]


def test_original_title_from_tmdb_is_searched(conn, tmp_path, make_season, prowl, monkeypatch):
    """A French catalogue title, an English release: TMDB gives the other name."""
    from tests.fakes.tmdb import KEY, FakeTMDB
    from vodarr import services

    with FakeTMDB() as tmdb:
        monkeypatch.setattr(services.TMDB, "BASE", tmdb.url + "/3")
        svc = core.get_setting(conn, "services")
        svc["tmdb"] = {"apiKey": KEY}
        core.set_setting(conn, "services", svc)
        rid, _ = make_season(
            [b"m" * 50_000], kind="movie", title="┃FR┃ Les Feux du Port", year="2020"
        )
        conn.execute("UPDATE files SET size = 50000")
        conn.execute("UPDATE releases SET tmdb_id = '777'")
        prowl.add("Nimbus", "Harbour.Lights.2020.1080p.BluRay-GRP", 50_000)
        run_search(conn, tmp_path, rid)
    assert candidates(conn, rid) == [("Harbour.Lights.2020.1080p.BluRay-GRP", "movie", True)]
    queries = [r.query["query"] for r in prowl.requests if r.path == "/api/v1/search"]
    assert queries == ["les feux du port", "harbour lights"]


def test_wrong_provider_tmdb_id_does_not_change_the_year(
    conn, tmp_path, make_season, prowl, monkeypatch
):
    """Real run: a 2021 film carried the TMDB id of a 2023 film with the same title. TMDB's
    year contradicts the source, so that id is ignored and the 2021 release is the match."""
    from tests.fakes.tmdb import KEY, FakeTMDB
    from vodarr import services

    with FakeTMDB() as tmdb:
        monkeypatch.setattr(services.TMDB, "BASE", tmdb.url + "/3")
        svc = core.get_setting(conn, "services")
        svc["tmdb"] = {"apiKey": KEY}
        core.set_setting(conn, "services", svc)
        rid, _ = make_season(
            [b"m" * 50_000], kind="movie", title="FR - Harbour Lights (2020)", year="2020"
        )
        conn.execute("UPDATE files SET size = 50000")
        conn.execute("UPDATE releases SET tmdb_id = '778'")  # the 2023 film
        prowl.add("Nimbus", "Harbour.Lights.2020.FRENCH.1080p.WEB-GRP", 50_000)
        prowl.add("Nimbus", "Harbour.Lights.2023.1080p.WEB-DL-GRP", 50_000)
        run_search(conn, tmp_path, rid)
    assert candidates(conn, rid) == [("Harbour.Lights.2020.FRENCH.1080p.WEB-GRP", "movie", True)]
