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


FFPROBE_REAL = {
    "format": {
        "format_name": "matroska,webm",
        "duration": "2712.45",
        "bit_rate": "6012345",
        "tags": {"title": "Northwind.Precinct.S01E01.1080p.WEB-DL-GRP"},
    },
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "hevc",
            "profile": "Main 10",
            "width": 1920,
            "height": 1080,
            "r_frame_rate": "24000/1001",
            "color_transfer": "smpte2084",
            "tags": {"language": "und"},
        },
        {"codec_type": "audio", "codec_name": "eac3", "channels": 6, "tags": {"language": "fre"}},
        {"codec_type": "audio", "codec_name": "aac", "channels": 2, "tags": {"language": "eng"}},
        {"codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "fre"}},
        {
            "codec_type": "subtitle",
            "codec_name": "subrip",
            "tags": {"language": "fre"},
            "disposition": {"forced": 1},
        },
    ],
}


def test_technical_metadata_stored(conn, tmp_path, make_season, season_data, monkeypatch):
    """Release page facts: resolution, codecs, languages, duration (0.1.1)."""
    rid, _ = make_season(season_data[:1])
    monkeypatch.setattr(stages, "ffprobe_json", lambda p: json.dumps(FFPROBE_REAL))
    release = conn.execute("SELECT * FROM releases WHERE id = ?", (rid,)).fetchone()
    stages.stage_analyse(pipeline.Ctx(conn, tmp_path / "config", release, lambda d, t: None))
    info = json.loads(conn.execute("SELECT media_info FROM files").fetchone()[0])
    assert info["video"] == {
        "codec": "hevc",
        "profile": "Main 10",
        "width": 1920,
        "height": 1080,
        "fps": 23.976,
        "hdr": True,
    }
    assert info["audio"] == [
        {"codec": "eac3", "channels": 6, "language": "fre"},
        {"codec": "aac", "channels": 2, "language": "eng"},
    ]
    assert info["subtitles"] == [
        {"language": "fre", "forced": False},
        {"language": "fre", "forced": True},  # forced: "français (forcés)", not "français" twice
    ]
    assert info["duration"] == 2712.45


def test_garbage_ffprobe_output_is_tolerated(conn, tmp_path, make_season, season_data, monkeypatch):
    rid, _ = make_season(season_data[:1])
    monkeypatch.setattr(stages, "ffprobe_json", lambda p: '["not", "an", "object"]')
    release = conn.execute("SELECT * FROM releases WHERE id = ?", (rid,)).fetchone()
    msg = stages.stage_analyse(pipeline.Ctx(conn, tmp_path / "config", release, lambda d, t: None))
    assert msg.code == "analyse.done"
    assert conn.execute("SELECT media_info FROM files").fetchone()[0] is None
