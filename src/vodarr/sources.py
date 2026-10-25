"""VOD sources: Xtream Codes accounts and M3U playlists, with a 24 h on-disk catalogue cache."""

import functools
import hashlib
import hmac
import http.client
import json
import re
import shutil
import sqlite3
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vodarr.core import MsgError, atomic_write, register_secret

CACHE_TTL = 86400
VIDEO_EXTS = ("mkv", "mp4", "avi", "m4v", "mov", "wmv", "ts", "webm")
RELEASE = re.compile(r"^[\w.()+'&-]+(\.[\w()+'&-]+){3,}-\w+$")  # Name.Of.Title.720p.WEB-DL-TEAM
USER_AGENT = "VLC/3.0.20"  # some providers reject unknown clients
M3U_LIMIT = 50 * 1024 * 1024
EPISODE = re.compile(r"[Ss](\d{1,3})\s*[Ee](\d{1,4})")
EXTINF = re.compile(
    r'#EXTINF:\s*-?\d*((?:\s+[\w-]+="[^"]*")*)\s*,(.*)'
)  # quoted attrs may hold commas
ATTR = re.compile(r'([\w-]+)="([^"]*)"')


@dataclass
class Source:
    id: str
    name: str
    type: str  # xtream | m3u
    config: dict[str, Any] = field(repr=False)
    split_prefix: bool = False


def add(
    conn: sqlite3.Connection, name: str, type: str, config: dict, split_prefix: bool = False
) -> Source:
    """New source with a stable random id (AC-SET-01)."""
    source = Source(uuid.uuid4().hex, name, type, config, split_prefix)
    position = conn.execute("SELECT coalesce(max(position) + 1, 0) FROM sources").fetchone()[0]
    conn.execute(
        "INSERT INTO sources (id, name, type, config, split_prefix, position) VALUES (?,?,?,?,?,?)",
        (source.id, name, type, json.dumps(config), int(split_prefix), position),
    )
    return source


def get(conn: sqlite3.Connection, source_id: str) -> Source:
    row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise MsgError("source.not_found")
    return Source(
        row["id"], row["name"], row["type"], json.loads(row["config"]), bool(row["split_prefix"])
    )


def safe_ext(ext: str | None) -> str:
    ext = (ext or "").lower()
    return ext if ext in VIDEO_EXTS else "mkv"


def safe_filename(name: str, max_bytes: int = 180) -> str:
    """A single path component: no separator, no '..', at most `max_bytes` UTF-8 bytes (room
    left for an extension, '.part' and a suffix under the usual 255-byte limit)."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)
    name = re.sub(r"\.{2,}", lambda m: "_" * len(m.group()), name)
    name = name.encode()[:max_bytes].decode("utf-8", "ignore")
    return name.strip().strip(".") or "_"


def release_name(info: dict | None) -> str | None:
    """Original release name, often left by providers in the track tags."""
    for kind in ("video", "audio"):
        title = (((info or {}).get(kind) or {}).get("tags") or {}).get("title")
        if isinstance(title, str) and RELEASE.match(title):
            return title
    return None


def tmdb_id(value: object) -> str | None:
    """Providers send TMDB ids as numbers or strings, sometimes empty or "0"."""
    text = str(value or "").strip()
    return text if text.isdigit() and text != "0" else None


def fold(text: str) -> str:
    """Case- and accent-insensitive form used by the catalogue search."""
    text = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in text if not unicodedata.combining(c))


def search(items: list[dict], query: str) -> list[dict]:
    words = fold(query).split()
    return [it for it in items if all(w in fold(it["name"]) for w in words)]


XTREAM_LIMIT = 200 * 1024 * 1024
METADATA_HOSTS = {"metadata.google.internal", "metadata", "fd00:ec2::254"}
# Known limit: providers may redirect anywhere on the LAN (blind GET); only cloud metadata is
# refused, since the *arr hosts are legitimate targets. Add an allowlist if that matters.


class _HttpOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if target.scheme not in ("http", "https"):
            raise urllib.error.URLError("redirect to a non-http scheme")
        if (target.hostname or "").startswith("169.254.") or target.hostname in METADATA_HOSTS:
            raise urllib.error.URLError("redirect to a link-local address")
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and _origin(newurl) != _origin(req.full_url):
            for name in ("X-api-key", "Cookie", "Authorization"):  # never follow another host
                new.remove_header(name)
        return new


def _origin(url: str) -> tuple[str, str]:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme, parts.netloc.lower()


_opener = urllib.request.build_opener(_HttpOnlyRedirects)


def open_url(
    url: str, timeout: float = 300, headers: dict[str, str] | None = None, data: bytes | None = None
):
    """GET (or POST with `data`) over http(s) only, redirects included."""
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise MsgError("source.bad_url")
    headers = {"User-Agent": USER_AGENT, **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=headers)  # noqa: S310 - scheme checked
    return _opener.open(req, timeout=timeout)


def _http_get(url: str, limit: int, timeout: float = 300) -> bytes:
    """Body of `url`, at most `limit` + 1 bytes. Errors carry no URL (it holds credentials)."""
    try:
        with open_url(url, timeout) as r:
            return r.read(limit + 1)
    except urllib.error.HTTPError as e:
        code = "source.auth_failed" if e.code in (401, 403) else "source.http_error"
        raise MsgError(code, status=e.code) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError):
        # ValueError covers http.client.InvalidURL, whose message contains the URL.
        raise MsgError("source.unreachable") from None


def _untrusted(method):
    """Provider JSON is untrusted: any shape error becomes an invalid-response message."""

    @functools.wraps(method)
    def wrapper(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except (KeyError, TypeError, ValueError, AttributeError, IndexError):
            raise MsgError("source.invalid_response") from None

    return wrapper


class Catalogue:
    """Shared cache and category helpers; subclasses implement `_fetch` and the detail calls."""

    def __init__(self, source: Source, config_dir: Path):
        self.source = source
        self.config_dir = config_dir
        self.cache_dir = config_dir / "cache" / source.id

    def refresh(self) -> None:
        shutil.rmtree(self.cache_dir, ignore_errors=True)

    def _cached(self, action: str, fetch, valid) -> Any:
        path = self.cache_dir / f"{action}.json"
        if path.exists() and time.time() - path.stat().st_mtime < CACHE_TTL:
            try:
                return json.loads(path.read_text())
            except ValueError:
                pass  # corrupt cache: fetch again
        data = fetch()
        if not valid(data):
            raise MsgError("source.invalid_response")
        atomic_write(path, json.dumps(data).encode())
        return data

    def _category(self, cid: str, name: str) -> dict:
        provider, label = self.source.name, name
        if self.source.split_prefix and "|" in name:
            provider, label = (part.strip() for part in name.split("|", 1))
        return {"id": cid, "name": name, "provider": provider, "label": label}

    def test(self):
        """None if the source answers (bypassing the cache), else the error message."""
        try:
            self._probe()
        except MsgError as e:
            return e.msg
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _seasons(episodes: Any) -> dict[int, list[tuple[int, dict]]]:
    """season → [(episode number, episode)]. Panels send a dict keyed by season, or a list of
    per-season lists; an episode without a usable id or number is left out, not fatal."""
    if isinstance(episodes, dict):
        pairs = [(_int(k), eps) for k, eps in episodes.items()]
    else:
        pairs = [(None, eps) for eps in episodes or []]
    out: dict[int, list[tuple[int, dict]]] = {}
    for key, eps in pairs:
        for e in eps if isinstance(eps, list) else []:
            if not isinstance(e, dict) or e.get("id") is None:
                continue
            season = key if key is not None else _int(e.get("season"))
            num = _int(e.get("episode_num"))
            if season is not None and num is not None:
                out.setdefault(season, []).append((num, e))
    return out


class Xtream(Catalogue):
    def __init__(self, source: Source, config_dir: Path):
        super().__init__(source, config_dir)
        cfg = source.config
        self.base = cfg["server"].rstrip("/")
        self.user, self.password = cfg["user"], cfg["password"]
        register_secret(self.password)

    def _api(self, action: str | None = None, **params: str) -> Any:
        query = {"username": self.user, "password": self.password, **params}
        if action:
            query["action"] = action
        url = f"{self.base}/player_api.php?{urllib.parse.urlencode(query)}"
        raw = _http_get(url, XTREAM_LIMIT)
        if len(raw) > XTREAM_LIMIT:
            raise MsgError("source.invalid_response")
        try:
            data = json.loads(raw)
        except ValueError:
            raise MsgError("source.invalid_response") from None
        user_info = data.get("user_info") if isinstance(data, dict) else None
        if isinstance(user_info, dict) and str(user_info.get("auth")) == "0":
            raise MsgError("source.auth_failed")
        return data

    def _probe(self) -> None:
        self._api()

    def _list(self, action: str) -> list:
        return self._cached(action, lambda: self._api(action), lambda d: isinstance(d, list))

    @_untrusted
    def categories(self, kind: str) -> list[dict]:
        action = "get_series_categories" if kind == "series" else "get_vod_categories"
        return [
            self._category(str(c["category_id"]), str(c["category_name"]))
            for c in self._list(action)
            if isinstance(c, dict)
        ]

    @_untrusted
    def items(self, kind: str) -> list[dict]:
        if kind == "series":
            raw, id_key, cover_key = self._list("get_series"), "series_id", "cover"
        else:
            raw, id_key, cover_key = self._list("get_vod_streams"), "stream_id", "stream_icon"
        return [
            {
                "id": str(it[id_key]),
                "name": str(it.get("name") or ""),
                "category_id": str(it.get("category_id") or ""),
                "cover": it.get(cover_key) or None,
                "year": str(it.get("releaseDate") or it.get("year") or "")[:4],
            }
            for it in raw
            if isinstance(it, dict) and it.get(id_key) is not None
        ]

    @_untrusted
    def series(self, item_id: str) -> dict:
        data = self._api("get_series_info", series_id=item_id)
        if not isinstance(data, dict) or not data.get("info"):
            raise MsgError("source.not_found")
        info, seasons = data["info"], []
        for number, eps in sorted(_seasons(data.get("episodes")).items()):
            seasons.append(
                {
                    "season": number,
                    "episodes": [
                        {
                            "id": str(e["id"]),
                            "num": num,
                            "title": str(e.get("title") or ""),
                            "ext": safe_ext(e.get("container_extension")),
                            "release": release_name(e.get("info")),
                        }
                        for num, e in sorted(eps, key=lambda pair: pair[0])
                    ],
                }
            )
        return {
            "name": info.get("name"),
            "plot": info.get("plot"),
            "cover": info.get("cover") or None,
            "year": str(info.get("releaseDate") or "")[:4],
            "tmdb_id": tmdb_id(info.get("tmdb") or info.get("tmdb_id")),
            "seasons": seasons,
        }

    @_untrusted
    def movie(self, item_id: str) -> dict:
        data = self._api("get_vod_info", vod_id=item_id)
        if not isinstance(data, dict) or not data.get("movie_data"):
            raise MsgError("source.not_found")
        info, md = data.get("info") or {}, data["movie_data"]
        return {
            "name": md.get("name"),
            "plot": info.get("plot") or info.get("description"),
            "cover": info.get("movie_image") or None,
            "year": str(info.get("releasedate") or "")[:4],
            "ext": safe_ext(md.get("container_extension")),
            "release": release_name(info),
            "tmdb_id": tmdb_id(info.get("tmdb_id") or info.get("tmdb")),
        }

    def stream_url(self, kind: str, stream_id: str, ext: str) -> str:
        """Built server-side only: it carries the account credentials (AC-SRC-08)."""

        def q(value: str) -> str:
            return urllib.parse.quote(str(value), safe="")

        path = "series" if kind == "series" else "movie"
        return (
            f"{self.base}/{path}/{q(self.user)}/{q(self.password)}/{q(stream_id)}.{safe_ext(ext)}"
        )


def parse_m3u(text: str) -> list[tuple[dict[str, str], str, str]]:
    """[(attributes, title, url)] for every playlist entry."""
    out, current = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            m = EXTINF.match(line)
            current = (dict(ATTR.findall(m.group(1))), m.group(2).strip()) if m else None
        elif line and not line.startswith("#") and current:
            out.append((*current, line))
            current = None
    return out


def classify(name: str, url: str) -> str | None:
    """'series' | 'movie' | None (live) from the Xtream-style path, the extension and SxxExx."""
    path = urllib.parse.urlsplit(url).path.lower()
    last = path.rsplit("/", 1)[-1]
    ext = last.rsplit(".", 1)[-1] if "." in last else ""
    if "/live/" in path:
        return None
    if "/series/" in path or (ext in VIDEO_EXTS and EPISODE.search(name)):
        return "series"
    if "/movie/" in path or (ext in VIDEO_EXTS and ext != "ts"):
        return "movie"
    return None


def series_key(name: str) -> str:
    m = EPISODE.search(name)
    return re.sub(r"[\s._\-|]+$", "", name[: m.start()] if m else name).strip()


class M3U(Catalogue):
    def __init__(self, source: Source, config_dir: Path):
        super().__init__(source, config_dir)
        self.url, self.path = source.config.get("url"), source.config.get("path")
        register_secret(self.url)
        # Ids are keyed by the playlist location (secret for URLs): stable, reveal no stream URL.
        self._key = (self.url or self.path or "").encode()

    def _id(self, value: str) -> str:
        return hmac.new(self._key, value.encode(), hashlib.sha256).hexdigest()[:16]

    def _read(self) -> str:
        if self.url:
            if urllib.parse.urlsplit(self.url).scheme not in ("http", "https"):
                raise MsgError("source.bad_url")
            raw = _http_get(self.url, M3U_LIMIT)
        else:
            root = (self.config_dir / "playlists").resolve()
            try:
                path = (root / (self.path or "")).resolve()
            except (ValueError, OSError):
                raise MsgError("source.bad_url") from None
            if not self.path or not path.is_relative_to(root) or not path.is_file():
                raise MsgError("source.bad_url")
            with path.open("rb") as f:
                raw = f.read(M3U_LIMIT + 1)
        if len(raw) > M3U_LIMIT:
            raise MsgError("source.playlist_too_large", limitMb=M3U_LIMIT // (1024 * 1024))
        return raw.decode("utf-8-sig", "replace")  # some editors start the file with a BOM

    def _probe(self) -> None:
        if not self._read().lstrip().startswith("#EXTM3U"):
            raise MsgError("source.invalid_response")

    def _parse(self) -> dict:
        text = self._read()
        if not text.lstrip().startswith("#EXTM3U"):
            return {}
        cats: dict = {"series": {}, "movie": {}}
        items: dict = {"series": {}, "movie": {}}
        for attrs, title, url in parse_m3u(text):
            name = attrs.get("tvg-name") or title
            kind = classify(name, url)
            if not kind:
                continue
            group = attrs.get("group-title") or "—"
            cid = self._id("group|" + group)
            cats[kind][cid] = group
            last = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
            ext = safe_ext(last.rsplit(".", 1)[-1] if "." in last else "")
            year = (re.search(r"\((\d{4})\)", name) or [None, ""])[1]
            cover = attrs.get("tvg-logo") or None
            if kind == "movie":
                mid = self._id(url)
                items["movie"][mid] = {
                    "id": mid,
                    "name": name,
                    "category_id": cid,
                    "cover": cover,
                    "year": year,
                    "ext": ext,
                    "url": url,
                }
                continue
            key = series_key(name)
            sid = self._id(f"series|{group}|{key}")
            entry = items["series"].setdefault(
                sid,
                {
                    "id": sid,
                    "name": key,
                    "category_id": cid,
                    "cover": cover,
                    "year": year,
                    "episodes": [],
                },
            )
            m = EPISODE.search(name)
            season, num = (int(m[1]), int(m[2])) if m else (1, len(entry["episodes"]) + 1)
            entry["episodes"].append(
                {
                    "id": self._id(url),
                    "season": season,
                    "num": num,
                    "title": name,
                    "ext": ext,
                    "url": url,
                }
            )
        return {"categories": cats, "items": items}

    def _data(self) -> dict:
        return self._cached("playlist", self._parse, lambda d: bool(d))

    def categories(self, kind: str) -> list[dict]:
        return [self._category(cid, name) for cid, name in self._data()["categories"][kind].items()]

    def items(self, kind: str) -> list[dict]:
        keep = ("id", "name", "category_id", "cover", "year")
        return [{k: it[k] for k in keep} for it in self._data()["items"][kind].values()]

    def _item(self, kind: str, item_id: str) -> dict:
        try:
            return self._data()["items"][kind][item_id]
        except KeyError:
            raise MsgError("source.not_found") from None

    def series(self, item_id: str) -> dict:
        it = self._item("series", item_id)
        seasons: dict[int, list] = {}
        for e in sorted(it["episodes"], key=lambda e: (e["season"], e["num"])):
            seasons.setdefault(e["season"], []).append(
                {
                    "id": e["id"],
                    "num": e["num"],
                    "title": e["title"],
                    "ext": e["ext"],
                    "release": None,
                }
            )
        return {
            "name": it["name"],
            "plot": None,
            "cover": it["cover"],
            "year": it["year"],
            "tmdb_id": None,
            "seasons": [{"season": n, "episodes": eps} for n, eps in sorted(seasons.items())],
        }

    def movie(self, item_id: str) -> dict:
        it = self._item("movie", item_id)
        return {
            "name": it["name"],
            "plot": None,
            "cover": it["cover"],
            "year": it["year"],
            "ext": it["ext"],
            "release": None,
            "tmdb_id": None,
        }

    def stream_url(self, kind: str, stream_id: str, ext: str) -> str:
        # Known limit: the URL is looked up in the cached playlist at download time; a provider
        # that rotates tokens in entry URLs changes the ids and the queued file fails with
        # source.not_found. Store the URL in `files` at enqueue time if that shows up.
        if kind == "movie":
            return self._item("movie", stream_id)["url"]
        urls = {
            e["id"]: e["url"]
            for it in self._data()["items"]["series"].values()
            for e in it["episodes"]
        }
        if stream_id not in urls:
            raise MsgError("source.not_found")
        return urls[stream_id]


def client(source: Source, config_dir: Path) -> Catalogue:
    """Catalogue client; cache under <config_dir>/cache, M3U files under <config_dir>/playlists."""
    if source.type == "xtream":
        return Xtream(source, config_dir)
    if source.type == "m3u":
        return M3U(source, config_dir)
    raise ValueError(source.type)
