import contextlib
import os

import pytest

from tests.fakes.prowlarr import API_KEY, FakeProwlarr
from tests.fakes.torrents import make_torrent, random_bytes
from vodarr import core, pipeline, stages
from vodarr.core import MsgError

NAMES = [f"Northwind.Precinct.S01E0{i}.1080p.WEB-DL-GRP.mkv" for i in (1, 2, 3)]
PACK = "Northwind.Precinct.S01.1080p.WEB-DL-GRP"


@pytest.fixture
def env(conn, tmp_path, make_season, season_data, monkeypatch):
    monkeypatch.setattr(stages, "ffprobe_json", lambda p: "{}")
    link_dir = tmp_path / "data" / "links"
    link_dir.mkdir(parents=True)
    core.set_setting(conn, "crossseed", {"linkDirs": [str(link_dir)]})
    with FakeProwlarr() as srv:
        core.set_setting(conn, "services", {"prowlarr": {"url": srv.url, "apiKey": API_KEY}})
        yield srv, link_dir


def through_verify(conn, tmp_path, make_season, contents, prowl, results, padding=b""):
    rid, paths = make_season(contents, padding=padding)
    for i, (indexer, title, size, data) in enumerate(results):
        prowl.add(indexer, title, size, torrent_id=f"t{i}")
        prowl.torrents[f"t{i}"] = data
    release = conn.execute("SELECT * FROM releases WHERE id = ?", (rid,)).fetchone()
    ctx = pipeline.Ctx(conn, tmp_path / "config", release, lambda d, t: None)
    for stage in (stages.stage_analyse, stages.stage_search, stages.stage_verify):
        with contextlib.suppress(pipeline.Skip):
            stage(ctx)
    return rid, ctx, paths


def test_identical_reference_is_linked(conn, tmp_path, make_season, season_data, env):
    """AC-RES-02, AC-RES-04"""
    prowl, link_dir = env
    total = sum(map(len, season_data))
    rid, ctx, paths = through_verify(
        conn,
        tmp_path,
        make_season,
        season_data,
        prowl,
        [("Nimbus", PACK, total, make_torrent(PACK, list(zip(NAMES, season_data, strict=True))))],
        padding=b"\0" * 6,
    )
    inodes = [p.stat().st_ino for p in paths]
    msg = stages.stage_restore(ctx)
    root = link_dir / "Nimbus" / PACK
    assert all(os.path.samefile(root / n, p) for n, p in zip(NAMES, paths, strict=True))
    assert [p.stat().st_ino for p in paths] == inodes
    assert conn.execute("SELECT link_root FROM candidates").fetchone()[0] == str(root)
    assert msg.params == {"references": 1, "files": 3}


def test_no_reference_touches_nothing(conn, tmp_path, make_season, season_data, env):
    """AC-RES-01, E4"""
    prowl, link_dir = env
    rid, ctx, paths = through_verify(
        conn, tmp_path, make_season, season_data, prowl, [], padding=b"\0" * 6
    )
    sizes = [p.stat().st_size for p in paths]
    with pytest.raises(pipeline.Skip) as skip:
        stages.stage_restore(ctx)
    assert skip.value.msg.code == "restore.no_reference"
    assert [p.stat().st_size for p in paths] == sizes
    assert not any(link_dir.iterdir())


def test_partial_waits_for_user_then_completes(conn, tmp_path, make_season, season_data, env):
    """AC-RES-01, AC-RES-06, AC-PIPE-04, E2"""
    prowl, link_dir = env
    # E03 was provided in another format: different content and size locally.
    local = [*season_data[:2], random_bytes(len(season_data[2]) - 500, 7)]
    total = sum(map(len, season_data))
    rid, ctx, _ = through_verify(
        conn,
        tmp_path,
        make_season,
        local,
        prowl,
        [("Nimbus", PACK, total, make_torrent(PACK, list(zip(NAMES, season_data, strict=True))))],
    )
    with pytest.raises(pipeline.Skip) as skip:
        stages.stage_restore(ctx)
    assert skip.value.msg.code == "restore.partial_available"
    assert skip.value.msg.params == {"trackers": "Nimbus"}
    conn.execute("UPDATE candidates SET complete_requested = 1")
    stages.stage_restore(ctx)
    root = link_dir / "Nimbus" / PACK
    assert sorted(p.name for p in root.iterdir()) == NAMES[:2]


def test_no_link_dir_configured_skips(conn, tmp_path, make_season, season_data, env):
    """AC-PIPE-02"""
    prowl, _ = env
    core.set_setting(conn, "crossseed", {"linkDirs": []})
    total = sum(map(len, season_data))
    rid, ctx, _ = through_verify(
        conn,
        tmp_path,
        make_season,
        season_data,
        prowl,
        [("Nimbus", PACK, total, make_torrent(PACK, list(zip(NAMES, season_data, strict=True))))],
    )
    with pytest.raises(pipeline.Skip) as skip:
        stages.stage_restore(ctx)
    assert skip.value.msg.code == "restore.no_linkdir_configured"


def test_two_trackers_two_trees_same_files(conn, tmp_path, make_season, season_data, env):
    """E3"""
    prowl, link_dir = env
    total = sum(map(len, season_data))
    other_names = [n.replace("GRP", "OTHER") for n in NAMES]
    results = [
        ("Nimbus", PACK, total, make_torrent(PACK, list(zip(NAMES, season_data, strict=True)))),
        (
            "Aurora",
            "Northwind.Precinct.S01.1080p-OTHER",
            total,
            make_torrent(
                "Northwind.Precinct.S01.1080p-OTHER",
                list(zip(other_names, season_data, strict=True)),
            ),
        ),
    ]
    rid, ctx, paths = through_verify(conn, tmp_path, make_season, season_data, prowl, results)
    stages.stage_restore(ctx)
    for tracker, name, files in (
        ("Nimbus", PACK, NAMES),
        ("Aurora", "Northwind.Precinct.S01.1080p-OTHER", other_names),
    ):
        assert all(
            os.path.samefile(link_dir / tracker / name / f, p)
            for f, p in zip(files, paths, strict=True)
        )


def test_restore_failure_keeps_links_and_rerun_completes(
    conn, tmp_path, make_season, season_data, env
):
    """AC-RES-07"""
    prowl, link_dir = env
    total = sum(map(len, season_data))
    rid, ctx, paths = through_verify(
        conn,
        tmp_path,
        make_season,
        season_data,
        prowl,
        [("Nimbus", PACK, total, make_torrent(PACK, list(zip(NAMES, season_data, strict=True))))],
    )
    blocker = link_dir / "Nimbus" / PACK / NAMES[2]
    blocker.parent.mkdir(parents=True)
    blocker.write_bytes(b"foreign")
    with pytest.raises(MsgError) as err:
        stages.stage_restore(ctx)
    assert err.value.msg.code == "restore.target_exists"
    assert (link_dir / "Nimbus" / PACK / NAMES[0]).exists()
    blocker.unlink()
    stages.stage_restore(ctx)
    assert all(paths[i].exists() for i in range(3))
