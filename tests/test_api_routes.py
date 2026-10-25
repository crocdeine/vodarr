import json

import pytest
from fastapi.testclient import TestClient

from tests.fakes.prowlarr import API_KEY, FakeProwlarr
from tests.fakes.qbittorrent import PASSWORD as QPASS
from tests.fakes.qbittorrent import USER as QUSER
from tests.fakes.qbittorrent import FakeQbit
from tests.fakes.torrents import make_torrent, random_bytes
from tests.fakes.xtream import PASSWORD, USER, FakeXtream
from vodarr import auth, downloads, pipeline, stages
from vodarr.app import create_app
from vodarr.core import open_db

ADMIN = "correct horse battery"
H = {"Origin": "http://testserver"}
NAMES = [f"Northwind.Precinct.S01E0{i}.1080p.WEB-DL-GRP.mkv" for i in (1, 2)]
PACK = "Northwind.Precinct.S01.1080p.WEB-DL-GRP"


@pytest.fixture(autouse=True)
def fresh_throttle(monkeypatch):
    monkeypatch.setattr(auth, "throttle", auth.Throttle())
    monkeypatch.setattr(stages, "ffprobe_json", lambda p: "{}")
    monkeypatch.setattr(stages, "SEED_POLL", 0)


@pytest.fixture
def env(tmp_path):
    dl, links = tmp_path / "data" / "vod", tmp_path / "data" / "links"
    dl.mkdir(parents=True)
    links.mkdir()
    with (
        FakeXtream() as xt,
        FakeProwlarr() as prowl,
        FakeQbit() as qbit,
        TestClient(create_app(tmp_path / "config", hosts={"testserver"})) as c,
    ):
        c.post(
            "/api/auth/setup",
            json={"setupCode": "test-setup-code", "username": "admin", "password": ADMIN},
            headers=H,
        )
        r = c.put(
            "/api/settings",
            headers=H,
            json={
                "paths": {"downloads": str(dl)},
                "crossseed": {"linkDirs": [str(links)]},
                "services": {
                    "prowlarr": {"url": prowl.url, "apiKey": API_KEY},
                    "qbittorrent": {"url": qbit.url, "username": QUSER, "password": QPASS},
                },
            },
        )
        assert r.status_code == 200, r.text
        qbit.next_states = ["uploading"]
        yield c, xt, prowl, qbit, tmp_path


def add_source(c, xt, name="My IPTV"):
    r = c.post(
        "/api/sources",
        headers=H,
        json={"name": name, "type": "xtream", "server": xt.url, "user": USER, "password": PASSWORD},
    )
    assert r.status_code == 201, r.text
    return r.json()


def drain(tmp_path):
    conn = open_db(tmp_path / "config")
    while downloads.process_next(conn, tmp_path / "config", sleep=lambda s: None):
        pass
    while pipeline.process_next(conn, tmp_path / "config", pipeline.handlers()):
        pass


def test_sources_crud_and_masking(env):
    """AC-SET-01, AC-SET-02"""
    c, xt, *_ = env
    a = add_source(c, xt)
    b = add_source(c, xt, "Second")
    assert a["password"] == "••••" and PASSWORD not in json.dumps(c.get("/api/sources").json())
    assert c.patch(f"/api/sources/{a['id']}", json={"position": 5}, headers=H).status_code == 200
    assert c.delete(f"/api/sources/{b['id']}", headers=H).status_code == 200
    assert [s["id"] for s in c.get("/api/sources").json()] == [a["id"]]  # id unchanged


def test_source_secret_does_not_follow_a_new_server(env):
    """AC-SET-03"""
    c, xt, *_ = env
    a = add_source(c, xt)
    r = c.patch(
        f"/api/sources/{a['id']}",
        json={"server": "http://attacker:80", "password": "••••"},
        headers=H,
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "settings.secret_required"
    r = c.patch(f"/api/sources/{a['id']}", json={"server": "http://attacker:80"}, headers=H)
    assert r.status_code == 422  # omitted password: same rule
    assert (
        c.patch(
            f"/api/sources/{a['id']}", json={"name": "Renamed", "password": "••••"}, headers=H
        ).status_code
        == 200
    )


@pytest.mark.parametrize(
    "body",
    [
        {"name": "x", "type": "m3u", "url": "file:///etc/passwd"},
        {"name": "x", "type": "m3u", "path": "/etc/passwd"},
        {"name": "x", "type": "m3u", "path": "../vodarr.db"},
        {"name": "x", "type": "xtream", "server": "ftp://h", "user": "u", "password": "p"},
    ],
)
def test_bad_source_locations_refused(env, body):
    """AC-SET-09"""
    c, *_ = env
    r = c.post("/api/sources", json=body, headers=H)
    assert r.status_code == 422 and r.json()["error"]["code"] == "source.bad_url"


def test_catalogue_pagination_search_and_detail(env):
    c, xt, *_ = env
    sid = add_source(c, xt)["id"]
    cats = c.get(f"/api/sources/{sid}/categories?kind=series").json()
    assert cats[0]["label"] == "NORDIC | Crime"
    page = c.get(f"/api/sources/{sid}/items?kind=series&q=elan&pageSize=1").json()
    assert page["pagination"] == {"page": 1, "pageSize": 1, "totalItems": 1, "totalPages": 1}
    assert page["data"][0]["name"] == "Élan Vital"
    detail = c.get(f"/api/sources/{sid}/series/100").json()
    assert (
        detail["seasons"][0]["episodes"][0]["release"]
        == "Northwind.Precinct.S01E01.1080p.WEB-DL-GRP"
    )
    assert xt.url not in json.dumps(detail)
    assert c.get(f"/api/sources/{sid}/items?pageSize=500").status_code == 422


def test_full_flow_through_the_api(env):
    """AC-DL-06, AC-DL-07, AC-UI-02 data, AC-SEED-05, AC-PIPE-05"""
    c, xt, prowl, qbit, tmp_path = env
    sid = add_source(c, xt)["id"]
    data = [random_bytes(40_000, 1), random_bytes(35_000, 2)]
    xt.files.update({"1101": data[0], "1102": data[1]})
    prowl.add("Nimbus", PACK, sum(map(len, data)), torrent_id="t0")
    prowl.torrents["t0"] = make_torrent(PACK, list(zip(NAMES, data, strict=True)))
    r = c.post(
        "/api/releases",
        headers=H,
        json={
            "sourceId": sid,
            "kind": "series",
            "itemId": "100",
            "season": 1,
            "episodes": ["1101", "1102"],
        },
    )
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert r.json()["status"] == "downloading"
    assert [i["status"] for i in c.get("/api/downloads").json()["data"]] == ["queued", "queued"]
    drain(tmp_path)
    detail = c.get(f"/api/releases/{rid}").json()
    assert detail["status"] == "done"
    assert [s["state"] for s in detail["stages"]] == ["done"] * 6
    assert detail["stages"][2]["message"]["code"] == "search.done"
    [cand] = [x for x in detail["candidates"] if x["isCandidate"]]
    assert (
        cand["status"] == "identical"
        and cand["linked"]
        and cand["qbittorrent"]["state"] == "uploading"
    )
    text = json.dumps(detail)
    assert API_KEY not in text and PASSWORD not in text and "downloadUrl" not in text
    assert [f["status"] for f in cand["files"]] == ["identical", "identical"]
    assert c.get("/api/releases?status=done").json()["pagination"]["totalItems"] == 1
    assert c.get("/api/releases?status=error").json()["data"] == []
    assert c.delete(f"/api/sources/{sid}", headers=H).json()["error"]["code"] == "source.in_use"
    assert c.delete(f"/api/releases/{rid}", headers=H).status_code == 200
    assert qbit.deleted and c.get(f"/api/releases/{rid}").status_code == 404


def test_release_creation_is_idempotent(env):
    c, xt, *_ = env
    sid = add_source(c, xt)["id"]
    body = {"sourceId": sid, "kind": "series", "itemId": "100", "season": 1, "episodes": ["1101"]}
    a = c.post("/api/releases", headers=H, json=body).json()
    b = c.post("/api/releases", headers=H, json=body).json()
    assert a["id"] == b["id"] and b["files"] == 1


def test_unknown_episode_ids_are_ignored(env):
    """Episodes come from the catalogue, never from the request."""
    c, xt, *_ = env
    sid = add_source(c, xt)["id"]
    r = c.post(
        "/api/releases",
        headers=H,
        json={
            "sourceId": sid,
            "kind": "series",
            "itemId": "100",
            "season": 1,
            "episodes": ["../../etc"],
        },
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "queue.bad_selection"


def test_movie_release(env):
    c, xt, *_ = env
    sid = add_source(c, xt)["id"]
    r = c.post("/api/releases", headers=H, json={"sourceId": sid, "kind": "movie", "itemId": "500"})
    assert r.status_code == 201 and r.json()["season"] is None


def test_downloads_cancel_remove_clear(env):
    """AC-DL-05"""
    c, xt, _, _, tmp_path = env
    sid = add_source(c, xt)["id"]
    xt.files["1101"] = b"x" * 100
    c.post(
        "/api/releases",
        headers=H,
        json={
            "sourceId": sid,
            "kind": "series",
            "itemId": "100",
            "season": 1,
            "episodes": ["1101", "1102"],
        },
    )
    items = c.get("/api/downloads").json()["data"]
    assert c.post(f"/api/downloads/{items[1]['id']}/cancel", headers=H, json={}).status_code == 200
    assert c.delete(f"/api/downloads/{items[1]['id']}", headers=H).status_code == 200
    conn = open_db(tmp_path / "config")
    downloads.process_next(conn, tmp_path / "config", sleep=lambda s: None)
    assert c.post("/api/downloads/clear-finished", headers=H, json={}).status_code == 200
    assert c.get("/api/downloads").json()["data"] == []


def test_retry_and_complete_validation(env):
    c, xt, *_ = env
    sid = add_source(c, xt)["id"]
    rid = c.post(
        "/api/releases", headers=H, json={"sourceId": sid, "kind": "movie", "itemId": "500"}
    ).json()["id"]
    assert (
        c.post(f"/api/releases/{rid}/retry", json={"stage": "nope"}, headers=H).status_code == 422
    )
    r = c.post(f"/api/releases/{rid}/complete", json={"candidateId": 999}, headers=H)
    assert r.status_code == 404 and r.json()["error"]["code"] == "candidate.not_found"
    assert (
        c.post("/api/releases/missing/retry", json={"stage": "search"}, headers=H).status_code
        == 404
    )


def test_api_key_works_for_routes(env):
    c, xt, *_ = env
    key = c.post("/api/auth/api-key/reveal", json={"password": ADMIN}, headers=H).json()["apiKey"]
    c.cookies.clear()
    assert c.get("/api/sources", headers={"X-Api-Key": key}).status_code == 200
    assert c.get("/api/sources").status_code == 401


def test_release_detail_carries_technical_metadata(env):
    c, xt, _, _, tmp_path = env
    sid = add_source(c, xt)["id"]
    rid = c.post(
        "/api/releases", headers=H, json={"sourceId": sid, "kind": "movie", "itemId": "500"}
    ).json()["id"]
    conn = open_db(tmp_path / "config")
    conn.execute(
        "UPDATE files SET media_info = ?",
        (json.dumps({"video": {"codec": "hevc", "height": 1080}, "audio": [], "subtitles": []}),),
    )
    f = c.get(f"/api/releases/{rid}").json()["fileList"][0]
    assert f["media"]["video"] == {"codec": "hevc", "height": 1080}


def test_m3u_source_switches_from_file_to_url(env):
    """Bug hunt: the form resends the old path with the new URL; the URL must win."""
    c, *_ = env
    r = c.post("/api/sources", json={"name": "M", "type": "m3u", "path": "list.m3u"}, headers=H)
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    r = c.patch(
        f"/api/sources/{sid}",
        json={"path": "list.m3u", "url": "http://iptv.invalid/get.php"},
        headers=H,
    )
    assert r.status_code == 200, r.text
    conn = open_db(env[4] / "config")
    cfg = json.loads(conn.execute("SELECT config FROM sources WHERE id = ?", (sid,)).fetchone()[0])
    assert cfg == {"url": "http://iptv.invalid/get.php"}
