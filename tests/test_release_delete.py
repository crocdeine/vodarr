import os

import pytest

from tests.fakes.prowlarr import API_KEY, FakeProwlarr
from tests.fakes.qbittorrent import PASSWORD, USER, FakeQbit
from tests.fakes.torrents import make_torrent
from vodarr import core, pipeline, stages
from vodarr.core import MsgError

NAMES = [f"Northwind.Precinct.S01E0{i}.1080p.WEB-DL-GRP.mkv" for i in (1, 2, 3)]
PACK = "Northwind.Precinct.S01.1080p.WEB-DL-GRP"


@pytest.fixture
def seeded(conn, tmp_path, make_season, season_data, monkeypatch):
    monkeypatch.setattr(stages, "ffprobe_json", lambda p: "{}")
    monkeypatch.setattr(stages, "SEED_POLL", 0)
    link_dir = tmp_path / "data" / "links"
    link_dir.mkdir(parents=True)
    core.set_setting(conn, "crossseed", {"linkDirs": [str(link_dir)]})
    with FakeProwlarr() as prowl, FakeQbit() as qbit:
        core.set_setting(
            conn,
            "services",
            {
                "prowlarr": {"url": prowl.url, "apiKey": API_KEY},
                "qbittorrent": {"url": qbit.url, "username": USER, "password": PASSWORD},
            },
        )
        qbit.next_states = ["uploading"]
        rid, paths = make_season(season_data)
        prowl.add("Nimbus", PACK, sum(map(len, season_data)), torrent_id="t0")
        prowl.torrents["t0"] = make_torrent(PACK, list(zip(NAMES, season_data, strict=True)))
        while pipeline.process_next(conn, tmp_path / "config", pipeline.handlers()):
            pass
        assert pipeline.status(conn, rid) == "done"
        yield rid, paths, link_dir, qbit


def test_delete_keeps_downloaded_files_by_default(conn, tmp_path, seeded):
    """AC-PIPE-05"""
    rid, paths, link_dir, qbit = seeded
    other = link_dir / "Aurora" / "Someone.Else"
    other.mkdir(parents=True)
    stages.delete_release(conn, tmp_path / "config", rid)
    assert [h for h, files in qbit.deleted] and all(files for _, files in qbit.deleted)
    assert not (link_dir / "Nimbus").exists()
    assert other.exists()  # nothing outside this release's links
    assert all(p.exists() for p in paths)
    assert not list((tmp_path / "config" / "torrents").iterdir())
    for table in ("releases", "files", "stages", "candidates"):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0  # noqa: S608


def test_delete_with_files_checkbox(conn, tmp_path, seeded):
    """AC-PIPE-05"""
    rid, paths, link_dir, _ = seeded
    sibling = paths[0].parent.parent / "S02"
    sibling.mkdir()
    stages.delete_release(conn, tmp_path / "config", rid, delete_files=True)
    assert not any(p.exists() for p in paths)
    assert not paths[0].parent.exists()
    assert sibling.exists()


def test_preexisting_torrent_is_never_removed(conn, tmp_path, seeded):
    """AC-SEED-03 + AC-PIPE-05: a torrent VODarr did not add stays in qBittorrent."""
    rid, _, _, qbit = seeded
    conn.execute("UPDATE candidates SET added_to_qbit = 0")
    stages.delete_release(conn, tmp_path / "config", rid)
    assert qbit.deleted == []


def test_link_root_outside_link_dirs_is_not_deleted(conn, tmp_path, seeded):
    rid, _, _, _ = seeded
    victim = tmp_path / "precious"
    victim.mkdir()
    conn.execute("UPDATE candidates SET link_root = ?", (str(victim),))
    with pytest.raises(MsgError) as err:
        stages.delete_release(conn, tmp_path / "config", rid)
    assert err.value.msg.code == "release.unsafe_path"
    assert victim.exists()
    assert conn.execute("SELECT count(*) FROM releases").fetchone()[0] == 1  # nothing half-done
    assert pipeline.status(conn, rid) == "done"


def test_dotdot_in_link_root_is_refused(conn, tmp_path, seeded):
    rid, _, link_dir, qbit = seeded
    conn.execute("UPDATE candidates SET link_root = ?", (str(link_dir / ".." / "elsewhere"),))
    with pytest.raises(MsgError):
        stages.delete_release(conn, tmp_path / "config", rid)
    assert qbit.deleted == []


def test_qbit_unconfigured_refuses_delete(conn, tmp_path, seeded):
    """torrents VODarr added must not be orphaned in qBittorrent."""
    rid, *_ = seeded
    services = core.get_setting(conn, "services")
    services.pop("qbittorrent")
    core.set_setting(conn, "services", services)
    with pytest.raises(MsgError) as err:
        stages.delete_release(conn, tmp_path / "config", rid)
    assert err.value.msg.code == "release.qbit_unconfigured"


def test_torrent_shared_with_another_release_stays(conn, tmp_path, seeded):
    rid, _, _, qbit = seeded
    h = conn.execute("SELECT infohash FROM candidates WHERE infohash IS NOT NULL").fetchone()[0]
    conn.execute(
        "INSERT INTO releases (id, source_id, kind, item_id, season, title, year, dir, created_at) VALUES ('other', (SELECT source_id FROM releases), 'movie', 'x', NULL, 't', NULL, '/d', 0)"
    )
    conn.execute(
        "INSERT INTO candidates (release_id, indexer, title, scope, is_candidate, infohash) VALUES ('other', 'A', 't', 'movie', 1, ?)",
        (h,),
    )
    stages.delete_release(conn, tmp_path / "config", rid)
    assert qbit.deleted == []


def test_busy_release_refused(conn, tmp_path, seeded):
    rid, *_ = seeded
    conn.execute(
        "UPDATE stages SET state = 'running' WHERE release_id = ? AND name = 'seed'", (rid,)
    )
    with pytest.raises(MsgError) as err:
        stages.delete_release(conn, tmp_path / "config", rid)
    assert err.value.msg.code == "release.busy"


def test_hardlinked_files_survive_link_removal(conn, tmp_path, seeded):
    rid, paths, *_ = seeded
    before = [os.stat(p).st_nlink for p in paths]
    stages.delete_release(conn, tmp_path / "config", rid)
    assert [os.stat(p).st_nlink for p in paths] == [n - 1 for n in before]


def test_preexisting_folder_content_survives(conn, tmp_path, seeded):
    """a torrent named like an existing folder must not get it rmtree'd."""
    rid, _, link_dir, _ = seeded
    root = link_dir / "Nimbus" / "Northwind.Precinct.S01.1080p.WEB-DL-GRP"
    keep = root / "someone-elses-file.mkv"
    keep.write_bytes(b"keep me")
    stages.delete_release(conn, tmp_path / "config", rid)
    assert keep.read_bytes() == b"keep me"
    assert sorted(p.name for p in root.iterdir()) == ["someone-elses-file.mkv"]
