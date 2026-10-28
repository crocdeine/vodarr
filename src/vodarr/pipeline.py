"""Pipeline engine (ADR-0002): stages chain once the download is over; the stage functions
live in stages.py."""

import contextlib
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from vodarr import downloads
from vodarr.core import Msg, MsgError, get_setting

log = logging.getLogger(__name__)

STAGES = ("download", "analyse", "search", "verify", "restore", "seed")


class Skip(MsgError):
    """The stage does not apply (service not configured, nothing to do): the pipeline goes on.
    Caught before MsgError by the engine."""


@dataclass
class Ctx:
    conn: sqlite3.Connection
    config_dir: Path
    release: sqlite3.Row
    progress: Callable[[float, float], None]


Handler = Callable[[Ctx], Msg | None]
SET = (
    "UPDATE stages SET state = ?, msg_code = ?, msg_params = ?, pct = coalesce(?, pct)"
    " WHERE release_id = ? AND name = ?"
)
SET_RAN = SET + " AND state = 'running'"


def _set(
    conn, release_id, name, state, msg: Msg | None = None, pct: int | None = None, *, ran=False
) -> None:
    """`ran`: the end of a run, written only if the stage is still running (new episodes,
    a retry or a delete may have reset it meanwhile: the reset wins)."""
    sql = SET_RAN if ran else SET
    conn.execute(
        sql, (state, msg and msg.code, msg and json.dumps(msg.params), pct, release_id, name)
    )


def _next_stage(conn: sqlite3.Connection) -> tuple[sqlite3.Row, str] | None:
    """Oldest release whose download is over and whose next stage is pending."""
    for release in conn.execute("SELECT * FROM releases ORDER BY created_at, id").fetchall():
        rows = conn.execute(
            "SELECT name, state FROM stages WHERE release_id = ?", (release["id"],)
        ).fetchall()
        state = {r["name"]: r["state"] for r in rows}
        for name in STAGES:
            current = state.get(name)
            if current in ("done", "skipped"):
                continue
            if current == "pending" and name != "download":
                return release, name
            break  # running, error, or download not finished: this release waits
    return None


def process_next(
    conn: sqlite3.Connection, config_dir: Path, handlers: dict[str, Handler]
) -> str | None:
    """Run one pending stage. Returns the release id, or None if nothing is ready."""
    with locked(conn):  # claim atomically: the API thread may retry or delete meanwhile
        found = _next_stage(conn)
        if not found:
            return None
        release, name = found
        rid = release["id"]
        _set(conn, rid, name, "running", pct=0)

    def progress(done: float, total: float) -> None:
        pct = int(100 * done / total) if total else 0
        conn.execute(
            "UPDATE stages SET pct = ? WHERE release_id = ? AND name = ? AND state = 'running'",
            (pct, rid, name),
        )

    try:
        msg = handlers[name](Ctx(conn, config_dir, release, progress))
        _set(conn, rid, name, "done", msg, pct=100, ran=True)
    except Skip as e:
        _set(conn, rid, name, "skipped", e.msg, pct=100, ran=True)
    except MsgError as e:
        _set(conn, rid, name, "error", e.msg, ran=True)
    except Exception:
        # Details stay in the (redacted) log: exception text may carry URLs with credentials.
        log.exception("stage %s of release %s failed", name, rid)
        _set(conn, rid, name, "error", Msg("pipeline.internal_error"), ran=True)
    return rid


def recover(conn: sqlite3.Connection) -> None:
    """At startup: a stage interrupted while running starts again (AC-CORE-05)."""
    conn.execute(
        "UPDATE stages SET state = 'pending' WHERE state = 'running' AND name != 'download'"
    )


@contextlib.contextmanager
def locked(conn: sqlite3.Connection):
    """A write transaction taken up front: check-then-act without races between threads."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def ensure_idle(conn: sqlite3.Connection, release_id: str) -> None:
    """Refuse to change a release while one of its stages runs (call inside `locked`)."""
    if not conn.execute("SELECT 1 FROM releases WHERE id = ?", (release_id,)).fetchone():
        raise MsgError("release.not_found")
    running = conn.execute(
        "SELECT 1 FROM stages WHERE release_id = ? AND state = 'running' AND name != 'download'"
        " AND coalesce(msg_code, '') != 'seed.waiting'",  # only watching qBittorrent: interruptible
        (release_id,),
    ).fetchone()
    if running:
        raise MsgError("release.busy")


def retry(conn: sqlite3.Connection, release_id: str, stage: str) -> None:
    """Reset `stage` and every following stage to pending (AC-PIPE-03)."""
    if stage not in STAGES:
        raise MsgError("pipeline.unknown_stage", stage=stage)
    with locked(conn):
        ensure_idle(conn, release_id)
        _reset(conn, release_id, stage)
        if STAGES.index(stage) <= STAGES.index("verify"):
            # An explicit retry checks every candidate again (from the .torrent already saved:
            # nothing is downloaded twice). Links and torrents in qBittorrent keep their verdict.
            conn.execute(
                "UPDATE candidates SET status = 'pending', problem_code = NULL,"
                " problem_params = NULL, file_report = NULL"
                " WHERE release_id = ? AND link_root IS NULL AND added_to_qbit = 0",
                (release_id,),
            )


def _reset(conn: sqlite3.Connection, release_id: str, stage: str) -> None:
    names = STAGES[STAGES.index(stage) :]
    conn.executemany(
        "UPDATE stages SET state = 'pending', pct = 0, msg_code = NULL, msg_params = NULL"
        " WHERE release_id = ? AND name = ?",
        [(release_id, n) for n in names],
    )
    if stage == "download":
        conn.execute(
            "UPDATE files SET dl_status = 'queued', dl_error = NULL, in_queue = 1"
            " WHERE release_id = ? AND dl_status IN ('error', 'cancelled')",
            (release_id,),
        )
        # Done files are never downloaded again (they may be linked and seeding): with nothing
        # re-queued, the download stage is complete at once instead of waiting forever.
        downloads._update_download_stage(conn, release_id)


DAY = 86400
_last_tick = 0.0


def research_tick(conn: sqlite3.Connection, now: float | None = None) -> bool:
    """0.3: releases still without a match are searched again once a day for `researchDays`
    days (Settings › Prowlarr, 0 = never): many releases reach the trackers after the provider.
    Checked at most once a minute. True when a search was queued."""
    global _last_tick
    now = time.time() if now is None else now
    if now - _last_tick < 60:
        return False
    _last_tick = now
    cfg = (get_setting(conn, "services", {}) or {}).get("prowlarr") or {}
    days = cfg.get("researchDays", 30)
    if not days:
        return False
    queued = False
    with locked(conn):
        for row in conn.execute(
            "SELECT id, next_search, research_until FROM releases"
            " WHERE research_until IS NULL OR research_until > ? ORDER BY next_search, created_at",
            (now,),
        ).fetchall():
            if status(conn, row["id"]) != "no_match":
                continue
            if row["research_until"] is None:  # first time without a match: start the clock
                conn.execute(
                    "UPDATE releases SET next_search = ?, research_until = ? WHERE id = ?",
                    (int(now + DAY), int(now + days * DAY), row["id"]),
                )
            elif row["next_search"] <= now < row["research_until"]:
                search_again(conn, row["id"], now)
                queued = True
                break  # one release per minute: trackers are not hit all at once
    return queued


def search_again(conn: sqlite3.Connection, release_id: str, now: float) -> None:
    """New search keeping known verdicts: only new results are checked (call inside `locked`)."""
    _reset(conn, release_id, "search")
    # A tracker down on the day: candidates that failed are tried again ("different" stays).
    conn.execute(
        "UPDATE candidates SET status = 'pending', problem_code = NULL, problem_params = NULL"
        " WHERE release_id = ? AND status = 'error'",
        (release_id,),
    )
    conn.execute("UPDATE releases SET next_search = ? WHERE id = ?", (int(now + DAY), release_id))


def status(conn: sqlite3.Connection, release_id: str) -> str:
    """Aggregated status, computed, never stored (AC-PIPE-04):
    downloading | processing | action_required | done | no_match | error.
    no_match: finished, but nothing linked or seeded (no identical release on the trackers)."""
    rows = conn.execute(
        "SELECT name, state, msg_code FROM stages WHERE release_id = ?", (release_id,)
    ).fetchall()
    stage = {r["name"]: r for r in rows}
    if not stage:
        return "error"
    files = {
        r[0]
        for r in conn.execute("SELECT dl_status FROM files WHERE release_id = ?", (release_id,))
    }
    if any(r["state"] == "error" for r in rows):
        return "error"
    if files & {"queued", "running"} or stage["download"]["state"] in ("pending", "running"):
        return "downloading"
    if any(r["state"] in ("pending", "running") for r in rows):
        return "processing"
    # A partial candidate waits for "complete from" unless its scope already has a reference.
    waiting = conn.execute(
        "SELECT 1 FROM candidates p WHERE p.release_id = ? AND p.status = 'partial'"
        " AND p.complete_requested = 0 AND NOT EXISTS (SELECT 1 FROM candidates i"
        " WHERE i.release_id = p.release_id AND i.scope = p.scope"
        " AND i.status IN ('identical', 'identical_annex'))",
        (release_id,),
    ).fetchone()
    if waiting:
        return "action_required"
    seeded = conn.execute(
        "SELECT 1 FROM candidates WHERE release_id = ?"
        " AND (link_root IS NOT NULL OR added_to_qbit = 1)",
        (release_id,),
    ).fetchone()
    # no_match only after a real search found nothing identical: a search or restore skipped for
    # a setting (no Prowlarr, no link folder, no searchable title) is not something to search again.
    searched = stage["search"]["state"] == "done"
    nothing = stage["restore"]["msg_code"] == "restore.no_reference"
    return "no_match" if not seeded and searched and nothing else "done"


def handlers() -> dict[str, Handler]:
    from vodarr import stages  # noqa: PLC0415 - stages imports this module

    return {
        "analyse": stages.stage_analyse,
        "search": stages.stage_search,
        "verify": stages.stage_verify,
        "restore": stages.stage_restore,
        "seed": stages.stage_seed,
    }
