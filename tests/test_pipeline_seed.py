import pytest

from tests.fakes.prowlarr import API_KEY, FakeProwlarr
from tests.fakes.qbittorrent import PASSWORD, USER, FakeQbit
from tests.fakes.torrents import make_torrent
from vodarr import core, pipeline, stages
from vodarr.core import MsgError

NAMES = [f"Northwind.Precinct.S01E0{i}.1080p.WEB-DL-GRP.mkv" for i in (1, 2, 3)]
PACK = "Northwind.Precinct.S01.1080p.WEB-DL-GRP"


@pytest.fixture
def stack(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(stages, "ffprobe_json", lambda p: "{}")
    monkeypatch.setattr(stages, "SEED_POLL", 0)
    link_dir = tmp_path / "data" / "links"
    link_dir.mkdir(parents=True)
    core.set_setting(conn, "crossseed", {"linkDirs": [str(link_dir)]})
    core.set_setting(
        conn, "paths", {"dataRoot": str(tmp_path / "data"), "qbitDataRoot": "/mnt/media"}
    )
    with FakeProwlarr() as prowl, FakeQbit() as qbit:
        core.set_setting(
            conn,
            "services",
            {
                "prowlarr": {"url": prowl.url, "apiKey": API_KEY},
                "qbittorrent": {"url": qbit.url, "username": USER, "password": PASSWORD},
            },
        )
        qbit.next_states = ["checkingUP", "checkingUP", "uploading"]
        yield prowl, qbit, link_dir


def restored(conn, tmp_path, make_season, season_data, prowl, trackers=("Nimbus",)):
    rid, paths = make_season(season_data)
    data = make_torrent(PACK, list(zip(NAMES, season_data, strict=True)))
    for i, tracker in enumerate(trackers):
        prowl.add(tracker, PACK, sum(map(len, season_data)), torrent_id=f"t{i}")
        prowl.torrents[f"t{i}"] = data
    release = conn.execute("SELECT * FROM releases WHERE id = ?", (rid,)).fetchone()
    ctx = pipeline.Ctx(conn, tmp_path / "config", release, lambda d, t: None)
    for stage in (
        stages.stage_analyse,
        stages.stage_search,
        stages.stage_verify,
        stages.stage_restore,
    ):
        stage(ctx)
    return ctx, data


def test_add_with_translated_savepath_and_wait_for_check(
    conn, tmp_path, make_season, season_data, stack
):
    """AC-SEED-01, AC-SEED-02, AC-SEED-04"""
    prowl, qbit, _ = stack
    ctx, data = restored(conn, tmp_path, make_season, season_data, prowl)
    msg = stages.stage_seed(ctx)
    [added] = qbit.added
    assert added["savepath"] == "/mnt/media/links/Nimbus"
    assert (added["category"], added["autoTMM"], added["skip_checking"]) == (
        "cross-seed-link",
        "false",
        "false",
    )
    assert "cross-seed-link" in qbit.categories
    assert msg.code == "seed.done" and msg.params == {"trackers": "Nimbus"}
    info_calls = [r for r in qbit.requests if r.path == "/api/v2/torrents/info"]
    assert len(info_calls) >= 3  # waited through the checking states


def test_json_add_answer(conn, tmp_path, make_season, season_data, stack):
    """qBittorrent 5.1+ answers torrents/add with JSON counts."""
    prowl, qbit, _ = stack
    qbit.json_add = True
    ctx, _ = restored(conn, tmp_path, make_season, season_data, prowl)
    assert stages.stage_seed(ctx).code == "seed.done"


def test_existing_infohash_not_added_again(conn, tmp_path, make_season, season_data, stack):
    """AC-SEED-03"""
    prowl, qbit, _ = stack
    ctx, data = restored(conn, tmp_path, make_season, season_data, prowl)
    qbit.seed_existing(data)
    stages.stage_seed(ctx)
    assert qbit.added == []


def test_relogin_when_session_expires(conn, tmp_path, make_season, season_data, stack):
    """AC-SEED-01"""
    prowl, qbit, _ = stack
    ctx, _ = restored(conn, tmp_path, make_season, season_data, prowl)
    real_info = qbit.routes[("GET", "/api/v2/torrents/info")]
    calls = []

    def expiring(req):
        calls.append(1)
        if len(calls) == 2:
            qbit.expire_sessions()
        return real_info(req)

    qbit.routes[("GET", "/api/v2/torrents/info")] = expiring
    stages.stage_seed(ctx)
    logins = [r for r in qbit.requests if r.path == "/api/v2/auth/login"]
    assert len(logins) == 2


@pytest.mark.parametrize("state", ["error", "missingFiles"])
def test_failed_state_is_an_error(conn, tmp_path, make_season, season_data, stack, state):
    """AC-SEED-04"""
    prowl, qbit, _ = stack
    qbit.next_states = ["checkingUP", state]
    ctx, _ = restored(conn, tmp_path, make_season, season_data, prowl)
    with pytest.raises(MsgError) as err:
        stages.stage_seed(ctx)
    assert err.value.msg.code == "seed.torrent_failed"
    assert err.value.msg.params == {"state": state, "tracker": "Nimbus"}


def test_bad_credentials(conn, tmp_path, make_season, season_data, stack):
    prowl, qbit, _ = stack
    services = core.get_setting(conn, "services")
    services["qbittorrent"]["password"] = "wrong"
    core.set_setting(conn, "services", services)
    ctx, _ = restored(conn, tmp_path, make_season, season_data, prowl)
    with pytest.raises(MsgError) as err:
        stages.stage_seed(ctx)
    assert err.value.msg.code == "seed.auth_failed"


def test_no_qbittorrent_skips(conn, tmp_path, make_season, season_data, stack):
    """AC-PIPE-02"""
    prowl, qbit, _ = stack
    ctx, _ = restored(conn, tmp_path, make_season, season_data, prowl)
    core.set_setting(conn, "services", {"prowlarr": core.get_setting(conn, "services")["prowlarr"]})
    with pytest.raises(pipeline.Skip) as skip:
        stages.stage_seed(ctx)
    assert skip.value.msg.code == "seed.no_qbittorrent"


def test_unknown_category_creation_is_idempotent(conn, tmp_path, make_season, season_data, stack):
    prowl, qbit, _ = stack
    qbit.categories.add("cross-seed-link")
    ctx, _ = restored(conn, tmp_path, make_season, season_data, prowl)
    assert stages.stage_seed(ctx).code == "seed.done"


def test_timeout(conn, tmp_path, make_season, season_data, stack, monkeypatch):
    prowl, qbit, _ = stack
    qbit.next_states = ["checkingUP"] * 50
    monkeypatch.setattr(stages, "SEED_TIMEOUT", -1)
    ctx, _ = restored(conn, tmp_path, make_season, season_data, prowl)
    with pytest.raises(MsgError) as err:
        stages.stage_seed(ctx)
    assert err.value.msg.code == "seed.timeout"


def test_same_infohash_on_two_trackers_added_once(conn, tmp_path, make_season, season_data, stack):
    """one torrent cross-posted on two trackers."""
    prowl, qbit, _ = stack
    ctx, _ = restored(
        conn, tmp_path, make_season, season_data, prowl, trackers=("Nimbus", "Aurora")
    )
    msg = stages.stage_seed(ctx)
    assert len(qbit.added) == 1
    assert msg.params == {"trackers": "Aurora, Nimbus"}
    assert (
        conn.execute("SELECT count(*) FROM candidates WHERE added_to_qbit = 1").fetchone()[0] == 2
    )


def test_torrent_removed_from_qbit_mid_wait(conn, tmp_path, make_season, season_data, stack):
    prowl, qbit, _ = stack
    qbit.next_states = ["checkingUP"] * 20
    ctx, _ = restored(conn, tmp_path, make_season, season_data, prowl)
    real = qbit.routes[("GET", "/api/v2/torrents/info")]
    calls = []

    def vanish(req):
        calls.append(1)
        if len(calls) == 3:
            qbit.torrents.clear()
        return real(req)

    qbit.routes[("GET", "/api/v2/torrents/info")] = vanish
    with pytest.raises(MsgError) as err:
        stages.stage_seed(ctx)
    assert err.value.msg.code == "seed.torrent_missing"


def test_paused_incomplete_torrent_is_an_error(conn, tmp_path, make_season, season_data, stack):
    prowl, qbit, _ = stack
    qbit.next_states = ["checkingUP", "stoppedDL"]
    ctx, _ = restored(conn, tmp_path, make_season, season_data, prowl)
    with pytest.raises(MsgError) as err:
        stages.stage_seed(ctx)
    assert err.value.msg.code == "seed.torrent_stopped"


@pytest.mark.parametrize("mode", ["v5_login", "bypass"])
def test_qbittorrent_5_2_login_and_whitelisted_subnet(
    conn, tmp_path, make_season, season_data, stack, mode
):
    """Real run, qBittorrent 5.2.0: login answers 204 with a QBT_SID_<port> cookie, or no
    session at all when the client's subnet bypasses authentication."""
    prowl, qbit, _ = stack
    setattr(qbit, mode, True)
    ctx, _ = restored(conn, tmp_path, make_season, season_data, prowl)
    assert stages.stage_seed(ctx).code == "seed.done"
    assert len(qbit.added) == 1


def test_qbittorrent_5_2_wrong_password(conn, tmp_path, make_season, season_data, stack):
    prowl, qbit, _ = stack
    qbit.v5_login = True
    services = core.get_setting(conn, "services")
    services["qbittorrent"]["password"] = "wrong"
    core.set_setting(conn, "services", services)
    ctx, _ = restored(conn, tmp_path, make_season, season_data, prowl)
    with pytest.raises(MsgError) as err:
        stages.stage_seed(ctx)
    assert err.value.msg.code == "seed.auth_failed"
