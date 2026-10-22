import json

from vodarr import pipeline, stages


def fake_ffprobe(titles):
    """titles: {filename: (format_title, [stream titles])}"""

    def run(path):
        fmt, streams = titles.get(path.name, (None, []))
        return json.dumps(
            {
                "format": {"tags": {"title": fmt} if fmt else {}},
                "streams": [{"tags": {"title": t}} for t in streams],
            }
        )

    return run


def analyse(conn, tmp_path, rid, monkeypatch, titles):
    monkeypatch.setattr(stages, "ffprobe_json", fake_ffprobe(titles))
    release = conn.execute("SELECT * FROM releases WHERE id = ?", (rid,)).fetchone()
    return stages.stage_analyse(pipeline.Ctx(conn, tmp_path / "config", release, lambda d, t: None))


def test_size_and_release_from_tags(conn, tmp_path, make_season, season_data, monkeypatch):
    """AC-ANA-01"""
    rid, paths = make_season(season_data, padding=b"\0" * 6)
    titles = {
        paths[0].name: ("Northwind.Precinct.S01E01.1080p.WEB-DL-GRP", []),
        paths[1].name: (None, ["AC3-5.1-VFF", "Northwind.Precinct.S01E02.1080p.WEB-DL-GRP"]),
    }
    msg = analyse(conn, tmp_path, rid, monkeypatch, titles)
    rows = conn.execute("SELECT size, release_name FROM files ORDER BY id").fetchall()
    assert [r["size"] for r in rows] == [len(c) + 6 for c in season_data]
    assert [r["release_name"] for r in rows] == [
        "Northwind.Precinct.S01E01.1080p.WEB-DL-GRP",
        "Northwind.Precinct.S01E02.1080p.WEB-DL-GRP",
        None,
    ]
    assert msg.code == "analyse.done"
    assert msg.params == {"files": 3, "release": "Northwind.Precinct.S01.1080p.WEB-DL-GRP"}


def test_unknown_release_continues(conn, tmp_path, make_season, season_data, monkeypatch):
    """AC-ANA-01"""
    rid, _ = make_season(season_data)
    msg = analyse(conn, tmp_path, rid, monkeypatch, {})
    assert msg.params == {"files": 3, "release": None}


def test_missing_ffprobe_is_tolerated(conn, tmp_path, make_season, season_data, monkeypatch):
    rid, _ = make_season(season_data)
    monkeypatch.setattr(stages, "FFPROBE", "/nonexistent/ffprobe")
    release = conn.execute("SELECT * FROM releases WHERE id = ?", (rid,)).fetchone()
    msg = stages.stage_analyse(pipeline.Ctx(conn, tmp_path / "config", release, lambda d, t: None))
    assert msg.params["release"] is None


def test_missing_downloaded_file_is_an_error(conn, tmp_path, make_season, season_data, monkeypatch):
    import pytest

    from vodarr.core import MsgError

    rid, paths = make_season(season_data)
    paths[1].unlink()
    with pytest.raises(MsgError) as err:
        analyse(conn, tmp_path, rid, monkeypatch, {})
    assert err.value.msg.code == "analyse.file_missing"
