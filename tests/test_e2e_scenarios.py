"""Spec §9: full pipeline on fake servers, from the Xtream download to the qBittorrent seed."""

import json
import os

import pytest

from tests.fakes.prowlarr import API_KEY, FakeProwlarr
from tests.fakes.qbittorrent import PASSWORD as QPASS
from tests.fakes.qbittorrent import USER as QUSER
from tests.fakes.qbittorrent import FakeQbit
from tests.fakes.torrents import make_torrent, random_bytes
from tests.fakes.xtream import PASSWORD, USER, FakeXtream
from vodarr import core, downloads, pipeline, sources, stages

TITLE = "Northwind Precinct"


class Stack:
    def __init__(self, conn, tmp_path, xtream, prowl, qbit):
        self.conn, self.tmp, self.xtream, self.prowl, self.qbit = (
            conn,
            tmp_path,
            xtream,
            prowl,
            qbit,
        )
        self.config = tmp_path / "config"
        self.downloads = tmp_path / "data" / "vod"
        self.links = tmp_path / "data" / "links"
        self.links.mkdir(parents=True)
        core.set_setting(conn, "crossseed", {"linkDirs": [str(self.links)]})
        core.set_setting(
            conn,
            "services",
            {
                "prowlarr": {"url": prowl.url, "apiKey": API_KEY},
                "qbittorrent": {"url": qbit.url, "username": QUSER, "password": QPASS},
            },
        )
        self.source = sources.add(
            conn, "My IPTV", "xtream", {"server": xtream.url, "user": USER, "password": PASSWORD}
        )
        self.n = 0

    def season(self, contents, padding=b"", episodes=None, season=1):
        """Serve the episodes on the fake Xtream and queue them. Returns the release id."""
        eps = []
        for num, content in zip(episodes or range(1, len(contents) + 1), contents, strict=True):
            sid = f"{season}{num:03d}"
            self.xtream.files[sid] = content + padding
            eps.append(
                {"id": sid, "num": num, "title": f"{TITLE} - S{season:02d}E{num:02d}", "ext": "mkv"}
            )
        return downloads.enqueue(
            self.conn, self.source, "series", "100", TITLE, "2021", season, eps, self.downloads
        )

    def movie(self, content, title="Harbour Lights (2020)", padding=b""):
        self.xtream.files["500"] = content + padding
        return downloads.enqueue(
            self.conn,
            self.source,
            "movie",
            "500",
            title,
            "2020",
            None,
            [{"id": "500", "num": None, "title": title, "ext": "mkv"}],
            self.downloads,
        )

    def offer(self, indexer, title, files, name=None, size=None):
        """A tracker result whose .torrent holds `files` = [(path, content)]."""
        self.n += 1
        tid = f"t{self.n}"
        self.prowl.add(indexer, title, size or sum(len(c) for _, c in files), torrent_id=tid)
        self.prowl.torrents[tid] = make_torrent(name or title, files)
        return tid

    def run(self):
        while downloads.process_next(self.conn, self.config, sleep=lambda s: None):
            pass
        while pipeline.process_next(self.conn, self.config, pipeline.handlers()):
            pass

    def stages(self, rid):
        rows = self.conn.execute(
            "SELECT name, state, msg_code FROM stages WHERE release_id = ?", (rid,)
        )
        return {r["name"]: (r["state"], r["msg_code"]) for r in rows}

    def local(self, rid):
        folder = self.conn.execute("SELECT dir FROM releases WHERE id = ?", (rid,)).fetchone()[0]
        names = [
            r[0]
            for r in self.conn.execute(
                "SELECT filename FROM files WHERE release_id = ? ORDER BY id", (rid,)
            )
        ]
        return [os.path.join(folder, n) for n in names]

    def statuses(self, rid):
        rows = self.conn.execute(
            "SELECT indexer, scope, status FROM candidates WHERE release_id = ? AND is_candidate = 1"
            " ORDER BY id",
            (rid,),
        )
        return [(r["indexer"], r["scope"], r["status"]) for r in rows]


@pytest.fixture
def stack(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(stages, "SEED_POLL", 0)
    monkeypatch.setattr(stages, "probe_release", lambda p: None)
    with FakeXtream() as xtream, FakeProwlarr() as prowl, FakeQbit() as qbit:
        qbit.next_states = ["checkingUP", "uploading"]
        yield Stack(conn, tmp_path, xtream, prowl, qbit)


def eps(n, seed=0, size=40_000):
    return [random_bytes(size + i * 1000, seed + i) for i in range(n)]


def names(n, team="GRP", season=1):
    return [
        f"Northwind.Precinct.S{season:02d}E{i:02d}.1080p.WEB-DL-{team}.mkv" for i in range(1, n + 1)
    ]


PACK = "Northwind.Precinct.S01.1080p.WEB-DL-GRP"


def test_e1_identical_season_with_padding(stack):
    data = eps(3)
    rid = stack.season(data, padding=b"\0" * 6)
    stack.offer("Nimbus", PACK, list(zip(names(3), data, strict=True)))
    stack.run()
    local = stack.local(rid)
    inodes = [os.stat(p).st_ino for p in local]
    assert pipeline.status(stack.conn, rid) == "done"
    root = stack.links / "Nimbus" / PACK
    for name, path, content in zip(names(3), local, data, strict=True):
        assert os.path.samefile(root / name, path)
        assert os.path.getsize(path) == len(content)  # padding removed in place
        assert os.path.basename(path).startswith("Northwind Precinct - S01E")  # IPTV name kept
    assert [os.stat(p).st_ino for p in local] == inodes
    assert len(stack.qbit.added) == 1


def test_e2_partial_then_complete(stack):
    data = eps(4)
    local = [*data[:3], random_bytes(len(data[3]) - 700, 99)]  # E04 in another format
    rid = stack.season(local)
    stack.offer("Nimbus", PACK, list(zip(names(4), data, strict=True)))
    stack.run()
    assert pipeline.status(stack.conn, rid) == "action_required"
    assert stack.qbit.added == []
    stack.conn.execute("UPDATE candidates SET complete_requested = 1 WHERE release_id = ?", (rid,))
    pipeline.retry(stack.conn, rid, "restore")
    stack.run()
    assert pipeline.status(stack.conn, rid) == "done"
    linked = sorted(p.name for p in (stack.links / "Nimbus" / PACK).iterdir())
    assert linked == names(4)[:3]
    assert len(stack.qbit.added) == 1  # qBittorrent completes E04 itself


def test_e3_two_trackers_two_trees(stack):
    data = eps(3)
    rid = stack.season(data)
    stack.offer("Nimbus", PACK, list(zip(names(3), data, strict=True)))
    other = "Northwind.Precinct.S01.1080p.WEB.H264-OTHER"
    stack.offer("Aurora", other, list(zip(names(3, team="OTHER"), data, strict=True)))
    stack.run()
    local = stack.local(rid)
    for tracker, pack, files in (("Nimbus", PACK, names(3)), ("Aurora", other, names(3, "OTHER"))):
        assert all(
            os.path.samefile(stack.links / tracker / pack / f, p)
            for f, p in zip(files, local, strict=True)
        )
    assert len(stack.qbit.added) == 2


def test_e4_no_release(stack):
    data = eps(3)
    rid = stack.season(data, padding=b"\0" * 6)
    stack.run()
    s = stack.stages(rid)
    assert s["verify"] == ("skipped", "verify.no_candidates")
    assert s["restore"] == ("skipped", "restore.no_reference")
    assert s["seed"] == ("skipped", "seed.nothing")
    assert [os.path.getsize(p) for p in stack.local(rid)] == [len(c) + 6 for c in data]  # untouched
    assert pipeline.status(stack.conn, rid) == "done"


def test_e5_movie_with_missing_annexes(stack):
    movie = random_bytes(120_000, 5)
    rid = stack.movie(movie)
    name = "Harbour.Lights.2020.1080p.BluRay.x264-GRP"
    stack.offer(
        "Nimbus",
        name,
        [
            (f"{name}.mkv", movie),
            (f"{name}.nfo", b"notes" * 20),
            ("Sample/sample.mkv", random_bytes(500, 6)),
        ],
    )
    stack.run()
    assert stack.statuses(rid) == [("Nimbus", "movie", "identical_annex")]
    assert os.path.samefile(stack.links / "Nimbus" / name / f"{name}.mkv", stack.local(rid)[0])
    assert pipeline.status(stack.conn, rid) == "done"


def test_e6_non_null_padding_touches_nothing(stack):
    movie = random_bytes(60_000, 7)
    rid = stack.movie(movie, padding=b"\0\0X")
    name = "Harbour.Lights.2020.1080p.BluRay.x264-GRP"
    stack.offer("Nimbus", name, [(f"{name}.mkv", movie)], name=f"{name}.mkv")
    stack.run()
    report = json.loads(stack.conn.execute("SELECT file_report FROM candidates").fetchone()[0])
    assert report[0]["status"] == "different"
    assert os.path.getsize(stack.local(rid)[0]) == len(movie) + 3
    assert not any(stack.links.iterdir())
    assert stack.qbit.added == []


def test_e7_stop_at_first_identical(stack):
    data = eps(3)
    total = sum(map(len, data))
    rid = stack.season(data)
    files = list(zip(names(3), data, strict=True))
    first = stack.offer("Nimbus", "Northwind.Precinct.S01.1080p-AAA", files, name=PACK, size=total)
    stack.offer(
        "Nimbus", "Northwind.Precinct.S01.720p-BBB", files, name=PACK, size=int(total * 1.05)
    )
    stack.offer(
        "Nimbus", "Northwind.Precinct.S01.2160p-CCC", files, name=PACK, size=int(total * 0.9)
    )
    stack.run()
    assert stack.prowl.downloaded() == [first]
    assert [s for _, _, s in stack.statuses(rid)] == ["identical", "skipped", "skipped"]


def test_e8_wrong_season_results(stack):
    data = eps(3)
    rid = stack.season(data)
    total = sum(map(len, data))
    stack.offer(
        "Nimbus",
        "Northwind.Precinct.S12.1080p-GRP",
        list(zip(names(3), data, strict=True)),
        size=total,
    )
    stack.offer(
        "Nimbus", "Northwind Precinct Saison 10", list(zip(names(3), data, strict=True)), size=total
    )
    stack.offer(
        "Nimbus", "Northwind.Precinct.S02E01.1080p-GRP", [(names(1)[0], data[0])], size=len(data[0])
    )
    stack.run()
    assert stack.statuses(rid) == []
    assert stack.prowl.downloaded() == []


def test_e9_pack_and_isolated_episode(stack):
    data = eps(3)
    rid = stack.season(data)
    stack.offer("Nimbus", PACK, list(zip(names(3), data, strict=True)))
    single = names(3, team="SOLO")[1]
    stack.offer("Aurora", single.removesuffix(".mkv"), [(single, data[1])], name=single)
    stack.run()
    assert sorted(stack.statuses(rid)) == [
        ("Aurora", "S01E02", "identical"),
        ("Nimbus", "pack", "identical"),
    ]
    local = stack.local(rid)
    assert os.path.samefile(stack.links / "Aurora" / single, local[1])
    assert os.path.samefile(stack.links / "Nimbus" / PACK / names(3)[1], local[1])
    assert len(stack.qbit.added) == 2


def test_e10_episode_selection(stack):
    data = eps(3)
    rid = stack.season([data[0], data[2]], episodes=[1, 3])
    stack.offer("Nimbus", PACK, list(zip(names(3), data, strict=True)))  # size out of tolerance
    for i in (0, 2):
        single = names(3)[i]
        stack.offer("Nimbus", single.removesuffix(".mkv"), [(single, data[i])], name=single)
    stack.run()
    assert stack.statuses(rid) == [
        ("Nimbus", "S01E01", "identical"),
        ("Nimbus", "S01E03", "identical"),
    ]
    assert len(stack.qbit.added) == 2
