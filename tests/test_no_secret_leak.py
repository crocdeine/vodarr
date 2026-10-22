"""AC-CORE-04, AC-SRCH-04, AC-UI-09 (API part): a full run with known secrets, then a search for
them in the production log output and in every API response."""

import io
import json
import logging

import pytest
from fastapi.testclient import TestClient

from tests.fakes.http import FakeServer, Response
from tests.fakes.prowlarr import API_KEY, FakeProwlarr
from tests.fakes.qbittorrent import PASSWORD as QPASS
from tests.fakes.qbittorrent import USER as QUSER
from tests.fakes.qbittorrent import FakeQbit
from tests.fakes.torrents import make_torrent, random_bytes
from tests.fakes.xtream import PASSWORD, USER, FakeXtream
from vodarr import auth, core, downloads, pipeline, stages
from vodarr.app import create_app

ADMIN = "correct horse battery"
H = {"Origin": "http://testserver"}
PACK = "Northwind.Precinct.S01.1080p.WEB-DL-GRP"
M3U_SECRET = "m3u-token-SECRET-77"


@pytest.fixture
def log_output():
    """The production handler (setup_logging) at DEBUG, writing to a buffer."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.addFilter(core.SecretFilter())
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    yield buf
    root.removeHandler(handler)
    root.setLevel(old_level)


def test_no_secret_in_logs_or_responses(tmp_path, monkeypatch, log_output):
    monkeypatch.setattr(auth, "throttle", auth.Throttle())
    monkeypatch.setattr(stages, "probe_release", lambda p: None)
    monkeypatch.setattr(stages, "SEED_POLL", 0)
    dl, links = tmp_path / "data" / "vod", tmp_path / "data" / "links"
    dl.mkdir(parents=True)
    links.mkdir()
    bodies: list[str] = []

    with (
        FakeXtream() as xt,
        FakeProwlarr() as prowl,
        FakeQbit() as qbit,
        FakeServer() as m3u,
        TestClient(
            create_app(tmp_path / "config", hosts={"testserver"}), raise_server_exceptions=False
        ) as c,
    ):

        def call(method, url, **kw):
            r = c.request(method, url, **kw)
            bodies.append(r.text + json.dumps(dict(r.headers)))
            return r

        m3u.routes[("GET", "/get.php")] = lambda req: Response(
            200,
            (
                '#EXTM3U\n#EXTINF:-1 group-title="Films",Harbour Lights (2020)\n'
                f"{xt.url}/movie/{USER}/{PASSWORD}/500.mp4\n"
            ).encode(),
        )
        call(
            "POST",
            "/api/auth/setup",
            json={"setupCode": "test-setup-code", "username": "admin", "password": ADMIN},
            headers=H,
        )
        call(
            "PUT",
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
        key = call("POST", "/api/auth/api-key/reveal", json={"password": ADMIN}, headers=H).json()[
            "apiKey"
        ]
        sid = call(
            "POST",
            "/api/sources",
            headers=H,
            json={
                "name": "X",
                "type": "xtream",
                "server": xt.url,
                "user": USER,
                "password": PASSWORD,
            },
        ).json()["id"]
        mid = call(
            "POST",
            "/api/sources",
            headers=H,
            json={"name": "M", "type": "m3u", "url": f"{m3u.url}/get.php?token={M3U_SECRET}"},
        ).json()["id"]

        data = [random_bytes(40_000, 1), random_bytes(35_000, 2)]
        xt.files.update({"1101": data[0], "1102": data[1]})
        names = [f"Northwind.Precinct.S01E0{i}.1080p.WEB-DL-GRP.mkv" for i in (1, 2)]
        prowl.add("Nimbus", PACK, sum(map(len, data)), torrent_id="t0")
        prowl.torrents["t0"] = make_torrent(PACK, list(zip(names, data, strict=True)))
        qbit.next_states = ["checkingUP", "uploading"]

        for path in (
            f"/api/sources/{sid}/categories",
            f"/api/sources/{sid}/items",
            f"/api/sources/{sid}/series/100",
            f"/api/sources/{sid}/movies/500",
            f"/api/sources/{mid}/items?kind=movie",
            "/api/sources",
            "/api/settings",
        ):
            call("GET", path)
        call("POST", f"/api/sources/{sid}/test", headers=H, json={})
        call("POST", "/api/settings/test", headers=H, json={"service": "prowlarr"})
        call("POST", "/api/settings/test", headers=H, json={"service": "qbittorrent"})
        rid = call(
            "POST",
            "/api/releases",
            headers=H,
            json={"sourceId": sid, "kind": "series", "itemId": "100", "season": 1},
        ).json()["id"]
        xt.files["1103"] = b"short"  # third episode served too
        conn = core.open_db(tmp_path / "config")
        while downloads.process_next(conn, tmp_path / "config", sleep=lambda s: None):
            pass
        while pipeline.process_next(conn, tmp_path / "config", pipeline.handlers()):
            pass
        call("GET", "/api/downloads")
        call("GET", "/api/releases")
        call("GET", f"/api/releases/{rid}")

        # error paths: refused credentials, unreachable services, a failing download
        xt.mode = "auth_fail"
        call("POST", f"/api/sources/{sid}/refresh", headers=H, json={})
        call("GET", f"/api/sources/{sid}/items")
        prowl.mode = "bad_key"
        call("POST", f"/api/releases/{rid}/retry", headers=H, json={"stage": "search"})
        while pipeline.process_next(conn, tmp_path / "config", pipeline.handlers()):
            pass
        call("GET", f"/api/releases/{rid}")
        del xt.files["1102"]
        call(
            "POST",
            "/api/releases",
            headers=H,
            json={"sourceId": sid, "kind": "movie", "itemId": "500"},
        )
        downloads.process_next(conn, tmp_path / "config", sleep=lambda s: None)
        call("GET", "/api/downloads")
        core.close_thread_db()

    secrets = {
        "Xtream password": PASSWORD,
        "M3U URL secret": M3U_SECRET,
        "Prowlarr key": API_KEY,
        "qBittorrent password": QPASS,
        "API key": key,
        "admin password": ADMIN,
    }
    logs = log_output.getvalue()
    assert "download" in logs  # the error paths did log something
    for label, secret in secrets.items():
        assert secret not in logs, f"{label} leaked in logs"
    responses = "\n".join(bodies)
    for label, secret in secrets.items():
        if label == "API key":
            continue  # revealed on purpose, once, after the admin password
        assert secret not in responses, f"{label} leaked in an API response"
    assert "downloadUrl" not in responses and "apikey=" not in responses.lower()
    assert f"/{USER}/{PASSWORD}/" not in responses
