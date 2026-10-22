import os

import pytest

from tests.fakes.torrents import make_torrent, random_bytes, write_local
from vodarr import links, torrent
from vodarr.core import MsgError

PL = 16384
E1, E2 = random_bytes(40000, 1), random_bytes(35000, 2)
FILES = [("Show.S01E01.1080p-GRP.mkv", E1), ("Show.S01E02.1080p-GRP.mkv", E2)]


@pytest.fixture
def season(tmp_path):
    dl = tmp_path / "data" / "vod" / "Src" / "Show" / "S01"
    local = [
        write_local(dl / "Show - S01E01.mkv", E1, b"\0" * 6),
        write_local(dl / "Show - S01E02.mkv", E2),
    ]
    t = torrent.load_torrent(make_torrent("Show.S01.1080p-GRP", FILES, PL))
    report, verdict = torrent.verify(t, torrent.match(t.files, local))
    assert verdict == "identical"
    link_dir = tmp_path / "data" / "links"
    link_dir.mkdir()
    return t, report, local, link_dir


def snapshot(paths):
    return [(p, p.name, p.stat().st_ino) for p in paths]


def test_restore_hardlinks_under_original_names(season):
    """AC-RES-02, AC-RES-03, AC-RES-04"""
    t, report, local, link_dir = season
    before = snapshot(local)
    result = links.restore(
        t, report, link_dir.parent, link_dir, "Nimbus (API)", flat=False, link_type="hardlink"
    )
    root = link_dir / "Nimbus (API)" / "Show.S01.1080p-GRP"
    assert result.root == root
    assert result.save_path == link_dir / "Nimbus (API)"
    for (name, content), src in zip(FILES, local, strict=True):
        target = root / name
        assert target.read_bytes() == content
        assert os.path.samefile(target, src)
    assert snapshot(local) == before  # same paths, names, inodes: nothing moved or copied
    assert local[0].stat().st_size == len(E1)  # NUL padding truncated in place


def test_restore_flat_and_single_file(tmp_path):
    """AC-RES-04: flatLinking, single-file torrent."""
    movie = write_local(tmp_path / "dl" / "Movie (2020).mkv", E1)
    t = torrent.load_torrent(
        make_torrent("Movie.2020.1080p-GRP.mkv", [("Movie.2020.1080p-GRP.mkv", E1)])
    )
    report, _ = torrent.verify(t, torrent.match(t.files, [movie]))
    link_dir = tmp_path / "links"
    result = links.restore(t, report, tmp_path, link_dir, "Aurora", flat=True, link_type="hardlink")
    assert result.save_path == link_dir
    assert result.root == link_dir / "Movie.2020.1080p-GRP.mkv"
    assert os.path.samefile(result.root, movie)


def test_restore_symlink(season):
    t, report, local, link_dir = season
    result = links.restore(
        t, report, link_dir.parent, link_dir, "Nimbus", flat=False, link_type="symlink"
    )
    target = result.root / FILES[0][0]
    assert target.is_symlink()
    assert target.resolve() == local[0].resolve()


def test_reflink_copies_or_fails_explicitly(season):
    t, report, _, link_dir = season
    try:
        result = links.restore(
            t, report, link_dir.parent, link_dir, "Nimbus", flat=False, link_type="reflink"
        )
    except MsgError as e:
        assert e.msg.code == "restore.reflink_unsupported"
    else:
        assert (result.root / FILES[0][0]).read_bytes() == E1


def test_restore_is_idempotent(season):
    """AC-RES-05, AC-RES-07: re-running reuses existing links."""
    t, report, _, link_dir = season
    first = links.restore(
        t, report, link_dir.parent, link_dir, "Nimbus", flat=False, link_type="hardlink"
    )
    second = links.restore(
        t, report, link_dir.parent, link_dir, "Nimbus", flat=False, link_type="hardlink"
    )
    assert (first.root, first.linked) == (second.root, 2)
    assert second.linked == 0 and second.reused == 2


def test_existing_foreign_target_is_never_overwritten(season):
    """AC-RES-05"""
    t, report, _, link_dir = season
    foreign = write_local(link_dir / "Nimbus" / "Show.S01.1080p-GRP" / FILES[1][0], b"someone else")
    with pytest.raises(MsgError) as err:
        links.restore(
            t, report, link_dir.parent, link_dir, "Nimbus", flat=False, link_type="hardlink"
        )
    assert err.value.msg.code == "restore.target_exists"
    assert foreign.read_bytes() == b"someone else"
    # AC-RES-07: the link created before the failure stays
    assert (link_dir / "Nimbus" / "Show.S01.1080p-GRP" / FILES[0][0]).exists()


def test_only_identical_files_are_linked(season):
    """AC-RES-06 base: non-identical files are left to qBittorrent."""
    t, report, local, link_dir = season
    report[1]["status"] = "different"
    result = links.restore(
        t, report, link_dir.parent, link_dir, "Nimbus", flat=False, link_type="hardlink"
    )
    assert (result.root / FILES[0][0]).exists()
    assert not (result.root / FILES[1][0]).exists()


def test_padding_rechecked_before_truncation(season):
    """AC-RES-03: if the excess is no longer NUL-only, nothing is truncated."""
    t, report, local, link_dir = season
    with local[0].open("r+b") as f:
        f.seek(len(E1) + 2)
        f.write(b"X")
    size = local[0].stat().st_size
    with pytest.raises(MsgError) as err:
        links.restore(
            t, report, link_dir.parent, link_dir, "Nimbus", flat=False, link_type="hardlink"
        )
    assert err.value.msg.code == "verify.non_null_padding"
    assert local[0].stat().st_size == size


def test_pick_link_dir_same_filesystem(tmp_path, monkeypatch):
    """AC-RES-04: first linkDir on the same st_dev as downloads."""
    dl, a, b = tmp_path / "dl", tmp_path / "a", tmp_path / "b"
    for p in (dl, a, b):
        p.mkdir()
    devs = {dl: 1, a: 2, b: 1}
    monkeypatch.setattr(links, "_dev", lambda p: devs[p])
    assert links.pick_link_dir([a, b], dl, "hardlink") == b
    assert links.pick_link_dir([a, b], dl, "symlink") == a
    with pytest.raises(MsgError) as err:
        links.pick_link_dir([a], dl, "hardlink")
    assert err.value.msg.code == "restore.no_linkdir_same_fs"
    with pytest.raises(MsgError):
        links.pick_link_dir([tmp_path / "missing"], dl, "symlink")


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Nimbus (API)", "Nimbus (API)"),
        ('a/b\\c:d*e?"f<g>h|i', "a_b_c_d_e__f_g_h_i"),
        ("..", "_"),
        ("", "_"),
    ],
)
def test_tracker_dir_name(name, expected):
    assert links.tracker_dir_name(name) == expected


def restore(t, report, link_dir, **kw):
    kw.setdefault("downloads", link_dir.parent)
    return links.restore(
        t,
        report,
        kw.pop("downloads"),
        link_dir,
        kw.pop("indexer", "Nimbus"),
        flat=False,
        link_type=kw.pop("link_type", "hardlink"),
    )


def test_source_outside_downloads_is_refused(season, tmp_path):
    t, report, local, link_dir = season
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with pytest.raises(MsgError) as err:
        restore(t, report, link_dir, downloads=elsewhere)
    assert err.value.msg.code == "restore.unsafe_path"


def test_symlinked_source_is_refused(season):
    t, report, local, link_dir = season
    alias = local[0].with_name("alias.mkv")
    alias.symlink_to(local[0])
    report[0]["local"] = str(alias)
    with pytest.raises(MsgError) as err:
        restore(t, report, link_dir)
    assert err.value.msg.code == "restore.unsafe_path"
    assert local[0].stat().st_size == len(E1) + 6  # untouched


def test_symlinked_tracker_dir_escaping_link_dir_is_refused(season, tmp_path):
    t, report, _, link_dir = season
    outside = tmp_path / "outside"
    outside.mkdir()
    (link_dir / "Nimbus").symlink_to(outside)
    with pytest.raises(MsgError) as err:
        restore(t, report, link_dir)
    assert err.value.msg.code == "restore.unsafe_path"
    assert not any(outside.iterdir())


def test_destinations_come_from_torrent_not_report(season):
    t, report, _, link_dir = season
    report[0]["path"] = "../../escape.mkv"
    result = restore(t, report, link_dir)
    assert result.linked == 1  # the tampered entry no longer matches a torrent file
    assert not (link_dir.parent / "escape.mkv").exists()


def test_rerun_after_partial_failure_completes(season):
    """AC-RES-07"""
    t, report, _, link_dir = season
    foreign = link_dir / "Nimbus" / "Show.S01.1080p-GRP" / FILES[1][0]
    write_local(foreign, b"x")
    with pytest.raises(MsgError):
        restore(t, report, link_dir)
    foreign.unlink()
    result = restore(t, report, link_dir)
    assert (result.linked, result.reused) == (1, 1)


def test_os_error_becomes_message(season, monkeypatch):
    t, report, _, link_dir = season

    def fail(*_):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(links.os, "link", fail)
    with pytest.raises(MsgError) as err:
        restore(t, report, link_dir)
    assert err.value.msg.code == "restore.failed"


def test_file_without_padding_keeps_mtime(season):
    """AC-RES-02"""
    t, report, local, link_dir = season
    before = local[1].stat()
    restore(t, report, link_dir)
    after = local[1].stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
