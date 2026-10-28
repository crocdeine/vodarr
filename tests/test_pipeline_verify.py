import json
import stat

import pytest

from tests.fakes.prowlarr import API_KEY, FakeProwlarr
from tests.fakes.torrents import make_torrent, make_v2_only_torrent, random_bytes
from vodarr import core, pipeline, stages

NAMES = [f"Northwind.Precinct.S01E0{i}.1080p.WEB-DL-GRP.mkv" for i in (1, 2, 3)]
PACK = "Northwind.Precinct.S01.1080p.WEB-DL-GRP"


@pytest.fixture
def prowl(conn):
    with FakeProwlarr() as srv:
        core.set_setting(conn, "services", {"prowlarr": {"url": srv.url, "apiKey": API_KEY}})
        yield srv


def pack_torrent(contents, name=PACK):
    return make_torrent(name, list(zip(NAMES, contents, strict=False)), 16384)


def setup(conn, tmp_path, make_season, contents, prowl, results, padding=b""):
    """results: [(indexer, title, size, torrent bytes or None)] → runs analyse + search."""
    rid, paths = make_season(contents, padding=padding)
    for i, (indexer, title, size, data) in enumerate(results):
        prowl.add(indexer, title, size, torrent_id=f"t{i}")
        if data is not None:
            prowl.torrents[f"t{i}"] = data
    release = conn.execute("SELECT * FROM releases WHERE id = ?", (rid,)).fetchone()
    ctx = pipeline.Ctx(conn, tmp_path / "config", release, lambda d, t: None)
    stages.stage_analyse(ctx)
    stages.stage_search(ctx)
    return rid, ctx, paths


def cands(conn, rid):
    rows = conn.execute(
        "SELECT indexer, title, scope, status, problem_code FROM candidates WHERE release_id = ?"
        " AND is_candidate = 1 ORDER BY id",
        (rid,),
    )
    return [(r["indexer"], r["title"], r["status"], r["problem_code"]) for r in rows]


def test_identical_pack_with_padding(conn, tmp_path, make_season, season_data, prowl, monkeypatch):
    """AC-VER-04, E1 (verification part)"""
    monkeypatch.setattr(stages, "ffprobe_json", lambda p: "{}")
    total = sum(map(len, season_data))
    rid, ctx, _ = setup(
        conn,
        tmp_path,
        make_season,
        season_data,
        prowl,
        [("Nimbus", PACK, total, pack_torrent(season_data))],
        padding=b"\0" * 6,
    )
    msg = stages.stage_verify(ctx)
    assert cands(conn, rid) == [("Nimbus", PACK, "identical", None)]
    row = conn.execute("SELECT * FROM candidates").fetchone()
    report = json.loads(row["file_report"])
    assert [f["status"] for f in report] == ["identical"] * 3
    torrent_file = tmp_path / "config" / "torrents" / f"{rid}-{row['id']}.torrent"
    assert row["torrent_path"] == str(torrent_file)
    assert stat.S_IMODE(torrent_file.stat().st_mode) == 0o600
    assert len(row["infohash"]) == 40
    assert msg.params == {"identical": 1, "partial": 0, "checked": 1}


def test_stop_at_first_identical_per_tracker_and_scope(
    conn, tmp_path, make_season, season_data, prowl, monkeypatch
):
    """AC-VER-01, E7: closest size first; later .torrent files are not downloaded."""
    monkeypatch.setattr(stages, "ffprobe_json", lambda p: "{}")
    total = sum(map(len, season_data))
    good = pack_torrent(season_data)
    results = [
        ("Nimbus", "Northwind.Precinct.S01.720p-AAA", int(total * 1.1), good),
        ("Nimbus", "Northwind.Precinct.S01.1080p-BBB", total, good),
        ("Nimbus", "Northwind.Precinct.S01.2160p-CCC", int(total * 0.9), good),
        ("Aurora", PACK, total, good),  # other tracker: verified too
    ]
    rid, ctx, _ = setup(conn, tmp_path, make_season, season_data, prowl, results)
    stages.stage_verify(ctx)
    status = {(c[0], c[1]): c[2] for c in cands(conn, rid)}
    assert status[("Nimbus", "Northwind.Precinct.S01.1080p-BBB")] == "identical"
    assert status[("Nimbus", "Northwind.Precinct.S01.720p-AAA")] == "skipped"
    assert status[("Nimbus", "Northwind.Precinct.S01.2160p-CCC")] == "skipped"
    assert status[("Aurora", PACK)] == "identical"
    assert sorted(prowl.downloaded()) == ["t1", "t3"]


def test_partial_candidate(conn, tmp_path, make_season, season_data, prowl, monkeypatch):
    """AC-VER-06, E2"""
    monkeypatch.setattr(stages, "ffprobe_json", lambda p: "{}")
    other = [*season_data[:2], random_bytes(len(season_data[2]), 77)]
    rid, ctx, _ = setup(
        conn,
        tmp_path,
        make_season,
        season_data,
        prowl,
        [("Nimbus", PACK, sum(map(len, season_data)), pack_torrent(other))],
    )
    msg = stages.stage_verify(ctx)
    assert cands(conn, rid)[0][2] == "partial"
    assert msg.params["partial"] == 1


def test_unreadable_and_v2_candidates_are_set_aside(
    conn, tmp_path, make_season, season_data, prowl, monkeypatch
):
    """AC-VER-07: the other candidates still run."""
    monkeypatch.setattr(stages, "ffprobe_json", lambda p: "{}")
    total = sum(map(len, season_data))
    results = [
        ("Nimbus", "Northwind.Precinct.S01.A-X", total, b"garbage"),
        ("Aurora", "Northwind.Precinct.S01.B-Y", total, make_v2_only_torrent("x")),
        ("Cirrus", "Northwind.Precinct.S01.C-Z", total, None),  # 404
        ("Delta", PACK, total, pack_torrent(season_data)),
    ]
    rid, ctx, _ = setup(conn, tmp_path, make_season, season_data, prowl, results)
    stages.stage_verify(ctx)
    assert [c[2:] for c in cands(conn, rid)] == [
        ("error", "verify.unreadable"),
        ("error", "verify.v2_only"),
        ("error", "verify.http_error"),
        ("identical", None),
    ]


def test_no_candidates_skips(conn, tmp_path, make_season, season_data, prowl, monkeypatch):
    """E4"""
    monkeypatch.setattr(stages, "ffprobe_json", lambda p: "{}")
    rid, ctx, _ = setup(conn, tmp_path, make_season, season_data, prowl, [])
    with pytest.raises(pipeline.Skip) as skip:
        stages.stage_verify(ctx)
    assert skip.value.msg.code == "verify.no_candidates"


def test_isolated_episode_and_pack_both_verified(
    conn, tmp_path, make_season, season_data, prowl, monkeypatch
):
    """AC-VER-01, E9: pack on A, single episode on B."""
    monkeypatch.setattr(stages, "ffprobe_json", lambda p: "{}")
    ep2 = make_torrent(NAMES[1], [(NAMES[1], season_data[1])])
    results = [
        ("Nimbus", PACK, sum(map(len, season_data)), pack_torrent(season_data)),
        ("Aurora", "Northwind.Precinct.S01E02.1080p.WEB-DL-GRP", len(season_data[1]), ep2),
    ]
    rid, ctx, _ = setup(conn, tmp_path, make_season, season_data, prowl, results)
    stages.stage_verify(ctx)
    assert [c[2] for c in cands(conn, rid)] == ["identical", "identical"]


def test_download_url_always_goes_to_the_configured_prowlarr(
    conn, tmp_path, make_season, season_data, prowl, monkeypatch
):
    """the key only goes to Prowlarr, whatever host downloadUrl names."""
    from tests.fakes.http import FakeServer

    monkeypatch.setattr(stages, "ffprobe_json", lambda p: "{}")
    with FakeServer() as evil:
        rid, ctx, _ = setup(
            conn,
            tmp_path,
            make_season,
            season_data,
            prowl,
            [("Nimbus", PACK, sum(map(len, season_data)), pack_torrent(season_data))],
        )
        url = conn.execute("SELECT download_url FROM candidates").fetchone()[0]
        foreign = url.replace(prowl.url, evil.url)  # same path, another host (or another name)
        conn.execute("UPDATE candidates SET download_url = ?", (foreign,))
        stages.stage_verify(ctx)
        assert cands(conn, rid)[0][2] == "identical"  # fetched from the configured Prowlarr
        assert evil.requests == []


def test_redirect_to_other_host_drops_the_key(tmp_path):
    from tests.fakes.http import FakeServer, Response, redirect
    from vodarr import services

    with FakeServer() as other, FakeServer() as prowl:
        other.routes[("GET", "/t")] = lambda req: Response(200, b"data")
        prowl.routes[("GET", "/dl")] = lambda req: redirect(other.url + "/t")
        client = services.Prowlarr(prowl.url, "the-api-key")
        assert client.fetch_torrent(prowl.url + "/dl") == b"data"
    assert "x-api-key" not in other.requests[0].headers
    assert prowl.requests[0].headers["x-api-key"] == "the-api-key"


def test_rate_limit_during_verify_stops_the_stage(
    conn, tmp_path, make_season, season_data, prowl, monkeypatch
):
    """AC-SRCH-05 applies to .torrent downloads too."""
    from tests.fakes.http import Response
    from vodarr.core import MsgError

    monkeypatch.setattr(stages, "ffprobe_json", lambda p: "{}")
    total = sum(map(len, season_data))
    rid, ctx, _ = setup(
        conn,
        tmp_path,
        make_season,
        season_data,
        prowl,
        [("Nimbus", PACK, total, None), ("Aurora", PACK, total, None)],
    )
    prowl.routes[("GET", "*")] = lambda req: Response(429, b"slow down")
    with pytest.raises(MsgError) as err:
        stages.stage_verify(ctx)
    assert err.value.msg.code == "verify.rate_limited"
    assert len(prowl.downloaded()) == 1


def test_research_after_seed_keeps_references(
    conn, tmp_path, make_season, season_data, prowl, monkeypatch
):
    """a retry from search must not orphan links or qBittorrent torrents."""
    monkeypatch.setattr(stages, "ffprobe_json", lambda p: "{}")
    rid, ctx, _ = setup(
        conn,
        tmp_path,
        make_season,
        season_data,
        prowl,
        [("Nimbus", PACK, sum(map(len, season_data)), pack_torrent(season_data))],
    )
    stages.stage_verify(ctx)
    conn.execute("UPDATE candidates SET link_root = '/data/links/Nimbus/x', added_to_qbit = 1")
    stages.stage_search(ctx)
    rows = conn.execute("SELECT link_root, added_to_qbit, status FROM candidates").fetchall()
    assert [(r["link_root"], r["added_to_qbit"], r["status"]) for r in rows] == [
        ("/data/links/Nimbus/x", 1, "identical")
    ]
    before = len(prowl.downloaded())
    stages.stage_verify(ctx)
    assert len(prowl.downloaded()) == before  # already in place: not fetched again


def test_checks_per_tracker_can_be_capped(
    conn, tmp_path, make_season, season_data, prowl, monkeypatch
):
    """Real test: 11 .torrent fetched from one tracker for one season (each may be a snatch)."""
    monkeypatch.setattr(stages, "ffprobe_json", lambda p: "{}")
    other = [random_bytes(len(d), 90 + i) for i, d in enumerate(season_data)]
    total = sum(map(len, season_data))
    titles = [f"Northwind.Precinct.S01.1080p.WEB-DL-V{i}" for i in range(3)]
    rid, ctx, _ = setup(
        conn,
        tmp_path,
        make_season,
        season_data,
        prowl,
        [("Nimbus", t, total, pack_torrent(other, t)) for t in titles],
    )
    services = core.get_setting(conn, "services")
    services["prowlarr"]["maxChecksPerTracker"] = 2
    core.set_setting(conn, "services", services)
    stages.stage_verify(ctx)
    statuses = [(s, p) for _, _, s, p in cands(conn, rid)]
    assert statuses.count(("different", None)) == 2
    assert statuses.count(("skipped", "verify.tracker_limit")) == 1
    assert len(list((tmp_path / "config" / "torrents").iterdir())) == 2


def test_new_search_only_checks_new_results(
    conn, tmp_path, make_season, season_data, prowl, monkeypatch
):
    """0.3: a daily search keeps known verdicts and never downloads a .torrent twice."""
    monkeypatch.setattr(stages, "ffprobe_json", lambda p: "{}")
    other = [random_bytes(len(d), 70 + i) for i, d in enumerate(season_data)]
    total = sum(map(len, season_data))
    old = "Northwind.Precinct.S01.1080p.WEB-DL-OLD"
    rid, ctx, _ = setup(
        conn,
        tmp_path,
        make_season,
        season_data,
        prowl,
        [("Nimbus", old, total, pack_torrent(other, old))],
    )
    stages.stage_verify(ctx)
    assert prowl.downloaded() == ["t0"]
    prowl.add("Nimbus", PACK, total, torrent_id="t1")  # the matching release appears later
    prowl.torrents["t1"] = pack_torrent(season_data)
    stages.stage_search(ctx)
    stages.stage_verify(ctx)
    assert prowl.downloaded() == ["t0", "t1"]  # the old one is not fetched again
    assert [(t, s) for _, t, s, _ in cands(conn, rid)] == [(old, "different"), (PACK, "identical")]


def test_explicit_retry_reuses_saved_torrents(
    conn, tmp_path, make_season, season_data, prowl, monkeypatch
):
    monkeypatch.setattr(stages, "ffprobe_json", lambda p: "{}")
    total = sum(map(len, season_data))
    rid, ctx, _ = setup(
        conn,
        tmp_path,
        make_season,
        season_data,
        prowl,
        [("Nimbus", PACK, total, pack_torrent(season_data))],
    )
    conn.execute("UPDATE stages SET state = 'done' WHERE release_id = ?", (rid,))
    stages.stage_verify(ctx)
    pipeline.retry(conn, rid, "verify")  # checks again, from the saved file
    assert [s for _, _, s, _ in cands(conn, rid)] == ["pending"]
    stages.stage_verify(ctx)
    assert [s for _, _, s, _ in cands(conn, rid)] == ["identical"]
    assert prowl.downloaded() == ["t0"]


def test_added_episodes_reclassify_known_results(
    conn, tmp_path, make_season, season_data, prowl, monkeypatch
):
    """Backend audit: a pack too big for 2 episodes became a candidate once all 3 were queued."""
    monkeypatch.setattr(stages, "ffprobe_json", lambda p: "{}")
    total = sum(map(len, season_data))
    rid, ctx, _ = setup(
        conn,
        tmp_path,
        make_season,
        season_data[:2],
        prowl,
        [("Nimbus", PACK, total, pack_torrent(season_data))],
    )
    assert [r["is_candidate"] for r in conn.execute("SELECT is_candidate FROM candidates")] == [0]
    # the third episode arrives (as an enqueue with new files would leave it)
    path = tmp_path / conn.execute("SELECT dir FROM releases").fetchone()[0]
    conn.execute(
        "INSERT INTO files (release_id, stream_id, ext, season, episode, filename, size, dl_status,"
        " queued_at) VALUES (?, '3', 'mkv', 1, 3, 'e3.mkv', ?, 'done', 2)",
        (rid, len(season_data[2])),
    )
    (path / "e3.mkv").write_bytes(season_data[2])
    for i, f in enumerate(conn.execute("SELECT id FROM files ORDER BY id").fetchall()):
        conn.execute("UPDATE files SET size = ? WHERE id = ?", (len(season_data[i]), f["id"]))
    stages.stage_search(ctx)
    assert [r["is_candidate"] for r in conn.execute("SELECT is_candidate FROM candidates")] == [1]
    assert len(cands(conn, rid)) == 1  # updated, not added twice


def test_daily_search_retries_failed_candidates(
    conn, tmp_path, make_season, season_data, prowl, monkeypatch
):
    """Backend audit: a tracker down on the first day left the candidate in error for 30 days."""
    monkeypatch.setattr(stages, "ffprobe_json", lambda p: "{}")
    total = sum(map(len, season_data))
    rid, ctx, _ = setup(
        conn, tmp_path, make_season, season_data, prowl, [("Nimbus", PACK, total, None)]
    )
    stages.stage_verify(ctx)  # no .torrent behind the result: error
    assert [s for _, _, s, _ in cands(conn, rid)] == ["error"]
    prowl.torrents["t0"] = pack_torrent(season_data)  # the tracker is back
    pipeline.search_again(conn, rid, 1_000_000.0)
    stages.stage_verify(ctx)
    assert [s for _, _, s, _ in cands(conn, rid)] == ["identical"]
