"""Pipeline stages: analyse → search → verify → restore → seed. Each returns a message,
raises Skip when it does not apply, or MsgError on failure (see pipeline.py for the engine)."""

import contextlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

from vodarr import links, services, torrent
from vodarr.core import Msg, MsgError, atomic_write, get_setting
from vodarr.pipeline import Ctx, Skip, ensure_idle, locked
from vodarr.sources import RELEASE, fold

log = logging.getLogger(__name__)


# --- analyse ---------------------------------------------------------------------------------

FFPROBE = shutil.which("ffprobe") or "ffprobe"


def ffprobe_json(path: Path) -> str:
    out = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            FFPROBE,
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-show_entries",
            "format=duration,bit_rate,format_name:format_tags=title"
            ":stream=codec_type,codec_name,profile,width,height,channels,r_frame_rate,"
            "color_transfer:stream_tags=title,language",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return out.stdout


def probe(path: Path) -> tuple[str | None, dict | None]:
    """(release name from the format or track `title` tags, technical metadata)."""
    try:
        data = json.loads(ffprobe_json(path) or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        log.warning("ffprobe unavailable or failed on %s", path.name)
        return None, None
    if not isinstance(data, dict):
        return None, None
    streams = [st for st in data.get("streams") or [] if isinstance(st, dict)]
    fmt = data.get("format") if isinstance(data.get("format"), dict) else {}
    titles = [(fmt.get("tags") or {}).get("title")]
    titles += [(st.get("tags") or {}).get("title") for st in streams]
    release = next((t for t in titles if isinstance(t, str) and RELEASE.match(t)), None)
    return release, media_info(fmt, streams)


def _number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def media_info(fmt: dict, streams: list[dict]) -> dict | None:
    """The technical facts a release page shows: resolution, codecs, languages, duration."""
    if not streams:
        return None
    video = next((st for st in streams if st.get("codec_type") == "video"), {})

    def lang(st: dict) -> str | None:
        value = (st.get("tags") or {}).get("language")
        return value if isinstance(value, str) and value != "und" else None

    fps = None
    if isinstance(video.get("r_frame_rate"), str) and "/" in video["r_frame_rate"]:
        num, den = (_number(x) for x in video["r_frame_rate"].split("/", 1))
        fps = round(num / den, 3) if num and den else None
    return {
        "container": fmt.get("format_name"),
        "duration": _number(fmt.get("duration")),
        "bitrate": _number(fmt.get("bit_rate")),
        "video": {
            "codec": video.get("codec_name"),
            "profile": video.get("profile"),
            "width": video.get("width"),
            "height": video.get("height"),
            "fps": fps,
            "hdr": video.get("color_transfer") in ("smpte2084", "arib-std-b67"),
        }
        if video
        else None,
        "audio": [
            {"codec": st.get("codec_name"), "channels": st.get("channels"), "language": lang(st)}
            for st in streams
            if st.get("codec_type") == "audio"
        ],
        "subtitles": [lang(st) for st in streams if st.get("codec_type") == "subtitle"],
    }


def release_files(conn: sqlite3.Connection, release: sqlite3.Row) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM files WHERE release_id = ? AND dl_status = 'done'"
        " ORDER BY season, episode, id",
        (release["id"],),
    ).fetchall()


def file_path(release: sqlite3.Row, row: sqlite3.Row) -> Path:
    return Path(release["dir"]) / row["filename"]


def stage_analyse(ctx: Ctx) -> Msg:
    rows = release_files(ctx.conn, ctx.release)
    names = []
    for i, row in enumerate(rows, start=1):
        path = file_path(ctx.release, row)
        if not path.is_file():
            raise MsgError("analyse.file_missing", file=row["filename"])
        name, media = probe(path)
        ctx.conn.execute(
            "UPDATE files SET size = ?, release_name = ?, media_info = ? WHERE id = ?",
            (path.stat().st_size, name, json.dumps(media) if media else None, row["id"]),
        )
        names.append(name)
        ctx.progress(i, len(rows))
    known = next((n for n in names if n), None)
    if known and ctx.release["kind"] == "series":
        known = re.sub(r"(S\d{2})E\d{2}", r"\1", known, count=1, flags=re.I)
    return Msg("analyse.done", files=len(rows), release=known)


# --- search ----------------------------------------------------------------------------------

TOLERANCE = 0.15  # size slack around the local size; piece verification decides anyway


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", fold(text).replace("&", " and "))


# Provider tags before titles: "┃FR┃ Title", "[4K] Title", "NF - Title", "FR | Title".
# Unbracketed tags are country codes or known labels only: "CSI: Miami", "FBI - Most Wanted".
TAG = r"(?:[A-Z]{2}|[48]K|TOP|NEW|VIP|VFF|VFQ|VOST|VOSTFR|MULTI|AMZ|DSNP|FHD|UHD)"
PROVIDER_PREFIX = re.compile(
    rf"^\s*(?:[┃|│\[(]\s*[^┃|│\])]{{1,12}}?\s*[┃|│\])]\s*|{TAG}\s*[-–|:•]\s+)+"
)


def clean_title(title: str) -> str:
    """The title without provider tags or a trailing (year)."""
    cleaned = PROVIDER_PREFIX.sub("", title)
    cleaned = re.sub(r"\s*\(\d{4}\)\s*$", "", cleaned).strip()
    return cleaned or title


def title_words(title: str) -> list[str]:
    return words(re.sub(r"\(\d{4}\)", " ", clean_title(title)))


def episode_range(title: str, season: int) -> tuple[int, int] | None:
    """(first, last) episode of `season` named in a release title (S01E05, S01E05-E06...)."""
    m = re.search(rf"\bs0*{season}e(\d{{1,4}})(?:-?e(\d{{1,4}})|-(\d{{1,4}})\b)?", fold(title))
    if not m:
        return None
    first = int(m[1])
    return first, int(m[2] or m[3] or first)


def is_season_pack(title: str, season: int) -> bool:
    t = fold(title)
    if re.search(r"\bs\d+e\d+", t):
        return False
    return bool(re.search(rf"\bs0*{season}(?!\d)|\b(?:saison|season)[ ._]*0*{season}(?!\d)", t))


def _close(size: int | None, local: int) -> bool:
    return bool(size) and local > 0 and abs(size - local) <= TOLERANCE * local


def classify_result(
    result: dict,
    release: sqlite3.Row,
    sizes: dict[int | None, int],
    titles: list[str] | None = None,
    years: list[str] | None = None,
):
    """(scope, is_candidate) for a search result, or None if it is not this season / movie.
    `sizes` maps local episode number (None for a movie) → size; `titles` are the names the
    title may carry (catalogue, original, English), all words of one of them must match."""
    found = set(words(result["title"]))
    wanted = [set(title_words(t)) for t in titles or [release["title"]]]
    if not any(w and w <= found for w in wanted):  # an empty set would match everything
        return None
    size = result.get("size") if isinstance(result.get("size"), int) else None
    if release["kind"] == "movie":
        # Providers' years are sometimes wrong: TMDB's year is accepted too.
        years = [y for y in years or [release_year(release)] if y]
        if years and not any(re.search(rf"(?<!\d){y}(?!\d)", result["title"]) for y in years):
            return None
        return "movie", _close(size, sum(sizes.values()))
    season = release["season"]
    if rng := episode_range(result["title"], season):
        eps = range(rng[0], rng[1] + 1)
        scope = f"S{season:02d}E{rng[0]:02d}" + (f"-E{rng[1]:02d}" if rng[1] != rng[0] else "")
        present = all(e in sizes for e in eps)
        return scope, present and _close(size, sum(sizes.get(e, 0) for e in eps))
    if is_season_pack(result["title"], season):
        return "pack", _close(size, sum(sizes.values()))
    return None


def release_year(release: sqlite3.Row) -> str | None:
    return release["year"] or (re.search(r"\((\d{4})\)", release["title"]) or [None, None])[1]


def search_titles(ctx: Ctx) -> tuple[list[str], list[str]]:
    """Names to search: the catalogue title, plus TMDB's original and English titles when a
    TMDB key is set (catalogues are often localised, releases often use the original title),
    and the accepted years (the provider's, plus TMDB's)."""
    from vodarr import metadata  # noqa: PLC0415 - metadata imports settings, which imports us

    release = ctx.release
    titles = [release["title"]]
    try:
        tmdb_id = release["tmdb_id"]
        year = release_year(release)
        meta = metadata.lookup(
            ctx.conn,
            ctx.config_dir,
            release["kind"],
            tmdb_id,
            clean_title(release["title"]),
            year,
            "en",
        )
    except MsgError:
        meta = None  # TMDB down or refusing: search with the catalogue title alone
    for extra in (meta or {}).get("original_title"), (meta or {}).get("title"):
        if extra and set(title_words(extra)) not in [set(title_words(t)) for t in titles]:
            titles.append(extra)
    years = [y for y in (release_year(release), (meta or {}).get("year")) if y]
    return titles, list(dict.fromkeys(years))


def prowlarr(conn: sqlite3.Connection) -> services.Prowlarr | None:
    cfg = (get_setting(conn, "services", {}) or {}).get("prowlarr") or {}
    if not cfg.get("url") or not cfg.get("apiKey"):
        return None
    return services.Prowlarr(cfg["url"], cfg["apiKey"])


def stage_search(ctx: Ctx) -> Msg:
    client = prowlarr(ctx.conn)
    if client is None:
        raise Skip("search.no_prowlarr")
    release = ctx.release
    sizes = {r["episode"]: r["size"] or 0 for r in release_files(ctx.conn, release)}
    titles, years = search_titles(ctx)
    titles = [t for t in titles if title_words(t)]
    if not titles:  # e.g. a Cyrillic title without TMDB: an empty query returns everything
        raise Skip("search.no_title")
    results, seen = [], set()
    for title in titles:  # catalogue title, then original / English title from TMDB
        for r in client.search(" ".join(title_words(title))):
            if r["downloadUrl"] not in seen:
                seen.add(r["downloadUrl"])
                results.append(r)
    kept = 0
    trackers = set()
    with locked(ctx.conn):
        # References already linked or added to qBittorrent stay: deletion needs them.
        stale = "WHERE release_id = ? AND link_root IS NULL AND added_to_qbit = 0"
        for (path,) in ctx.conn.execute(
            f"SELECT torrent_path FROM candidates {stale} AND torrent_path IS NOT NULL",  # noqa: S608
            (release["id"],),
        ).fetchall():  # their .torrent files may carry a passkey: not left behind
            with contextlib.suppress(MsgError, OSError):
                _remove_tree(Path(path), [ctx.config_dir / "torrents"])
        ctx.conn.execute(f"DELETE FROM candidates {stale}", (release["id"],))  # noqa: S608
        known = {
            r[0]
            for r in ctx.conn.execute(
                "SELECT download_url FROM candidates WHERE release_id = ?", (release["id"],)
            )
        }
        for r in results:
            found = classify_result(r, release, sizes, titles, years)
            if not found or r["downloadUrl"] in known:
                continue
            scope, is_candidate = found
            _insert_candidate(ctx.conn, release["id"], r, scope, is_candidate)
            if is_candidate:
                kept += 1
                trackers.add(str(r.get("indexer") or "?"))
    ctx.progress(1, 1)
    return Msg(
        "search.done", results=len(results), candidates=kept, trackers=", ".join(sorted(trackers))
    )


def _insert_candidate(conn, release_id: str, r: dict, scope: str, is_candidate: bool) -> None:
    conn.execute(
        "INSERT INTO candidates (release_id, indexer, title, size, scope, is_candidate,"
        " download_url, info_url, seeders) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            release_id,
            str(r.get("indexer") or "?"),
            r["title"],
            r.get("size") if isinstance(r.get("size"), int) else None,
            scope,
            int(is_candidate),
            r["downloadUrl"],
            r.get("infoUrl") if isinstance(r.get("infoUrl"), str) else None,
            r.get("seeders") if isinstance(r.get("seeders"), int) else None,
        ),
    )


# --- verify ----------------------------------------------------------------------------------

IDENTICAL = ("identical", "identical_annex")


def _local_size(scope: str, sizes: dict[int | None, int]) -> int:
    rng = episode_range(scope, int(scope[1:3])) if scope.startswith("S") else None
    if rng:
        return sum(sizes.get(e, 0) for e in range(rng[0], rng[1] + 1))
    return sum(sizes.values())


def stage_verify(ctx: Ctx) -> Msg:
    """Per tracker and scope, closest size first, stop at the first identical (AC-VER-01)."""
    release, conn = ctx.release, ctx.conn
    client = prowlarr(conn)
    if client is None:
        raise Skip("search.no_prowlarr")
    rows = conn.execute(
        "SELECT * FROM candidates WHERE release_id = ? AND is_candidate = 1 ORDER BY id",
        (release["id"],),
    ).fetchall()
    if not rows:
        raise Skip("verify.no_candidates")
    files = release_files(conn, release)
    local = [file_path(release, f) for f in files]
    sizes = {f["episode"]: f["size"] or 0 for f in files}
    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault((row["indexer"], row["scope"]), []).append(row)
    conn.execute(
        "UPDATE candidates SET status = 'pending', problem_code = NULL, problem_params = NULL,"
        " file_report = NULL"
        " WHERE release_id = ? AND link_root IS NULL AND added_to_qbit = 0",
        (release["id"],),
    )
    in_place = {r["id"] for r in rows if r["link_root"] or r["added_to_qbit"]}
    # Each .torrent download may count as a snatch on a private tracker: optional cap per tracker.
    cfg = (get_setting(conn, "services", {}) or {}).get("prowlarr") or {}
    limit = cfg.get("maxChecksPerTracker") or 0
    fetched: dict[str, int] = {}
    done = 0
    for (indexer, scope), group in groups.items():
        target = _local_size(scope, sizes)
        group.sort(key=lambda r: abs((r["size"] or 0) - target))
        found = any(r["id"] in in_place and r["status"] in IDENTICAL for r in group)
        for row in group:
            done += 1
            if row["id"] in in_place:
                continue
            if found:
                _candidate(conn, row["id"], "skipped", "verify.after_identical")
                continue
            if limit and fetched.get(indexer, 0) >= limit:
                _candidate(conn, row["id"], "skipped", "verify.tracker_limit", {"limit": limit})
                continue
            fetched[indexer] = fetched.get(indexer, 0) + 1
            status = _verify_candidate(ctx, client, row, local)
            found = status in IDENTICAL
            ctx.progress(done, len(rows))
    counts = {
        r["status"]: r["n"]
        for r in conn.execute(
            "SELECT status, count(*) AS n FROM candidates WHERE release_id = ? GROUP BY status",
            (release["id"],),
        )
    }
    identical = counts.get("identical", 0) + counts.get("identical_annex", 0)
    return Msg(
        "verify.done",
        identical=identical,
        partial=counts.get("partial", 0),
        checked=sum(counts.get(k, 0) for k in (*IDENTICAL, "partial", "different")),
    )


def _candidate(
    conn, cid: int, status: str, problem: str | None = None, params: dict | None = None, **fields
) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        "UPDATE candidates SET status = ?, problem_code = ?, problem_params = ?"  # noqa: S608
        f"{', ' + sets if sets else ''} WHERE id = ?",
        (status, problem, json.dumps(params) if params else None, *fields.values(), cid),
    )


def _verify_candidate(ctx: Ctx, client, row: sqlite3.Row, local: list[Path]) -> str:
    path = ctx.config_dir / "torrents" / f"{ctx.release['id']}-{row['id']}.torrent"
    try:
        data = client.fetch_torrent(row["download_url"])
        t = torrent.load_torrent(data)
        atomic_write(path, data)
        report, verdict = torrent.verify(t, torrent.match(t.files, local))
    except MsgError as e:
        if e.msg.code == "verify.rate_limited":
            raise  # AC-SRCH-05: stop cleanly, no relaunch loop
        _candidate(ctx.conn, row["id"], "error", e.msg.code, e.msg.params)
        return "error"
    _candidate(
        ctx.conn,
        row["id"],
        verdict,
        None,
        file_report=json.dumps(report),
        torrent_path=str(path),
        infohash=t.infohash,
    )
    return verdict


# --- restore ---------------------------------------------------------------------------------

CROSSSEED_DEFAULTS = {
    "linkDirs": [],
    "linkType": "hardlink",
    "flatLinking": False,
    "linkCategory": "cross-seed-link",
}


def crossseed_settings(conn: sqlite3.Connection) -> dict:
    return {**CROSSSEED_DEFAULTS, **(get_setting(conn, "crossseed", {}) or {})}


def references(conn: sqlite3.Connection, release_id: str) -> list[sqlite3.Row]:
    """Identical candidates, plus partial ones the user chose to complete from (AC-RES-06)."""
    return conn.execute(
        "SELECT * FROM candidates WHERE release_id = ? AND (status IN ('identical',"
        " 'identical_annex') OR (status = 'partial' AND complete_requested = 1)) ORDER BY id",
        (release_id,),
    ).fetchall()


def stage_restore(ctx: Ctx) -> Msg:
    release, conn = ctx.release, ctx.conn
    refs = references(conn, release["id"])
    if not refs:
        partial = conn.execute(
            "SELECT indexer FROM candidates WHERE release_id = ? AND status = 'partial'"
            " ORDER BY id",
            (release["id"],),
        ).fetchall()
        if partial:
            raise Skip(
                "restore.partial_available",
                trackers=", ".join(sorted({r["indexer"] for r in partial})),
            )
        raise Skip("restore.no_reference")  # downloaded files left untouched (AC-RES-01)
    cfg = crossseed_settings(conn)
    if not cfg["linkDirs"]:
        raise Skip("restore.no_linkdir_configured")
    folder = Path(release["dir"])
    link_dir = links.pick_link_dir([Path(d) for d in cfg["linkDirs"]], folder, cfg["linkType"])
    linked = 0
    for i, ref in enumerate(refs, start=1):
        t = torrent.load_torrent(Path(ref["torrent_path"]).read_bytes())
        report = json.loads(ref["file_report"])
        result = links.restore(
            t, report, folder, link_dir, ref["indexer"], bool(cfg["flatLinking"]), cfg["linkType"]
        )
        conn.execute(
            "UPDATE candidates SET link_root = ? WHERE id = ?", (str(result.root), ref["id"])
        )
        linked += result.linked + result.reused
        ctx.progress(i, len(refs))
    return Msg("restore.done", references=len(refs), files=linked)


# --- seed ------------------------------------------------------------------------------------

CHECKING = (
    "checkingUP",
    "checkingDL",
    "checkingResumeData",
    "moving",
    "metaDL",
    "queuedDL",
    "queuedUP",
    "allocating",
)
FAILED = ("error", "missingFiles")
STOPPED = ("pausedDL", "stoppedDL")
MISSING_POLLS = 3
SEED_POLL = 5.0
# Known limit: one pipeline worker waits here; annexes on a dead torrent hold it up to this long.
SEED_TIMEOUT = 6 * 3600


def qbittorrent(conn: sqlite3.Connection) -> services.QBittorrent | None:
    cfg = (get_setting(conn, "services", {}) or {}).get("qbittorrent") or {}
    if not cfg.get("url"):
        return None
    return services.QBittorrent(cfg["url"], cfg.get("username", ""), cfg.get("password", ""))


def qbit_path(conn: sqlite3.Connection, path: Path) -> str:
    """The same path as qBittorrent's container sees it (paths.dataRoot → paths.qbitDataRoot)."""
    paths = get_setting(conn, "paths", {}) or {}
    ours, theirs = paths.get("dataRoot") or "/data", paths.get("qbitDataRoot") or "/data"
    if path.is_relative_to(ours):
        return str(Path(theirs) / path.relative_to(ours))
    return str(path)


def stage_seed(ctx: Ctx) -> Msg | None:
    conn = ctx.conn
    client = qbittorrent(conn)
    if client is None:
        raise Skip("seed.no_qbittorrent")
    refs = conn.execute(
        "SELECT * FROM candidates WHERE release_id = ? AND link_root IS NOT NULL ORDER BY id",
        (ctx.release["id"],),
    ).fetchall()
    if not refs:
        raise Skip("seed.nothing")
    category = crossseed_settings(conn)["linkCategory"]
    client.ensure_category(category)
    # One infohash may come from several trackers (same torrent cross-posted): add it once.
    unique = {r["infohash"]: r for r in refs}
    hashes = {
        h: ", ".join(sorted({r["indexer"] for r in refs if r["infohash"] == h})) for h in unique
    }
    present = {t.get("hash") for t in client.info(list(hashes))}
    for h, ref in unique.items():
        if h in present:
            continue  # already in qBittorrent: followed, not added again (AC-SEED-03)
        client.add(
            Path(ref["torrent_path"]).read_bytes(),
            qbit_path(conn, Path(ref["link_root"]).parent),
            category,
        )
        conn.execute(
            "UPDATE candidates SET added_to_qbit = 1 WHERE release_id = ? AND infohash = ?",
            (ctx.release["id"], h),
        )
    deadline = time.monotonic() + SEED_TIMEOUT
    missing = 0
    stage = "WHERE release_id = ? AND name = 'seed' AND state = 'running'"
    # From here on we only watch: retry, delete and "complete" may interrupt (ensure_idle).
    watched = conn.execute(
        f"UPDATE stages SET msg_code = 'seed.waiting' {stage}",  # noqa: S608
        (ctx.release["id"],),
    ).rowcount
    while True:
        if (
            watched
            and not conn.execute(
                f"SELECT 1 FROM stages {stage}",  # noqa: S608
                (ctx.release["id"],),
            ).fetchone()
        ):
            return None  # reset or deleted meanwhile: nothing to report
        states = {t.get("hash"): t for t in client.info(list(hashes))}
        missing = missing + 1 if len(states) < len(hashes) else 0
        if missing >= MISSING_POLLS:
            gone = next(h for h in hashes if h not in states)
            raise MsgError("seed.torrent_missing", tracker=hashes[gone])
        for h, t in states.items():
            progress = float(t.get("progress") or 0)
            if t.get("state") in FAILED:
                raise MsgError("seed.torrent_failed", state=t["state"], tracker=hashes[h])
            if t.get("state") in STOPPED and progress < 1:
                raise MsgError("seed.torrent_stopped", tracker=hashes[h])
        if len(states) == len(hashes):
            ctx.progress(sum(float(t.get("progress") or 0) for t in states.values()), len(hashes))
            if all(
                float(t.get("progress") or 0) >= 1 and t.get("state") not in CHECKING
                for t in states.values()
            ):
                break
        if time.monotonic() > deadline:
            raise MsgError("seed.timeout")
        time.sleep(SEED_POLL)
    return Msg("seed.done", trackers=", ".join(sorted(set(hashes.values()))))


# --- deletion (AC-PIPE-05) -------------------------------------------------------------------


def _inside(path: Path, roots: list[Path]) -> bool:
    """Strictly under one of `roots`, compared on normalised absolute paths (no '..')."""
    norm = Path(os.path.normpath(path))
    return (
        norm.is_absolute()
        and ".." not in path.parts
        and any(
            norm.is_relative_to(Path(os.path.normpath(r))) and norm != Path(os.path.normpath(r))
            for r in roots
        )
    )


def _remove_tree(path: Path, roots: list[Path]) -> None:
    """Remove `path` (file, link or directory), then its parents while empty, inside `roots`."""
    if not _inside(path, roots):
        raise MsgError("release.unsafe_path")
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)
    parent = path.parent
    while _inside(parent, roots):
        try:
            parent.rmdir()  # only if empty
        except OSError:
            break
        parent = parent.parent


def _remove_links(ref: sqlite3.Row, link_dirs: list[Path]) -> None:
    """Remove only the files this reference's torrent lists under its link root, then the
    folders left empty: a folder that existed before (same name) keeps its other content."""
    root = Path(ref["link_root"])
    try:
        t = torrent.load_torrent(Path(ref["torrent_path"]).read_bytes())
        targets = [root / f.path for f in t.files] if t.multi else [root]
    except (OSError, TypeError, MsgError):
        targets = []  # torrent gone: only empty folders are removed
    for target in targets:
        if not _inside(target, link_dirs):
            raise MsgError("release.unsafe_path")
        if target.is_symlink() or target.is_file():
            target.unlink()
    if root.is_dir() and not root.is_symlink():
        for folder, _, _ in sorted(os.walk(root), key=lambda w: -len(w[0])):
            with contextlib.suppress(OSError):
                Path(folder).rmdir()
    parent = root.parent
    while _inside(parent, link_dirs):
        try:
            parent.rmdir()  # only if empty
        except OSError:
            break
        parent = parent.parent


def delete_release(
    conn: sqlite3.Connection, config_dir: Path, release_id: str, delete_files: bool = False
) -> None:
    """Remove everything VODarr created for this release; downloaded files only if asked."""
    link_dirs = [Path(d) for d in crossseed_settings(conn)["linkDirs"]]
    torrents_dir = config_dir / "torrents"
    with locked(conn):
        ensure_idle(conn, release_id)
        release = conn.execute("SELECT * FROM releases WHERE id = ?", (release_id,)).fetchone()
        folder = Path(release["dir"])
        refs = conn.execute(
            "SELECT * FROM candidates WHERE release_id = ?", (release_id,)
        ).fetchall()
        names = [
            r[0]
            for r in conn.execute("SELECT filename FROM files WHERE release_id = ?", (release_id,))
        ]
        # Every path is checked before anything is touched.
        for ref in refs:  # links outside today's link folders are left alone (see below)
            if ref["torrent_path"] and not _inside(Path(ref["torrent_path"]), [torrents_dir]):
                raise MsgError("release.unsafe_path")
        if delete_files and not all(_inside(folder / n, [folder]) for n in names):
            raise MsgError("release.unsafe_path")
        ours = {r["infohash"] for r in refs if r["added_to_qbit"] and r["infohash"]}
        shared = {
            r[0]
            for r in conn.execute(
                "SELECT infohash FROM candidates WHERE release_id != ? AND infohash IS NOT NULL",
                (release_id,),
            )
        }
        ours -= shared  # still seeded for another release: leave it
        # Links outside today's link folders (the setting changed, or the record is wrong) are
        # never deleted: those torrents leave qBittorrent without their files.
        keep_files = {
            r["infohash"]
            for r in refs
            if r["link_root"] and not _inside(Path(r["link_root"]), link_dirs)
        }
        client = qbittorrent(conn) if ours else None
        if ours and client is None:
            raise MsgError("release.qbit_unconfigured")
        if client is not None:  # unreachable: refuse now, before anything is parked or removed
            client.timeout = 10.0
            client.info(sorted(ours))
        # Park the pipeline so the worker cannot pick the release up while we clean it.
        conn.execute(
            "UPDATE stages SET state = 'error', msg_code = 'release.deleting'"
            " WHERE release_id = ? AND name != 'download'",
            (release_id,),
        )
        running = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM files WHERE release_id = ? AND dl_status = 'running'",
                (release_id,),
            )
        ]
        conn.execute(
            "UPDATE files SET dl_status = 'cancelled' WHERE release_id = ?"
            " AND dl_status IN ('queued', 'running')",
            (release_id,),
        )
    _wait_downloads_stopped(running)
    if client is not None:
        # deleteFiles removes only that torrent's files: our links and what qBittorrent completed.
        client.delete(sorted(ours - keep_files), delete_files=True)
        client.delete(sorted(ours & keep_files), delete_files=False)
    for ref in refs:
        if ref["link_root"] and _inside(Path(ref["link_root"]), link_dirs):
            _remove_links(ref, link_dirs)
        elif ref["link_root"]:  # link folder changed since: never delete outside the current ones
            log.warning("links of release %s left in place: outside the link folders", release_id)
        if ref["torrent_path"]:
            _remove_tree(Path(ref["torrent_path"]), [torrents_dir])
    if delete_files:
        for name in names:
            for target in (folder / name, folder / (name + ".part")):
                _remove_tree(target, [folder])
        for empty in (folder, folder.parent):  # season folder, then the title folder if empty
            with contextlib.suppress(OSError):
                empty.rmdir()
    conn.execute("DELETE FROM releases WHERE id = ?", (release_id,))


def _wait_downloads_stopped(file_ids: list[int], timeout: float = 10.0) -> None:
    """The download worker notices a cancel within a second; wait so no .part is rewritten."""
    from vodarr import downloads  # noqa: PLC0415 - avoid an import cycle at load time

    deadline = time.monotonic() + timeout
    while downloads.ACTIVE & set(file_ids) and time.monotonic() < deadline:
        time.sleep(0.2)
