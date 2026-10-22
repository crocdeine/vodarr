from tests.fakes.torrents import bencode, make_info, make_torrent, pieces, random_bytes


def test_bencode_sorts_keys_and_encodes_types():
    assert bencode({"b": 1, "a": [b"x", "y"]}) == b"d1:al1:x1:ye1:bi1ee"


def test_random_bytes_are_stable_per_seed():
    assert random_bytes(64, 1) == random_bytes(64, 1) != random_bytes(64, 2)


def test_pieces_cover_concatenated_files():
    a, b = random_bytes(20000, 1), random_bytes(13000, 2)
    info = make_info("Show.S01", [("Show.S01E01.mkv", a), ("Show.S01E02.mkv", b)], 16384)
    assert info["pieces"] == pieces(a + b, 16384)
    assert len(info["pieces"]) == 20 * 3
    assert [f["length"] for f in info["files"]] == [20000, 13000]


def test_single_file_layout_and_stable_hash():
    data = random_bytes(5000, 3)
    info = make_info("Movie.2020.mkv", [("Movie.2020.mkv", data)])
    assert info["length"] == 5000 and "files" not in info
    t1 = make_torrent("Movie.2020.mkv", [("Movie.2020.mkv", data)])
    t2 = make_torrent("Movie.2020.mkv", [("Movie.2020.mkv", data)])
    assert t1 == t2
