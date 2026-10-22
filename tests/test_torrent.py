import hashlib

import pytest

from tests.fakes.torrents import (
    bencode,
    make_info,
    make_torrent,
    make_v2_only_torrent,
    random_bytes,
    write_local,
)
from vodarr import torrent
from vodarr.core import MsgError

PL = 16384
E1, E2, E3 = random_bytes(40000, 1), random_bytes(35000, 2), random_bytes(30001, 3)
SEASON = [
    ("Show.S01.1080p-GRP/Show.S01E01.1080p-GRP.mkv", E1),
    ("Show.S01.1080p-GRP/Show.S01E02.1080p-GRP.mkv", E2),
    ("Show.S01.1080p-GRP/Show.S01E03.1080p-GRP.mkv", E3),
]


def season_torrent(extra=()):
    files = [(p.split("/", 1)[1], c) for p, c in SEASON] + list(extra)
    return torrent.load_torrent(make_torrent("Show.S01.1080p-GRP", files, PL))


def local_season(tmp_path, padding=b"\0" * 6, skip=()):
    out = []
    for i, (_, content) in enumerate(SEASON, start=1):
        if i in skip:
            continue
        out.append(write_local(tmp_path / f"Show - S01E0{i} - IPTV.mkv", content, padding))
    return out


# --- T5: reading and matching -------------------------------------------------


def test_load_multi_file_torrent_and_infohash():
    files = [(p.split("/", 1)[1], c) for p, c in SEASON]
    data = make_torrent("Show.S01.1080p-GRP", files, PL)
    t = torrent.load_torrent(data)
    assert t.name == "Show.S01.1080p-GRP"
    assert t.multi
    assert [f.path for f in t.files] == [p.split("/", 1)[1] for p, _ in SEASON]
    assert t.piece_length == PL
    expected = hashlib.sha1(bencode(make_info("Show.S01.1080p-GRP", files, PL))).hexdigest()
    assert t.infohash == expected


def test_load_single_file_torrent():
    t = torrent.load_torrent(make_torrent("Movie.2020.mkv", [("Movie.2020.mkv", E1)]))
    assert not t.multi
    assert [(f.path, f.length) for f in t.files] == [("Movie.2020.mkv", len(E1))]


def test_infohash_uses_raw_info_bytes_even_if_not_canonical():
    # Unsorted keys: re-encoding would change the hash; the raw slice must be used.
    info = b"d4:name1:x6:lengthi1e12:piece lengthi16384e6:pieces20:" + b"\0" * 20 + b"e"
    data = b"d4:info" + info + b"e"
    assert torrent.load_torrent(data).infohash == hashlib.sha1(info).hexdigest()


def test_v2_only_torrent_rejected():
    """AC-VER-07"""
    with pytest.raises(MsgError) as err:
        torrent.load_torrent(make_v2_only_torrent("Show.S01"))
    assert err.value.msg.code == "verify.v2_only"


@pytest.mark.parametrize(
    "data", [b"", b"garbage", b"d4:infoi1ee", b"d4:infod4:name1:xee", b"l" * 5000]
)
def test_unreadable_torrent_rejected(data):
    """AC-VER-07"""
    with pytest.raises(MsgError) as err:
        torrent.load_torrent(data)
    assert err.value.msg.code == "verify.unreadable"


@pytest.mark.parametrize("bad", ["../evil.mkv", "a/../../b", "/abs.mkv", ""])
def test_unsafe_paths_rejected(bad):
    files = [{"path": bad.split("/"), "length": 1}]
    info = {"name": "x", "piece length": PL, "pieces": b"\0" * 20, "files": files}
    with pytest.raises(MsgError) as err:
        torrent.load_torrent(bencode({"info": info}))
    assert err.value.msg.code == "verify.unreadable"


def test_unsafe_name_rejected():
    info = {"name": "..", "piece length": PL, "pieces": b"\0" * 20, "length": 1}
    with pytest.raises(MsgError):
        torrent.load_torrent(bencode({"info": info}))


def test_match_by_episode_tag(tmp_path):
    """AC-VER-02"""
    local = local_season(tmp_path)
    t = season_torrent()
    mapping = torrent.match(t.files, local)
    assert [mapping[f.path].name for f in t.files] == [p.name for p in local]


def test_match_single_file_episode_uses_torrent_name(tmp_path):
    """AC-VER-02: single-file torrent, tag read from the torrent name."""
    local = local_season(tmp_path)
    t = torrent.load_torrent(
        make_torrent("Show.S01E02.1080p-GRP.mkv", [("Show.S01E02.1080p-GRP.mkv", E2)])
    )
    assert torrent.match(t.files, local)[t.files[0].path] == local[1]


def test_match_movie_by_size_within_64_bytes(tmp_path):
    """AC-VER-02"""
    movie = write_local(tmp_path / "Movie (2020).mkv", E1, b"\0" * 64)
    other = write_local(tmp_path / "Other.mkv", E2)
    t = torrent.load_torrent(
        make_torrent("Movie.2020.1080p-GRP.mkv", [("Movie.2020.1080p-GRP.mkv", E1)])
    )
    assert torrent.match(t.files, [movie, other]) == {t.files[0].path: movie}


def test_match_size_beyond_64_bytes_or_smaller_is_unmatched(tmp_path):
    movie = write_local(tmp_path / "Movie.mkv", E1, b"\0" * 65)
    t = torrent.load_torrent(make_torrent("Movie.mkv", [("Movie.mkv", E1)]))
    assert torrent.match(t.files, [movie]) == {"Movie.mkv": None}


def test_ambiguous_match_is_unmatched(tmp_path):
    """AC-VER-02"""
    a = write_local(tmp_path / "a.mkv", E1)
    b = write_local(tmp_path / "b.mkv", E1)
    t = torrent.load_torrent(make_torrent("Movie.mkv", [("Movie.mkv", E1)]))
    assert torrent.match(t.files, [a, b]) == {"Movie.mkv": None}


# --- T6: verification ---------------------------------------------------------


def test_check_padding(tmp_path):
    """AC-VER-03"""
    ok = write_local(tmp_path / "ok", b"abc", b"\0\0")
    bad = write_local(tmp_path / "bad", b"abc", b"\0x")
    assert torrent.check_padding(ok, 3) is None
    assert torrent.check_padding(ok, 5) is None
    assert torrent.check_padding(ok, 6) == "verify.too_short"
    assert torrent.check_padding(bad, 3) == "verify.non_null_padding"


def statuses(report):
    return [f["status"] for f in report]


def test_identical_season_with_nul_padding(tmp_path):
    """AC-VER-04, AC-VER-05"""
    t = season_torrent()
    report, verdict = torrent.verify(t, torrent.match(t.files, local_season(tmp_path)))
    assert statuses(report) == ["identical"] * 3
    assert verdict == "identical"
    assert report[0]["local"].endswith("S01E01 - IPTV.mkv")


def test_non_null_padding_marks_file_different(tmp_path):
    """AC-VER-03, E6"""
    local = local_season(tmp_path)
    write_local(local[1], E2, b"\0\0X")
    t = season_torrent()
    report, verdict = torrent.verify(t, torrent.match(t.files, local))
    assert statuses(report) == ["identical", "different", "identical"]
    assert verdict == "partial"


def test_each_file_gets_a_status(tmp_path):
    """AC-VER-04: a broken file does not stop the others."""
    local = local_season(tmp_path, skip=(3,))
    corrupted = bytearray(E2)
    corrupted[20000] ^= 0xFF
    write_local(local[1], bytes(corrupted))
    write_local(tmp_path / "Show - S01E03.mkv", E3[:-10])
    local.append(tmp_path / "Show - S01E03.mkv")
    t = season_torrent()
    report, verdict = torrent.verify(t, torrent.match(t.files, local))
    assert statuses(report) == ["identical", "different", "too_short"]
    assert verdict == "partial"


def test_missing_video_is_partial(tmp_path):
    """AC-VER-06"""
    t = season_torrent()
    report, verdict = torrent.verify(t, torrent.match(t.files, local_season(tmp_path, skip=(2,))))
    assert statuses(report) == ["identical", "missing", "identical"]
    assert verdict == "partial"


def test_nothing_identical_is_different(tmp_path):
    t = season_torrent()
    other = write_local(tmp_path / "Show - S01E01.mkv", random_bytes(len(E1), 99))
    report, verdict = torrent.verify(t, torrent.match(t.files, [other]))
    assert verdict == "different"


def test_missing_small_annexes_are_identical_annex(tmp_path):
    """AC-VER-05, E5: .nfo and sample missing locally."""
    sample = random_bytes(300, 7)  # < 1 % of the biggest video
    nfo = b"release notes" * 10
    files = [
        ("Movie.2020.1080p-GRP.mkv", E1),
        ("Movie.2020.1080p-GRP.nfo", nfo),
        ("Sample/sample.mkv", sample),
    ]
    t = torrent.load_torrent(make_torrent("Movie.2020.1080p-GRP", files, PL))
    movie = write_local(tmp_path / "Movie (2020).mkv", E1)
    report, verdict = torrent.verify(t, torrent.match(t.files, [movie]))
    assert statuses(report) == ["identical", "missing", "missing"]
    assert [f["annex"] for f in report] == [False, True, True]
    assert verdict == "identical_annex"


def test_big_non_video_missing_is_not_annex(tmp_path):
    big = random_bytes(1024 * 1024, 8)
    files = [("Movie.mkv", E1), ("Extras.zip", big)]
    t = torrent.load_torrent(make_torrent("Movie", files, PL))
    movie = write_local(tmp_path / "m.mkv", E1)
    report, verdict = torrent.verify(t, torrent.match(t.files, [movie]))
    assert verdict == "partial"


def test_progress_callback_reaches_total(tmp_path):
    t = season_torrent()
    seen = []
    torrent.verify(
        t, torrent.match(t.files, local_season(tmp_path)), progress=lambda k, n: seen.append((k, n))
    )
    assert seen[-1][0] == seen[-1][1]


# --- hardening (checkpoint A review) -------------------------------------------


def raw_torrent(info):
    return bencode({"info": info})


def test_too_few_pieces_rejected():
    """AC-VER-04: every byte must be covered by a piece hash."""
    info = make_info("Movie.mkv", [("Movie.mkv", E1)], PL)
    info["pieces"] = info["pieces"][:20]
    with pytest.raises(MsgError) as err:
        torrent.load_torrent(raw_torrent(info))
    assert err.value.msg.code == "verify.unreadable"


@pytest.mark.parametrize(
    "files",
    [
        [{"path": ["a.mkv"], "length": 1}, {"path": ["a.mkv"], "length": 1}],
        [{"path": ["a"], "length": 1}, {"path": ["a", "b.mkv"], "length": 1}],
        [{"path": ["a.mkv"], "length": b"5"}],
        [{"path": ["a.mkv"], "length": -1}],
        [],
    ],
)
def test_bad_file_lists_rejected(files):
    info = {"name": "x", "piece length": PL, "pieces": b"\0" * 20, "files": files}
    with pytest.raises(MsgError):
        torrent.load_torrent(raw_torrent(info))


@pytest.mark.parametrize("raw", [b"i01e", b"i-0e", b"i 1e", b"i1_0e", b"-1:x"])
def test_non_canonical_bencode_rejected(raw):
    with pytest.raises(ValueError):
        torrent.bdecode(raw)


def test_huge_piece_length_rejected():
    info = {"name": "x", "piece length": 1 << 40, "pieces": b"\0" * 20, "length": 1}
    with pytest.raises(MsgError):
        torrent.load_torrent(raw_torrent(info))


def test_small_file_with_no_checkable_piece_is_unverified(tmp_path):
    """AC-VER-04: a file all of whose pieces touch missing files is never 'identical'."""
    srt = b"wrong subtitles" * 7
    files = [
        ("a.S01E01.mkv", E1),
        ("a.S01E01.srt", random_bytes(len(srt), 5)),
        ("a.S01E02.mkv", E2),
    ]
    t = torrent.load_torrent(make_torrent("pack", files, PL))
    local = write_local(tmp_path / "x.srt", srt)
    report, verdict = torrent.verify(
        t, {"a.S01E01.mkv": None, "a.S01E01.srt": local, "a.S01E02.mkv": None}
    )
    assert statuses(report) == ["missing", "unverified", "missing"]
    assert verdict == "different"


def test_one_local_file_never_backs_two_torrent_files(tmp_path):
    """AC-VER-02"""
    files = [("A.mkv", E1), ("B.mkv", E1[:-10])]
    t = torrent.load_torrent(make_torrent("pack", files, PL))
    local = write_local(tmp_path / "x.mkv", E1)
    assert torrent.match(t.files, [local]) == {"A.mkv": None, "B.mkv": None}


def test_tag_absent_locally_falls_back_to_size(tmp_path):
    """AC-VER-02"""
    t = torrent.load_torrent(make_torrent("Show.S01E01.mkv", [("Show.S01E01.mkv", E1)]))
    local = write_local(tmp_path / "episode one.mkv", E1)
    assert torrent.match(t.files, [local]) == {"Show.S01E01.mkv": local}


def test_sample_needs_a_whole_word():
    """AC-VER-05: a title containing 'Samples' is not an annex."""
    files = [("Samples.of.Love.2020.mkv", E1), ("Sample/samples-sample.mkv", b"x" * 100)]
    t = torrent.load_torrent(make_torrent("Samples.of.Love.2020", files, PL))
    assert torrent.is_annex(t) == {
        "Samples.of.Love.2020.mkv": False,
        "Sample/samples-sample.mkv": True,
    }


def test_verify_too_short_status(tmp_path):
    t = torrent.load_torrent(make_torrent("Movie.mkv", [("Movie.mkv", E1)]))
    local = write_local(tmp_path / "m.mkv", E1[:-1])
    report, verdict = torrent.verify(t, {"Movie.mkv": local})
    assert statuses(report) == ["too_short"] and verdict == "different"
