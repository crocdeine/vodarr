import os

import pytest

os.environ["VODARR_WORKERS"] = "0"  # tests drive the workers step by step
os.environ["VODARR_SETUP_TOKEN"] = "test-setup-code"  # the code a fresh app expects

from tests.fakes.torrents import random_bytes
from vodarr import core, downloads, sources


@pytest.fixture
def conn(tmp_path):
    conn = core.open_db(tmp_path / "config")
    yield conn
    core.close_thread_db()


@pytest.fixture
def make_season(conn, tmp_path):
    """A downloaded season: files on disk, download stage done. Returns (release_id, paths)."""

    def make(contents, season=1, title="Northwind Precinct", padding=b"", kind="series", year=None):
        src = sources.add(
            conn, "Src", "xtream", {"server": "http://x.invalid", "user": "u", "password": "p"}
        )
        if kind == "series":
            eps = [
                {"id": str(i), "num": i, "title": f"{title} - S{season:02d}E{i:02d}", "ext": "mkv"}
                for i in range(1, len(contents) + 1)
            ]
            rid = downloads.enqueue(
                conn,
                src,
                "series",
                "100",
                title,
                year,
                season,
                eps,
                tmp_path / "data" / "vod",
                now=1,
            )
        else:
            eps = [{"id": "500", "num": None, "title": title, "ext": "mkv"}]
            rid = downloads.enqueue(
                conn, src, "movie", "500", title, year, None, eps, tmp_path / "data" / "vod", now=1
            )
        folder = conn.execute("SELECT dir FROM releases WHERE id = ?", (rid,)).fetchone()[0]
        paths = []
        for row, content in zip(
            conn.execute(
                "SELECT filename FROM files WHERE release_id = ? ORDER BY id", (rid,)
            ).fetchall(),
            contents,
            strict=True,
        ):
            path = tmp_path / folder / row[0]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content + padding)
            paths.append(path)
        conn.execute("UPDATE files SET dl_status = 'done' WHERE release_id = ?", (rid,))
        conn.execute(
            "UPDATE stages SET state = 'done' WHERE release_id = ? AND name = 'download'", (rid,)
        )
        return rid, paths

    return make


@pytest.fixture
def season_data():
    return [random_bytes(40_000, 1), random_bytes(35_000, 2), random_bytes(30_001, 3)]
