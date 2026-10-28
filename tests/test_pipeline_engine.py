import pytest

from vodarr import downloads, pipeline, sources
from vodarr.core import Msg, MsgError


def make_release(conn, tmp_path, download_state="done", item="100"):
    src = sources.add(conn, "S", "xtream", {"server": "http://x", "user": "u", "password": "p"})
    eps = [{"id": "1", "num": 1, "title": "Show - S01E01", "ext": "mkv"}]
    rid = downloads.enqueue(
        conn, src, "series", item, f"Show {item}", None, 1, eps, tmp_path / "dl", now=1
    )
    if download_state == "done":
        conn.execute("UPDATE files SET dl_status = 'done' WHERE release_id = ?", (rid,))
    conn.execute(
        "UPDATE stages SET state = ? WHERE name = 'download' AND release_id = ?",
        (download_state, rid),
    )
    return rid


def states(conn, rid):
    rows = conn.execute(
        "SELECT name, state FROM stages WHERE release_id = ? ORDER BY rowid", (rid,)
    )
    return {r["name"]: r["state"] for r in rows}


def handlers(log, **overrides):
    def ok(name):
        def run(ctx):
            log.append(name)
            ctx.progress(1, 2)
            return Msg(f"{name}.done")

        return run

    out = {name: ok(name) for name in pipeline.STAGES}
    out.update(overrides)
    return out


def nothing_found(log):
    """Stages where the search ran and nothing identical was found."""

    def restore(ctx):
        log.append("restore")
        raise pipeline.Skip("restore.no_reference")

    return handlers(log, restore=restore)


def run_all(conn, tmp_path, hs):
    while pipeline.process_next(conn, tmp_path / "config", hs):
        pass


def test_stages_chain_in_order(conn, tmp_path):
    """AC-PIPE-01"""
    rid = make_release(conn, tmp_path)
    log = []
    run_all(conn, tmp_path, handlers(log))
    assert log == ["analyse", "search", "verify", "restore", "seed"]
    assert set(states(conn, rid).values()) == {"done"}
    assert pipeline.status(conn, rid) == "done"  # restore did not report "no reference"
    msg = conn.execute(
        "SELECT msg_code, pct FROM stages WHERE release_id = ? AND name = 'seed'", (rid,)
    ).fetchone()
    assert (msg["msg_code"], msg["pct"]) == ("seed.done", 100)


def test_waits_for_download(conn, tmp_path):
    rid = make_release(conn, tmp_path, download_state="running")
    log = []
    assert pipeline.process_next(conn, tmp_path / "config", handlers(log)) is None
    assert log == [] and pipeline.status(conn, rid) == "downloading"


def test_skipped_stage_continues(conn, tmp_path):
    """AC-PIPE-02"""
    rid = make_release(conn, tmp_path)
    log = []

    def skip(ctx):
        raise pipeline.Skip("search.no_prowlarr")

    run_all(conn, tmp_path, handlers(log, search=skip))
    assert states(conn, rid)["search"] == "skipped"
    assert log == ["analyse", "verify", "restore", "seed"]
    assert pipeline.status(conn, rid) == "done"  # restore did not report "no reference"


def test_error_stops_release(conn, tmp_path):
    """AC-PIPE-01"""
    rid = make_release(conn, tmp_path)
    log = []

    def fail(ctx):
        raise MsgError("verify.unreadable")

    run_all(conn, tmp_path, handlers(log, verify=fail))
    assert log == ["analyse", "search"]
    s = states(conn, rid)
    assert (s["verify"], s["restore"], s["seed"]) == ("error", "pending", "pending")
    assert pipeline.status(conn, rid) == "error"


def test_unexpected_exception_is_an_error_without_details(conn, tmp_path):
    rid = make_release(conn, tmp_path)

    def boom(ctx):
        raise RuntimeError("http://u:secret@host leaked?")

    run_all(conn, tmp_path, handlers([], analyse=boom))
    row = conn.execute(
        "SELECT state, msg_code, msg_params FROM stages WHERE release_id = ? AND name = 'analyse'",
        (rid,),
    ).fetchone()
    assert (row["state"], row["msg_code"]) == ("error", "pipeline.internal_error")
    assert "secret" not in (row["msg_params"] or "")


def test_retry_from_stage_resets_following(conn, tmp_path):
    """AC-PIPE-03"""
    rid = make_release(conn, tmp_path)
    run_all(conn, tmp_path, handlers([]))
    pipeline.retry(conn, rid, "verify")
    s = states(conn, rid)
    assert [s[n] for n in pipeline.STAGES] == [
        "done",
        "done",
        "done",
        "pending",
        "pending",
        "pending",
    ]
    log = []
    run_all(conn, tmp_path, handlers(log))
    assert log == ["verify", "restore", "seed"]


def test_retry_download_requeues_failed_files(conn, tmp_path):
    rid = make_release(conn, tmp_path)
    conn.execute("UPDATE files SET dl_status = 'error'")
    pipeline.retry(conn, rid, "download")
    assert conn.execute("SELECT dl_status FROM files").fetchone()[0] == "queued"
    assert pipeline.status(conn, rid) == "downloading"


def test_unknown_stage_rejected(conn, tmp_path):
    rid = make_release(conn, tmp_path)
    with pytest.raises(MsgError):
        pipeline.retry(conn, rid, "nope")


def test_running_stage_resumes_after_restart(conn, tmp_path):
    """AC-CORE-05"""
    rid = make_release(conn, tmp_path)
    conn.execute(
        "UPDATE stages SET state = 'running' WHERE release_id = ? AND name = 'analyse'", (rid,)
    )
    pipeline.recover(conn)
    log = []
    run_all(conn, tmp_path, handlers(log))
    assert log[0] == "analyse"


def test_action_required_status(conn, tmp_path):
    """AC-PIPE-04"""
    rid = make_release(conn, tmp_path)

    def partial(ctx):
        raise pipeline.Skip("restore.partial_available", tracker="Nimbus")

    run_all(conn, tmp_path, handlers([], restore=partial))
    cand = "INSERT INTO candidates (release_id, indexer, title, scope, is_candidate, status) VALUES (?, ?, 't', 'pack', 1, ?)"
    conn.execute(cand, (rid, "Nimbus", "partial"))
    assert pipeline.status(conn, rid) == "action_required"
    conn.execute(cand, (rid, "Aurora", "identical"))  # the scope already has a reference
    conn.execute("UPDATE candidates SET link_root = '/links/x' WHERE indexer = 'Aurora'")
    assert pipeline.status(conn, rid) == "done"


def test_retry_refused_while_a_stage_runs(conn, tmp_path):
    """the worker would overwrite the reset."""
    rid = make_release(conn, tmp_path)
    conn.execute(
        "UPDATE stages SET state = 'running' WHERE release_id = ? AND name = 'verify'", (rid,)
    )
    with pytest.raises(MsgError) as err:
        pipeline.retry(conn, rid, "search")
    assert err.value.msg.code == "release.busy"


def test_deleted_release_between_claim_and_run_is_harmless(conn, tmp_path):
    rid = make_release(conn, tmp_path)

    def analyse(ctx):
        conn.execute("DELETE FROM releases WHERE id = ?", (rid,))
        return pipeline.Msg("analyse.done")

    pipeline.process_next(conn, tmp_path / "config", handlers([], analyse=analyse))
    assert conn.execute("SELECT count(*) FROM stages").fetchone()[0] == 0


def test_progress_is_stored(conn, tmp_path):
    rid = make_release(conn, tmp_path)
    seen = []

    def analyse(ctx):
        ctx.progress(1, 4)
        seen.append(
            conn.execute(
                "SELECT pct FROM stages WHERE release_id = ? AND name = 'analyse'", (rid,)
            ).fetchone()[0]
        )
        return Msg("analyse.done")

    pipeline.process_next(conn, tmp_path / "config", handlers([], analyse=analyse))
    assert seen == [25]


def test_reset_while_running_wins_over_the_stale_result(conn, tmp_path):
    """Bug hunt: episodes added while seed ran were linked but never seeded."""
    rid = make_release(conn, tmp_path)

    def seed_then_reset(ctx):  # new episodes arrive while this stage runs
        conn.execute("UPDATE stages SET state = 'pending' WHERE release_id = ?", (rid,))
        return Msg("seed.done")

    log = []
    hs = handlers(log, seed=seed_then_reset)
    while pipeline.process_next(conn, tmp_path / "config", hs) and len(log) < 4:
        pass
    pipeline.process_next(conn, tmp_path / "config", hs)  # the seed run, reset meanwhile
    assert states(conn, rid)["seed"] == "pending"


def test_retry_download_of_a_finished_release_completes(conn, tmp_path):
    """Bug hunt: the download stage stayed pending forever (no file is re-queued)."""
    rid = make_release(conn, tmp_path)
    run_all(conn, tmp_path, handlers([]))
    pipeline.retry(conn, rid, "download")
    assert states(conn, rid)["download"] == "done"
    log = []
    run_all(conn, tmp_path, handlers(log))
    assert log == ["analyse", "search", "verify", "restore", "seed"]


def test_release_without_files_is_not_stuck_downloading(conn, tmp_path):
    rid = make_release(conn, tmp_path, download_state="running")
    conn.execute("DELETE FROM files WHERE release_id = ?", (rid,))
    downloads._update_download_stage(conn, rid)
    assert states(conn, rid)["download"] == "error"


def test_no_match_status(conn, tmp_path):
    """0.2: a finished release with nothing linked or seeded is not "done"."""
    rid = make_release(conn, tmp_path)
    run_all(conn, tmp_path, nothing_found([]))
    assert pipeline.status(conn, rid) == "no_match"
    conn.execute(
        "INSERT INTO candidates (release_id, indexer, title, scope, is_candidate, status,"
        " added_to_qbit) VALUES (?, 'Nimbus', 't', 'pack', 1, 'identical', 1)",
        (rid,),
    )
    assert pipeline.status(conn, rid) == "done"


def test_release_without_match_is_searched_again_daily(conn, tmp_path, monkeypatch):
    """0.3: a release still without a match is searched once a day for researchDays days."""
    from vodarr import core

    monkeypatch.setattr(pipeline, "_last_tick", 0.0)
    rid = make_release(conn, tmp_path)
    run_all(conn, tmp_path, nothing_found([]))
    assert pipeline.status(conn, rid) == "no_match"
    core.set_setting(conn, "services", {"prowlarr": {"researchDays": 3}})
    day, t0 = pipeline.DAY, 1_000_000.0
    assert pipeline.research_tick(conn, t0) is False  # starts the clock
    row = conn.execute("SELECT next_search, research_until FROM releases").fetchone()
    assert (row["next_search"], row["research_until"]) == (t0 + day, t0 + 3 * day)
    assert pipeline.research_tick(conn, t0 + 120) is False  # not due yet
    assert pipeline.research_tick(conn, t0 + day + 1) is True
    assert states(conn, rid)["search"] == "pending" and states(conn, rid)["verify"] == "pending"
    log = []
    run_all(conn, tmp_path, nothing_found(log))
    assert log == ["search", "verify", "restore", "seed"]  # download and analyse are not redone
    assert pipeline.research_tick(conn, t0 + 2 * day + 2) is True
    run_all(conn, tmp_path, nothing_found([]))
    assert pipeline.research_tick(conn, t0 + 3 * day + 3) is False  # the period is over


def test_automatic_search_can_be_turned_off(conn, tmp_path, monkeypatch):
    from vodarr import core

    monkeypatch.setattr(pipeline, "_last_tick", 0.0)
    rid = make_release(conn, tmp_path)
    run_all(conn, tmp_path, nothing_found([]))
    core.set_setting(conn, "services", {"prowlarr": {"researchDays": 0}})
    assert pipeline.research_tick(conn, 2_000_000.0) is False
    assert (
        conn.execute("SELECT next_search FROM releases WHERE id = ?", (rid,)).fetchone()[0] is None
    )


def test_skipped_search_is_not_no_match(conn, tmp_path):
    """Backend audit: no Prowlarr (search skipped) must not be searched again daily."""

    def no_prowlarr(ctx):
        raise pipeline.Skip("search.no_prowlarr")

    rid = make_release(conn, tmp_path)
    log = []
    hs = nothing_found(log)
    hs["search"] = no_prowlarr
    run_all(conn, tmp_path, hs)
    assert pipeline.status(conn, rid) == "done"


def test_one_release_searched_per_tick(conn, tmp_path, monkeypatch):
    """Backend audit: releases due together must not hit the trackers in the same minute."""
    from vodarr import core

    rids = [make_release(conn, tmp_path, item=str(i)) for i in range(3)]
    run_all(conn, tmp_path, nothing_found([]))
    core.set_setting(conn, "services", {"prowlarr": {"researchDays": 30}})
    monkeypatch.setattr(pipeline, "_last_tick", 0.0)
    t0 = 1_000_000.0
    pipeline.research_tick(conn, t0)
    due = t0 + pipeline.DAY + 1
    for minute in range(3):
        assert pipeline.research_tick(conn, due + 60 * minute) is True
        pending = [r for r in rids if states(conn, r)["search"] == "pending"]
        assert len(pending) == minute + 1
