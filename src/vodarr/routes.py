"""API routes for sources, catalogue, releases and the download queue (ADR-0004).
Every response is built by an explicit serializer: secrets and URLs never leave the server."""

import json
import logging
import math
import re
import sqlite3
import threading
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import Field

from vodarr import downloads, metadata, pipeline, settings, sources, stages
from vodarr.core import MASK, MsgError, register_secret
from vodarr.schemas import ApiModel

log = logging.getLogger(__name__)
Kind = Literal["series", "movie"]


class SourceIn(ApiModel):
    name: str = Field(min_length=1, max_length=100)
    type: Literal["xtream", "m3u"]
    server: str | None = None
    user: str | None = None
    password: str | None = None
    url: str | None = None
    path: str | None = None
    split_prefix: bool = False


ReleaseStatus = Literal["downloading", "processing", "action_required", "done", "no_match", "error"]


class SourcePatch(ApiModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    server: str | None = Field(None, max_length=4096)
    user: str | None = Field(None, max_length=4096)
    password: str | None = Field(None, max_length=4096)
    url: str | None = Field(None, max_length=4096)
    path: str | None = Field(None, max_length=4096)
    split_prefix: bool | None = None
    position: int | None = Field(None, ge=0, le=1_000_000)


class ReleaseIn(ApiModel):
    source_id: str
    kind: Kind
    item_id: str = Field(min_length=1, max_length=100)
    season: int | None = Field(None, ge=0, le=1000)
    episodes: list[str] | None = Field(None, max_length=2000)  # episode ids; None = whole season


class RetryIn(ApiModel):
    stage: Literal["download", "analyse", "search", "verify", "restore", "seed"]


class CompleteIn(ApiModel):
    candidate_id: int


def page_of(items: list, page: int, page_size: int) -> dict:
    total = len(items)
    start = (page - 1) * page_size
    return {
        "data": items[start : start + page_size],
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total_items": total,
            "total_pages": max(1, math.ceil(total / page_size)),
        },
    }


# --- serializers (allow-lists) ---------------------------------------------------------------


def source_out(row: sqlite3.Row) -> dict:
    cfg = json.loads(row["config"])
    if row["type"] == "xtream":
        shown = {"server": cfg.get("server"), "user": cfg.get("user"), "password": MASK}
    else:
        shown = {"url": MASK if cfg.get("url") else None, "path": cfg.get("path")}
    return {
        "id": row["id"],
        "name": row["name"],
        "type": row["type"],
        "split_prefix": bool(row["split_prefix"]),
        "position": row["position"],
        **shown,
    }


def _msg(code: str | None, params: str | None) -> dict | None:
    return {"code": code, "params": json.loads(params or "{}")} if code else None


def release_out(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    files = conn.execute(
        "SELECT count(*) AS n, coalesce(sum(dl_done), 0) AS done, sum(dl_total) AS total"
        " FROM files WHERE release_id = ?",
        (row["id"],),
    ).fetchone()
    return {
        "id": row["id"],
        "source_id": row["source_id"],
        "kind": row["kind"],
        "item_id": row["item_id"],
        "season": row["season"],
        "title": row["title"],
        "year": row["year"],
        "created_at": row["created_at"],
        "status": pipeline.status(conn, row["id"]),
        "tmdb_id": row["tmdb_id"],
        "cover": row["cover"],
        "next_search": row["next_search"],
        "research_until": row["research_until"],
        "files": files["n"],
        "done": files["done"],
        "total": files["total"],
    }


def _safe_info_url(url: str | None) -> str | None:
    """Tracker page link without query or fragment: those can carry keys (authkey, rsskey…)."""
    if not url or not url.startswith(("http://", "https://")):
        return None
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def candidate_out(row: sqlite3.Row) -> dict:
    report = json.loads(row["file_report"] or "[]")
    return {
        "id": row["id"],
        "indexer": row["indexer"],
        "title": row["title"],
        "size": row["size"],
        "scope": row["scope"],
        "is_candidate": bool(row["is_candidate"]),
        "info_url": _safe_info_url(row["info_url"]),
        "seeders": row["seeders"],
        "status": row["status"],
        "problem": row["problem_code"],
        "problem_params": json.loads(row["problem_params"] or "{}"),
        "complete_requested": bool(row["complete_requested"]),
        "linked": bool(row["link_root"]),
        "in_qbittorrent": bool(row["added_to_qbit"]),
        "infohash": row["infohash"],
        "files": [
            {"path": f["path"], "size": f["size"], "status": f["status"], "annex": f["annex"]}
            for f in report
        ],
    }


def release_detail(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    rid = row["id"]
    stage_rows = conn.execute(
        "SELECT * FROM stages WHERE release_id = ? ORDER BY rowid", (rid,)
    ).fetchall()
    file_rows = conn.execute(
        "SELECT * FROM files WHERE release_id = ? ORDER BY season, episode, id", (rid,)
    ).fetchall()
    cands = conn.execute("SELECT * FROM candidates WHERE release_id = ? ORDER BY id", (rid,))
    return {
        **release_out(conn, row),
        "stages": [
            {
                "name": s["name"],
                "state": s["state"],
                "pct": s["pct"],
                "message": _msg(s["msg_code"], s["msg_params"]),
            }
            for s in stage_rows
        ],
        "file_list": [
            {
                "id": f["id"],
                "filename": f["filename"],
                "season": f["season"],
                "episode": f["episode"],
                "size": f["size"],
                "release_name": f["release_name"],
                "status": f["dl_status"],
                "done": f["dl_done"],
                "total": f["dl_total"],
                "error": f["dl_error"],
                "error_params": json.loads(f["dl_error_params"] or "{}"),
                "media": json.loads(f["media_info"]) if f["media_info"] else None,
            }
            for f in file_rows
        ],
        "candidates": [candidate_out(c) for c in cands],
    }


QBIT_LIVE_TIMEOUT = 3.0
QBIT_BACKOFF = 15.0
_qbit_down_until = 0.0


def qbit_states(conn: sqlite3.Connection, hashes: list[str]) -> dict:
    """Live qBittorrent state per infohash (AC-SEED-05); empty if unreachable. The UI polls
    this every few seconds: a short timeout, then a pause after a failure, so a hung
    qBittorrent cannot hold every API worker."""
    global _qbit_down_until
    client = stages.qbittorrent(conn) if hashes else None
    if client is None or time.monotonic() < _qbit_down_until:
        return {}
    client.timeout = QBIT_LIVE_TIMEOUT
    try:
        return {
            t["hash"]: {
                "state": t.get("state"),
                "ratio": t.get("ratio"),
                "uploaded": t.get("uploaded"),
                "progress": t.get("progress"),
            }
            for t in client.info(hashes)
        }
    except MsgError:
        _qbit_down_until = time.monotonic() + QBIT_BACKOFF
        return {}


# --- routes ----------------------------------------------------------------------------------


def register(
    api: APIRouter,
    db: Callable[[], sqlite3.Connection],
    config_dir: Path,
    wake: dict[str, threading.Event],
) -> None:
    def catalogue(source_id: str) -> sources.Catalogue:
        return sources.client(sources.get(db(), source_id), config_dir)

    def source_row(source_id: str) -> sqlite3.Row:
        row = db().execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            raise MsgError("source.not_found")
        return row

    def check_config(type_: str, cfg: dict) -> dict:
        if type_ == "xtream":
            if not (cfg.get("server") or "").startswith(("http://", "https://")):
                raise MsgError("source.bad_url")
            if not cfg.get("user") or not cfg.get("password"):
                raise MsgError("source.credentials_required")
            return {k: cfg[k] for k in ("server", "user", "password")}
        if any("\x00" in str(v) for v in cfg.values() if v):
            raise MsgError("source.bad_url")
        url, path = cfg.get("url"), cfg.get("path")
        if bool(url) == bool(path):
            raise MsgError("source.bad_url")
        if url and not url.startswith(("http://", "https://")):
            raise MsgError("source.bad_url")  # AC-SET-09
        if path and (path.startswith("/") or ".." in Path(path).parts):
            raise MsgError("source.bad_url")  # relative to /config/playlists only
        return {"url": url} if url else {"path": path}

    @api.get("/sources")
    def sources_list():
        rows = db().execute("SELECT * FROM sources ORDER BY position, name").fetchall()
        return [source_out(r) for r in rows]

    @api.post("/sources", status_code=201)
    def sources_create(body: SourceIn):
        cfg = check_config(body.type, body.model_dump())
        src = sources.add(db(), body.name, body.type, cfg, body.split_prefix)
        return source_out(source_row(src.id))

    @api.patch("/sources/{source_id}")
    def sources_update(source_id: str, body: SourcePatch):
        conn = db()
        row = source_row(source_id)
        old = json.loads(row["config"])
        changes = body.model_dump(exclude_none=True)
        new = {
            **old,
            **{
                k: changes[k] for k in ("server", "user", "password", "url", "path") if k in changes
            },
        }
        # A masked or omitted secret is kept only while its server stays the same (AC-SET-03).
        if row["type"] == "xtream" and changes.get("password") in (None, MASK):
            if new.get("server") != old.get("server"):
                raise MsgError("settings.secret_required", service="source")
            new["password"] = old.get("password")
        if new.get("url") == MASK:
            new["url"] = old.get("url")
        if changes.get("path") not in (None, old.get("path")):  # the form resends the old path
            new.pop("url", None)
        elif "url" in changes and changes["url"] != MASK:
            new.pop("path", None)
        new = check_config(row["type"], new)
        register_secret(new.get("password") or new.get("url"))
        conn.execute(
            "UPDATE sources SET name = ?, config = ?, split_prefix = ?, position = ? WHERE id = ?",
            (
                changes.get("name", row["name"]),
                json.dumps(new),
                int(changes.get("split_prefix", row["split_prefix"])),
                changes.get("position", row["position"]),
                source_id,
            ),
        )
        if new != old:
            catalogue(source_id).refresh()
        return source_out(source_row(source_id))

    @api.delete("/sources/{source_id}")
    def sources_delete(source_id: str):
        conn = db()
        source_row(source_id)
        if conn.execute("SELECT 1 FROM releases WHERE source_id = ?", (source_id,)).fetchone():
            raise MsgError("source.in_use")
        catalogue(source_id).refresh()
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        return {"ok": True}

    @api.post("/sources/{source_id}/test")
    def sources_test(source_id: str):
        msg = catalogue(source_id).test()
        return {"ok": msg is None, "error": msg.as_dict() if msg else None}

    @api.post("/sources/{source_id}/refresh")
    def sources_refresh(source_id: str):
        catalogue(source_id).refresh()
        return {"ok": True}

    @api.get("/sources/{source_id}/categories")
    def sources_categories(source_id: str, kind: Kind = "series"):
        return catalogue(source_id).categories(kind)

    @api.get("/sources/{source_id}/items")
    def sources_items(
        source_id: str,
        kind: Kind = "series",
        category: str | None = None,
        q: str = Query("", max_length=200),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200, alias="pageSize"),
    ):
        items = catalogue(source_id).items(kind)
        if category:
            items = [i for i in items if i["category_id"] == category]
        if q:
            items = sources.search(items, q)
        out = page_of(sorted(items, key=lambda i: sources.fold(i["name"])), page, page_size)
        # Titles already queued: several providers often carry the same series.
        out["data"] = [{**i, "releases": queued(source_id, kind, i["id"])} for i in out["data"]]
        return out

    def queued(source_id: str, kind: str, item_id: str) -> list[dict]:
        conn = db()
        rows = conn.execute(
            "SELECT id, season FROM releases WHERE source_id = ? AND kind = ? AND item_id = ?"
            " ORDER BY season",
            (source_id, kind, str(item_id)),
        ).fetchall()
        return [
            {"id": r["id"], "season": r["season"], "status": pipeline.status(conn, r["id"])}
            for r in rows
        ]

    def elsewhere(source_id: str, kind: str, item_id: str, detail: dict) -> list[dict]:
        """Releases of the same title made from another catalogue entry (another provider
        often carries it): same TMDB id, or the same title once provider tags are removed."""
        conn = db()
        key = sources.fold(stages.clean_title(detail.get("name") or ""))
        out = []
        for r in conn.execute(
            "SELECT * FROM releases WHERE kind = ? AND NOT (source_id = ? AND item_id = ?)"
            " ORDER BY title, season",
            (kind, source_id, str(item_id)),
        ):
            same_tmdb = detail.get("tmdb_id") and r["tmdb_id"] == detail["tmdb_id"]
            years = {str(y)[:4] for y in (detail.get("year"), r["year"]) if y}
            same_title = key and sources.fold(stages.clean_title(r["title"])) == key
            if same_tmdb or (same_title and len(years) < 2):  # "Dune" 1984 is not "Dune" 2021
                status = pipeline.status(conn, r["id"])
                out.append(
                    {"id": r["id"], "title": r["title"], "season": r["season"], "status": status}
                )
        return out

    @api.get("/sources/{source_id}/series/{item_id}")
    def sources_series(source_id: str, item_id: str):
        detail = catalogue(source_id).series(item_id)
        return {
            **detail,
            "releases": queued(source_id, "series", item_id),
            "elsewhere": elsewhere(source_id, "series", item_id, detail),
        }

    @api.get("/sources/{source_id}/movies/{item_id}")
    def sources_movie(source_id: str, item_id: str):
        detail = catalogue(source_id).movie(item_id)
        return {
            **detail,
            "releases": queued(source_id, "movie", item_id),
            "elsewhere": elsewhere(source_id, "movie", item_id, detail),
        }

    @api.post("/releases", status_code=201)
    def releases_create(body: ReleaseIn):
        conn = db()
        root = settings.load(conn)["paths"]["downloads"]
        if not root:
            raise MsgError("settings.downloads_missing")
        source = sources.get(conn, body.source_id)
        cat = sources.client(source, config_dir)
        if body.kind == "movie":
            detail = cat.movie(body.item_id)
            title, season = detail["name"] or body.item_id, None
            episodes = [{"id": body.item_id, "num": None, "title": title, "ext": detail["ext"]}]
        else:
            if body.season is None:
                raise MsgError("queue.bad_selection")
            detail = cat.series(body.item_id)
            title, season = detail["name"] or body.item_id, body.season
            found = next((s for s in detail["seasons"] if s["season"] == season), None)
            if not found:
                raise MsgError("source.not_found")
            wanted = set(body.episodes) if body.episodes is not None else None
            # Episodes come from the catalogue, never from the request body (ids only).
            episodes = [e for e in found["episodes"] if wanted is None or e["id"] in wanted]
            if not episodes:
                raise MsgError("queue.bad_selection")
        rid = downloads.enqueue(
            conn,
            source,
            body.kind,
            body.item_id,
            title,
            detail.get("year") or None,
            season,
            episodes,
            Path(root),
        )
        cover = detail.get("cover")
        cover = (
            cover
            if isinstance(cover, str) and re.fullmatch(r"https?://\S{1,2000}", cover)
            else None
        )
        conn.execute(
            "UPDATE releases SET tmdb_id = coalesce(?, tmdb_id), cover = coalesce(?, cover)"
            " WHERE id = ?",
            (detail.get("tmdb_id") or None, cover, rid),
        )
        wake["downloads"].set()
        row = conn.execute("SELECT * FROM releases WHERE id = ?", (rid,)).fetchone()
        return release_out(conn, row)

    @api.get("/releases")
    def releases_list(
        status: Annotated[list[ReleaseStatus] | None, Query()] = None,  # repeatable
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200, alias="pageSize"),
    ):
        conn = db()
        rows = conn.execute("SELECT * FROM releases ORDER BY created_at DESC, id").fetchall()
        if status:  # statuses first, then the full output only for the page shown
            rows = [r for r in rows if pipeline.status(conn, r["id"]) in status]
        out = page_of(rows, page, page_size)
        out["data"] = [release_out(conn, r) for r in out["data"]]
        return out

    def release_row(release_id: str) -> sqlite3.Row:
        row = db().execute("SELECT * FROM releases WHERE id = ?", (release_id,)).fetchone()
        if row is None:
            raise MsgError("release.not_found")
        return row

    @api.get("/releases/{release_id}")
    def releases_get(release_id: str):
        conn = db()
        detail = release_detail(conn, release_row(release_id))
        hashes = [c["infohash"] for c in detail["candidates"] if c["infohash"] and c["linked"]]
        states = qbit_states(conn, hashes)
        for c in detail["candidates"]:
            c["qbittorrent"] = states.get(c["infohash"])
        return detail

    @api.post("/releases/{release_id}/retry")
    def releases_retry(release_id: str, body: RetryIn):
        release_row(release_id)
        pipeline.retry(db(), release_id, body.stage)
        wake["downloads" if body.stage == "download" else "pipeline"].set()
        return {"ok": True}

    @api.post("/releases/{release_id}/research")
    def releases_research(release_id: str):
        """Search now, keeping known verdicts (0.3); the daily clock restarts from now."""
        conn = db()
        row = release_row(release_id)
        now = time.time()
        # Each search may fetch new .torrent files (snatches): not more than once in 5 minutes.
        if row["next_search"] and row["next_search"] - pipeline.DAY > now - 300:
            raise MsgError("release.search_too_soon")
        with pipeline.locked(conn):
            pipeline.ensure_idle(conn, release_id)
            pipeline.search_again(conn, release_id, now)
            conn.execute(  # a manual search restarts the period of daily searches
                "UPDATE releases SET research_until = NULL WHERE id = ?", (release_id,)
            )
        wake["pipeline"].set()
        return {"ok": True}

    @api.post("/releases/{release_id}/complete")
    def releases_complete(release_id: str, body: CompleteIn):
        """« Complete from <tracker> »: link this partial candidate's identical files."""
        conn = db()
        release_row(release_id)
        row = conn.execute(
            "SELECT status FROM candidates WHERE id = ? AND release_id = ?",
            (body.candidate_id, release_id),
        ).fetchone()
        if row is None:
            raise MsgError("candidate.not_found")
        if row["status"] != "partial":
            raise MsgError("candidate.not_partial")
        with pipeline.locked(conn):  # flag and reset together, or neither
            pipeline.ensure_idle(conn, release_id)
            conn.execute(
                "UPDATE candidates SET complete_requested = 1 WHERE id = ?", (body.candidate_id,)
            )
            pipeline._reset(conn, release_id, "restore")
        wake["pipeline"].set()
        return {"ok": True}

    @api.delete("/releases/{release_id}")
    def releases_delete(release_id: str, delete_files: bool = Query(False, alias="deleteFiles")):
        stages.delete_release(db(), config_dir, release_id, delete_files)
        return {"ok": True}

    @api.get("/metadata")
    def metadata_get(
        kind: Kind,
        tmdb_id: str | None = Query(None, alias="tmdbId", pattern=r"^\d{1,12}$"),
        title: str | None = Query(None, max_length=200),
        year: str | None = Query(None, pattern=r"^\d{4}$"),
        lang: Literal["en", "fr"] = "en",
    ):
        """TMDB details for a title (0.1.1); {"data": null} without a key or a match."""
        return {"data": metadata.lookup(db(), config_dir, kind, tmdb_id, title, year, lang)}

    @api.get("/downloads")
    def downloads_list():
        return {"data": downloads.queue(db())}

    @api.post("/downloads/clear-finished")
    def downloads_clear():
        downloads.clear_finished(db())
        return {"ok": True}

    @api.post("/downloads/{file_id}/cancel")
    def downloads_cancel(file_id: int):
        downloads.cancel(db(), file_id)
        return {"ok": True}

    @api.delete("/downloads/{file_id}")
    def downloads_remove(file_id: int):
        downloads.remove(db(), file_id)
        return {"ok": True}
