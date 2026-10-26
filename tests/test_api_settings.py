import os

import pytest
from fastapi.testclient import TestClient

from tests.fakes.prowlarr import API_KEY, FakeProwlarr
from tests.fakes.qbittorrent import PASSWORD as QPASS
from tests.fakes.qbittorrent import USER as QUSER
from tests.fakes.qbittorrent import FakeQbit
from vodarr import auth
from vodarr.app import create_app

PASSWORD = "correct horse battery"
H = {"Origin": "http://testserver"}


@pytest.fixture(autouse=True)
def fresh_throttle(monkeypatch):
    monkeypatch.setattr(auth, "throttle", auth.Throttle())


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "config", hosts={"testserver"})) as c:
        c.post(
            "/api/auth/setup",
            json={"setupCode": "test-setup-code", "username": "admin", "password": PASSWORD},
            headers=H,
        )
        yield c


@pytest.fixture
def dirs(tmp_path):
    dl, links = tmp_path / "data" / "vod", tmp_path / "data" / "links"
    dl.mkdir(parents=True)
    links.mkdir()
    return dl, links


def put(client, body):
    return client.put("/api/settings", json=body, headers=H)


def test_defaults_match_cross_seed(client):
    """AC-SET-05"""
    s = client.get("/api/settings").json()
    assert s["crossseed"] == {
        "linkDirs": [],
        "linkType": "hardlink",
        "flatLinking": False,
        "linkCategory": "cross-seed-link",
    }
    assert s["ui"] == {"language": "auto", "theme": "auto"}


def test_secrets_masked_and_kept_when_server_unchanged(client):
    """AC-SET-02, AC-SET-03"""
    r = put(
        client,
        {
            "services": {
                "prowlarr": {"url": "http://prowlarr:9696", "apiKey": "real-key-123"},
                "qbittorrent": {
                    "url": "http://qbit:8080",
                    "username": "u",
                    "password": "qpass-123",
                },
            }
        },
    )
    assert r.status_code == 200, r.text
    body = r.text + client.get("/api/settings").text
    assert "real-key-123" not in body and "qpass-123" not in body
    assert r.json()["services"]["prowlarr"]["apiKey"] == "••••"
    r = put(client, {"services": {"prowlarr": {"url": "http://prowlarr:9696", "apiKey": "••••"}}})
    assert r.status_code == 200
    from vodarr import settings as st
    from vodarr.core import open_db

    conn = open_db(client.app.state.config_dir)
    assert st.load(conn)["services"]["prowlarr"]["apiKey"] == "real-key-123"


def test_masked_secret_refused_when_server_changes(client):
    """AC-SET-03: a secret never follows a changed URL."""
    put(
        client,
        {"services": {"prowlarr": {"url": "http://prowlarr:9696", "apiKey": "real-key-123"}}},
    )
    r = put(client, {"services": {"prowlarr": {"url": "http://attacker:9696", "apiKey": "••••"}}})
    assert r.status_code == 422
    omitted = put(client, {"services": {"prowlarr": {"url": "http://attacker:9696"}}})
    assert omitted.status_code == 422  # bug hunt: an omitted key followed the new URL
    assert r.json()["error"] == {
        "code": "settings.secret_required",
        "params": {"service": "prowlarr"},
    }


def test_paths_must_exist_be_writable_and_share_filesystem(client, dirs, tmp_path, monkeypatch):
    """AC-SET-04"""
    dl, links = dirs
    r = put(client, {"paths": {"downloads": "/vodarr-no-such-top-folder/vod"}})
    assert r.json()["error"]["code"] == "settings.path_missing"
    os.chmod(links, 0o500)
    try:
        r = put(client, {"paths": {"downloads": str(dl)}, "crossseed": {"linkDirs": [str(links)]}})
        assert r.json()["error"]["code"] == "settings.path_not_writable"
    finally:
        os.chmod(links, 0o700)
    from vodarr import settings as st

    real_check = st._check_dir

    class FakeStat:
        def __init__(self, dev):
            self.st_dev = dev

    def other_fs(path):
        p = real_check(path)
        if p == links:
            return type(
                "P", (), {"stat": lambda self: FakeStat(-1), "__eq__": lambda s, o: False}
            )()
        return p

    monkeypatch.setattr(st, "_check_dir", other_fs)
    r = put(client, {"paths": {"downloads": str(dl)}, "crossseed": {"linkDirs": [str(links)]}})
    assert r.json()["error"]["code"] == "settings.not_same_filesystem"
    monkeypatch.setattr(st, "_check_dir", real_check)
    r = put(
        client,
        {
            "paths": {"downloads": str(dl)},
            "crossseed": {"linkDirs": [str(links)], "linkType": "hardlink"},
        },
    )
    assert r.status_code == 200, r.text


def test_cross_seed_values_validated(client):
    """AC-SET-05"""
    assert put(client, {"crossseed": {"linkType": "copy"}}).status_code == 422
    assert (
        put(client, {"crossseed": {"flatLinking": True, "linkCategory": "xs"}}).json()["crossseed"][
            "flatLinking"
        ]
        is True
    )
    assert put(client, {"crossseed": {"unknown": 1}}).status_code == 422


def test_language_kept(client):
    """AC-SET-10"""
    assert put(client, {"ui": {"language": "fr"}}).status_code == 200
    assert client.get("/api/settings").json()["ui"]["language"] == "fr"
    assert put(client, {"ui": {"language": "de"}}).status_code == 422


def test_service_tests_hide_urls(client):
    """AC-SET-07"""
    with FakeProwlarr() as prowl, FakeQbit() as qbit:
        put(
            client,
            {
                "services": {
                    "prowlarr": {"url": prowl.url, "apiKey": API_KEY},
                    "qbittorrent": {"url": qbit.url, "username": QUSER, "password": QPASS},
                }
            },
        )
        assert client.post(
            "/api/settings/test", json={"service": "prowlarr"}, headers=H
        ).json() == {"ok": True, "error": None}
        assert (
            client.post("/api/settings/test", json={"service": "qbittorrent"}, headers=H).json()[
                "ok"
            ]
            is True
        )
        put(
            client,
            {
                "services": {
                    "qbittorrent": {"url": qbit.url, "username": QUSER, "password": "wrong-pass"}
                }
            },
        )
        r = client.post("/api/settings/test", json={"service": "qbittorrent"}, headers=H).json()
        assert r == {"ok": False, "error": {"code": "seed.auth_failed", "params": {}}}
        assert qbit.url not in str(r)


def test_service_url_scheme(client):
    assert (
        put(client, {"services": {"prowlarr": {"url": "file:///etc", "apiKey": "k"}}}).status_code
        == 422
    )


def test_cross_seed_import_saves_nothing(client, tmp_path):
    """AC-SET-06"""
    cfg = tmp_path / "config.js"
    cfg.write_text(
        'module.exports = { linkDirs: ["/data/links"], linkType: "symlink", flatLinking: true };'
    )
    r = client.post("/api/settings/cross-seed-import", json={"path": str(cfg)}, headers=H)
    assert r.json() == {
        "values": {"linkDirs": ["/data/links"], "linkType": "symlink", "flatLinking": True},
        "manual": [],
    }
    assert client.get("/api/settings").json()["crossseed"]["linkType"] == "hardlink"


def test_partial_update_keeps_other_fields(client):
    """PUT with only the URL must not wipe the stored key."""
    put(
        client,
        {"services": {"prowlarr": {"url": "http://prowlarr:9696", "apiKey": "real-key-123"}}},
    )
    r = put(client, {"services": {"prowlarr": {"url": "http://prowlarr:9696"}}})
    assert r.status_code == 200
    from vodarr import settings as st
    from vodarr.core import open_db

    assert (
        st.load(open_db(client.app.state.config_dir))["services"]["prowlarr"]["apiKey"]
        == "real-key-123"
    )


def test_service_test_uses_unsaved_form_values(client):
    """Real run: Test checked the saved settings, so testing before saving always failed."""
    with FakeProwlarr() as prowl, FakeQbit(v5_login=True) as qbit:
        r = client.post(
            "/api/settings/test",
            json={"service": "prowlarr", "url": prowl.url, "apiKey": API_KEY},
            headers=H,
        )
        assert r.json() == {"ok": True, "error": None}
        r = client.post(
            "/api/settings/test",
            json={"service": "qbittorrent", "url": qbit.url, "username": QUSER, "password": QPASS},
            headers=H,
        )
        assert r.json()["ok"] is True
        assert (
            client.get("/api/settings").json()["services"]["prowlarr"]["url"] == ""
        )  # nothing saved
        put(client, {"services": {"prowlarr": {"url": prowl.url, "apiKey": API_KEY}}})
        r = client.post(
            "/api/settings/test",
            json={"service": "prowlarr", "url": prowl.url, "apiKey": "••••"},
            headers=H,
        )
        assert r.json()["ok"] is True  # masked key, same address: the stored key is used
        r = client.post(
            "/api/settings/test",
            json={"service": "prowlarr", "url": "http://elsewhere:9696", "apiKey": "••••"},
            headers=H,
        )
        assert r.json()["error"]["code"] == "settings.secret_required"


def test_missing_folders_are_created_under_an_existing_top_folder(client, tmp_path):
    """Real run: folders typed in Settings did not exist yet; they are created on save."""
    dl = tmp_path / "data" / "vodarr" / "vod"
    links = tmp_path / "data" / "vodarr" / "links"
    r = put(client, {"paths": {"downloads": str(dl)}, "crossseed": {"linkDirs": [str(links)]}})
    assert r.status_code == 200, r.text
    assert dl.is_dir() and links.is_dir()


@pytest.mark.parametrize("path", ["/vodarr-unmounted/vod", "relative/vod", "/data/../etc/x"])
def test_folders_are_not_created_outside_an_existing_top_folder(client, path):
    r = put(client, {"paths": {"downloads": path}})
    assert r.json()["error"]["code"] == "settings.path_missing"


@pytest.mark.parametrize(
    "body",
    [
        {"paths": None},
        {"services": None},
        {"services": {"prowlarr": None}},
        {"crossseed": {"linkDirs": None, "flatLinking": None}},
    ],
)
def test_null_means_keep_never_a_500(client, body):
    """Bug hunt: nulls crashed the save (500) or were stored as None."""
    before = client.get("/api/settings").json()
    r = put(client, body)
    assert r.status_code == 200, r.text
    assert r.json() == before


def test_nul_byte_in_a_path_is_refused(client):
    r = put(client, {"paths": {"downloads": "/data/a\x00b"}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "settings.path_missing"


def test_tracker_check_limit_is_validated(client):
    r = put(client, {"services": {"prowlarr": {"maxChecksPerTracker": 3}}})
    assert r.status_code == 200
    assert r.json()["services"]["prowlarr"]["maxChecksPerTracker"] == 3
    assert put(client, {"services": {"prowlarr": {"maxChecksPerTracker": -1}}}).status_code == 422
