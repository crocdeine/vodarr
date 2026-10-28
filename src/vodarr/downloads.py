"""Download queue: one global FIFO, one file at a time, resumable via .part files."""

import json
import logging
import os
import re
import sqlite3
import time
import urllib.error
import uuid
from collections.abc import Callable
from pathlib import Path

from vodarr import sources
from vodarr.core import MsgError
from vodarr.sources import EPISODE, Source, open_url, safe_ext, safe_filename

log = logging.getLogger(__name__)


class Cancelled(Exception):  # noqa: N818
    """Raised by a progress callback to stop a download; the .part file is kept."""


class _CallbackError(Exception):  # noqa: N818
    """Wraps an exception raised by the progress callback so it is not retried as a network one."""


CONTENT_RANGE = re.compile(r"bytes (\d+)-\d+/(\d+|\*)")
RETRYABLE_4XX = (408, 429)


def download(
    url: str,
    path: Path,
    tries: int = 5,
    chunk: int = 64 << 10,  # small reads: a cancel is noticed quickly even on slow providers
    progress: Callable[[int, int], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Download `url` to `path` through `path.part`, resuming with Range. Returns the size.
    Gives up after `tries` consecutive attempts without progress. `progress(received, total)`
    may raise Cancelled. Errors never mention the URL (it carries credentials)."""
    part = path.with_name(path.name + ".part")
    failures = 0
    while True:
        before = part.stat().st_size if part.exists() else 0
        have = before
        try:
            with open_url(url, timeout=60, headers={"Range": f"bytes={have}-"}) as r:
                m = CONTENT_RANGE.fullmatch(r.headers.get("Content-Range") or "")
                if r.status == 206 and m and int(m[1]) == have:
                    total = int(m[2]) if m[2] != "*" else 0
                else:  # Range ignored or not where we asked: start over
                    have, total = 0, int(r.headers.get("Content-Length") or 0)
                with part.open("ab" if have else "wb") as f:
                    received = have
                    while buf := r.read(chunk):
                        f.write(buf)
                        received += len(buf)
                        if progress:
                            try:
                                progress(received, total)
                            except Exception as e:
                                raise _CallbackError() from e
            size = part.stat().st_size
            # Known limit: without Content-Length a clean EOF counts as complete; the piece
            # verification catches a truncated file. Check the length if providers do this.
            if not total or size == total:
                break
            if size > total:
                part.unlink()
            log.info("download incomplete (%d/%d bytes), resuming", size, total)
        except _CallbackError as e:
            raise e.__cause__ from None
        except urllib.error.HTTPError as e:
            if e.code == 416 and have:
                m = re.search(r"/(\d+)$", e.headers.get("Content-Range") or "")
                if m and int(m[1]) == have:
                    break  # the .part is already complete
                part.unlink(missing_ok=True)  # stale .part: the file changed upstream
            elif 400 <= e.code < 500 and e.code not in RETRYABLE_4XX:
                raise MsgError("download.http_error", status=e.code) from None
            log.warning("download attempt failed: HTTP %d", e.code)
        except Exception as e:  # noqa: BLE001 - network errors come in many types; retry them all
            log.warning("download attempt failed: %s", type(e).__name__)
        grew = part.exists() and part.stat().st_size > before
        failures = 0 if grew else failures + 1
        if failures >= tries:
            raise MsgError("download.failed", tries=tries)
        sleep(min(60, 5 * max(failures, 1)))
    os.replace(part, path)
    return path.stat().st_size


STAGES = ("download", "analyse", "search", "verify", "restore", "seed")
CHECK_EVERY = 1.0  # seconds between progress writes / cancellation checks
RATES: dict[int, float] = {}  # file id → bytes/s of the running download (in memory only)
ACTIVE: set[int] = set()  # files being written by the worker right now


def _release_dir(downloads_root: Path, source: Source, title: str, season: int | None) -> Path:
    path = downloads_root / safe_filename(source.name) / safe_filename(title)
    return path / f"S{season:02d}" if season is not None else path


def _filename(title: str, season: int | None, num: int | None, ext: str) -> str:
    name = safe_filename(title)
    # Episode files must carry SxxExx: verification matches torrent files by it.
    if season is not None and num is not None and not EPISODE.search(name):
        name = f"{name} - S{season:02d}E{num:02d}"
    return f"{name}.{safe_ext(ext)}"


def enqueue(
    conn: sqlite3.Connection,
    source: Source,
    kind: str,
    item_id: str,
    title: str,
    year: str | None,
    season: int | None,
    episodes: list[dict],
    downloads_root: Path,
    now: int | None = None,
) -> str:
    """Create (or join) the release for this season / movie and queue its files. Idempotent."""
    now = int(time.time()) if now is None else now
    if season is not None and not isinstance(season, int):
        raise MsgError("queue.bad_selection")
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Strict FIFO across calls: a sequence, not the clock.
        seq = conn.execute("SELECT coalesce(max(queued_at), 0) FROM files").fetchone()[0]
        changed = False
        row = conn.execute(
            "SELECT id, dir FROM releases WHERE source_id = ? AND kind = ? AND item_id = ?"
            " AND coalesce(season, -1) = coalesce(?, -1)",
            (source.id, kind, item_id, season),
        ).fetchone()
        if row:
            release_id, folder = row["id"], Path(row["dir"])
        else:
            release_id = uuid.uuid4().hex
            folder = _release_dir(downloads_root, source, title, season)
            if conn.execute("SELECT 1 FROM releases WHERE dir = ?", (str(folder),)).fetchone():
                # Homonymous titles (remakes) must not share a folder.
                base = safe_filename(f"{title} ({year})" if year else f"{title} [{release_id[:6]}]")
                folder = _release_dir(downloads_root, source, base, season)
                if conn.execute("SELECT 1 FROM releases WHERE dir = ?", (str(folder),)).fetchone():
                    base = safe_filename(f"{title} [{release_id[:6]}]")
                    folder = _release_dir(downloads_root, source, base, season)
            conn.execute(
                "INSERT INTO releases (id, source_id, kind, item_id, season, title, year, dir,"
                " created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (release_id, source.id, kind, item_id, season, title, year, str(folder), now),
            )
        taken = {
            r[0]
            for r in conn.execute("SELECT filename FROM files WHERE release_id = ?", (release_id,))
        }
        for ep in episodes:
            if ep.get("num") is not None and not isinstance(ep["num"], int):
                raise MsgError("queue.bad_selection")
            existing = conn.execute(
                "SELECT id, dl_status FROM files WHERE release_id = ? AND stream_id = ?",
                (release_id, ep["id"]),
            ).fetchone()
            if existing:
                if existing["dl_status"] in ("error", "cancelled"):
                    conn.execute(
                        "UPDATE files SET dl_status = 'queued', dl_error = NULL, in_queue = 1,"
                        " queued_at = ? WHERE id = ?",
                        (seq := seq + 1, existing["id"]),
                    )
                    changed = True
                continue
            name = _filename(ep["title"], season, ep.get("num"), ep["ext"])
            if name in taken:
                stem, ext = name.rsplit(".", 1)
                name = f"{stem} [{safe_filename(ep['id'])}].{ext}"
            taken.add(name)
            conn.execute(
                "INSERT INTO files (release_id, stream_id, ext, season, episode, filename,"
                " queued_at) VALUES (?,?,?,?,?,?,?)",
                (
                    release_id,
                    ep["id"],
                    safe_ext(ep["ext"]),
                    season,
                    ep.get("num"),
                    name,
                    seq := seq + 1,
                ),
            )
            changed = True
        conn.executemany(
            "INSERT OR IGNORE INTO stages (release_id, name) VALUES (?, ?)",
            [(release_id, name) for name in STAGES],
        )
        if changed:  # new or re-queued files: the whole pipeline runs again
            conn.execute(
                "UPDATE stages SET state = 'pending', pct = 0, msg_code = NULL, msg_params = NULL"
                " WHERE release_id = ?",
                (release_id,),
            )
            # Verdicts were given against fewer files: checked again (from the saved .torrent,
            # nothing is downloaded twice); the automatic searches start over.
            conn.execute(
                "UPDATE candidates SET status = 'pending', problem_code = NULL,"
                " problem_params = NULL, file_report = NULL"
                " WHERE release_id = ? AND link_root IS NULL AND added_to_qbit = 0",
                (release_id,),
            )
            conn.execute(
                "UPDATE releases SET next_search = NULL, research_until = NULL WHERE id = ?",
                (release_id,),
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return release_id


def recover(conn: sqlite3.Connection) -> None:
    """At startup: an interrupted download goes back to the queue and resumes from its .part."""
    conn.execute("UPDATE files SET dl_status = 'queued' WHERE dl_status = 'running'")


def _update_download_stage(conn: sqlite3.Connection, release_id: str) -> None:
    states = {
        r[0]
        for r in conn.execute("SELECT dl_status FROM files WHERE release_id = ?", (release_id,))
    }
    if states & {"queued", "running"}:
        return
    if "error" in states:
        state, code = "error", "download.some_failed"
    elif "done" in states:
        state, code = "done", None  # cancelled files are left out of the release's work
    else:  # only cancelled files, or none left at all
        state, code = "error", "download.cancelled"
    conn.execute(
        "UPDATE stages SET state = ?, pct = 100, msg_code = ? WHERE release_id = ?"
        " AND name = 'download'",
        (state, code, release_id),
    )


def process_next(
    conn: sqlite3.Connection, config_dir: Path, sleep: Callable[[float], None] = time.sleep
) -> dict | None:
    """Download the oldest queued file. Returns its row, or None if the queue is empty."""
    row = conn.execute(
        "SELECT f.*, r.dir, r.source_id, r.kind FROM files f JOIN releases r ON r.id = f.release_id"
        " WHERE f.dl_status = 'queued' ORDER BY f.queued_at, f.id LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    fid = row["id"]
    claimed = conn.execute(
        "UPDATE files SET dl_status = 'running', dl_error = NULL"
        " WHERE id = ? AND dl_status = 'queued'",
        (fid,),
    ).rowcount
    if not claimed:  # cancelled by the API between the SELECT and now
        return dict(row)
    conn.execute(
        "UPDATE stages SET state = 'running' WHERE release_id = ? AND name = 'download'",
        (row["release_id"],),
    )
    ACTIVE.add(fid)
    last = {"t": 0.0, "bytes": 0}

    def progress(done: int, total: int) -> None:
        now = time.monotonic()
        if now - last["t"] < CHECK_EVERY:
            return
        if last["t"]:
            RATES[fid] = (done - last["bytes"]) / (now - last["t"])
        last.update(t=now, bytes=done)
        conn.execute("UPDATE files SET dl_done = ?, dl_total = ? WHERE id = ?", (done, total, fid))
        stop_if_cancelled()

    def stop_if_cancelled() -> None:
        found = conn.execute("SELECT dl_status FROM files WHERE id = ?", (fid,)).fetchone()
        if found is None or found[0] != "running":  # cancelled, or removed with its release
            raise Cancelled

    def wait(seconds: float) -> None:  # between attempts: a cancel must not wait for data
        sleep(seconds)
        stop_if_cancelled()

    try:
        source = sources.get(conn, row["source_id"])
        url = sources.client(source, config_dir).stream_url(
            row["kind"], row["stream_id"], row["ext"]
        )
        folder = Path(row["dir"])
        folder.mkdir(parents=True, exist_ok=True)
        size = download(url, folder / row["filename"], progress=progress, sleep=wait)
        conn.execute(
            "UPDATE files SET dl_status = 'done', dl_done = ?, dl_total = ?"
            " WHERE id = ? AND dl_status = 'running'",
            (size, size, fid),
        )
    except Cancelled:
        pass
    except MsgError as e:
        _fail(conn, fid, e.msg.code, e.msg.params)
    except OSError as e:
        log.warning("download of file %d failed: %s", fid, e.strerror)
        _fail(conn, fid, "download.disk_error")
    except Exception as e:  # noqa: BLE001 - never leave a file stuck in 'running'
        # Only the type: messages and tracebacks may carry the stream URL.
        log.warning("download of file %d failed: %s", fid, type(e).__name__)
        _fail(conn, fid, "download.failed")
    finally:
        RATES.pop(fid, None)
        ACTIVE.discard(fid)
    _update_download_stage(conn, row["release_id"])
    return dict(row)


def _fail(conn: sqlite3.Connection, fid: int, code: str, params: dict | None = None) -> None:
    conn.execute(
        "UPDATE files SET dl_status = 'error', dl_error = ?, dl_error_params = ?"
        " WHERE id = ? AND dl_status = 'running'",
        (code, json.dumps(params) if params else None, fid),
    )


def cancel(conn: sqlite3.Connection, file_id: int) -> None:
    conn.execute(
        "UPDATE files SET dl_status = 'cancelled'"
        " WHERE id = ? AND dl_status IN ('queued', 'running')",
        (file_id,),
    )


def remove(conn: sqlite3.Connection, file_id: int) -> None:
    """Remove a queued, cancelled or failed item and its .part. Finished files stay (clear them)."""
    row = conn.execute(
        "SELECT f.*, r.dir FROM files f JOIN releases r ON r.id = f.release_id WHERE f.id = ?",
        (file_id,),
    ).fetchone()
    if row is None:
        raise MsgError("queue.not_found")
    if row["dl_status"] not in ("queued", "cancelled", "error") or file_id in ACTIVE:
        raise MsgError("queue.not_removable")  # still being written: wait for the cancel
    (Path(row["dir"]) / (row["filename"] + ".part")).unlink(missing_ok=True)
    conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
    _update_download_stage(conn, row["release_id"])


def clear_finished(conn: sqlite3.Connection) -> None:
    """Hide finished items from the queue; the files and the release are untouched."""
    conn.execute("UPDATE files SET in_queue = 0 WHERE dl_status = 'done'")


def queue(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT f.id, f.release_id, r.title, r.season, f.episode, f.filename, f.dl_status,"
        " f.dl_done, f.dl_total, f.dl_error, f.dl_error_params"
        " FROM files f JOIN releases r ON r.id = f.release_id"
        " WHERE f.in_queue = 1 ORDER BY f.queued_at, f.id"
    )
    return [
        {
            "id": r["id"],
            "release_id": r["release_id"],
            "title": r["title"],
            "season": r["season"],
            "episode": r["episode"],
            "filename": r["filename"],
            "status": r["dl_status"],
            "done": r["dl_done"],
            "total": r["dl_total"],
            "error": r["dl_error"],
            "error_params": json.loads(r["dl_error_params"] or "{}"),
            "rate": RATES.get(r["id"], 0.0),
        }
        for r in rows
    ]
