"""Clients for the *arr stack services: Prowlarr (search) and qBittorrent (seed)."""

import http.client
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import uuid
from typing import Any

from vodarr.core import MsgError, register_secret
from vodarr.sources import open_url

log = logging.getLogger(__name__)
TORRENT_LIMIT = 10 * 1024 * 1024


def _request(
    url: str,
    service: str,
    limit: int,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: float = 120,
) -> tuple[bytes, Any]:
    """Body and headers; errors become `<service>.<reason>` messages and never chain an
    exception (its text may hold the URL and its credentials)."""
    try:
        with open_url(url, timeout=timeout, headers=headers, data=data) as r:
            body, response_headers = r.read(limit + 1), r.headers
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise MsgError(f"{service}.rate_limited") from None
        if e.code in (401, 403):
            raise MsgError(f"{service}.auth_failed") from None
        raise MsgError(f"{service}.http_error", status=e.code) from None
    except MsgError:
        raise
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError):
        raise MsgError(f"{service}.unreachable") from None
    if len(body) > limit:
        raise MsgError(f"{service}.too_large")
    return body, response_headers


def _get(url: str, headers: dict[str, str], service: str, limit: int) -> bytes:
    return _request(url, service, limit, headers)[0]


class Prowlarr:
    def __init__(self, url: str, api_key: str):
        self.url, self.api_key = url.rstrip("/"), api_key
        register_secret(api_key)

    def search(self, query: str) -> list[dict]:
        """All indexers, `type=search` (AC-SRCH-01). Results keep `downloadUrl` server-side only."""
        params = urllib.parse.urlencode({"query": query, "type": "search", "limit": 1000})
        body = _get(
            f"{self.url}/api/v1/search?{params}",
            {"X-Api-Key": self.api_key},
            "search",
            50 * 1024 * 1024,
        )
        try:
            data = json.loads(body)
        except ValueError:
            raise MsgError("search.invalid_response") from None
        if not isinstance(data, list):
            raise MsgError("search.invalid_response")
        return [
            r
            for r in data
            if isinstance(r, dict)
            and r.get("protocol", "torrent") == "torrent"
            and isinstance(r.get("title"), str)
            and r.get("downloadUrl")
        ]

    def fetch_torrent(self, download_url: str) -> bytes:
        """Only the path and query of downloadUrl are kept, sent to the configured Prowlarr:
        the key never goes to another host, and a downloadUrl written with another name for
        Prowlarr (proxy, external URL, Docker DNS) still works."""
        theirs = urllib.parse.urlsplit(download_url)
        ours = urllib.parse.urlsplit(self.url)
        url = urllib.parse.urlunsplit((ours.scheme, ours.netloc, theirs.path, theirs.query, ""))
        return _get(url, {"X-Api-Key": self.api_key}, "verify", TORRENT_LIMIT)

    def test(self) -> Any:
        try:
            _get(f"{self.url}/api/v1/indexer", {"X-Api-Key": self.api_key}, "prowlarr", 10 << 20)
        except MsgError as e:
            return e.msg
        return None


class QBittorrent:
    """WebUI API v2 (qBittorrent >= 4.6). Session cookie named SID (before 5.x) or
    QBT_SID_<port> (5.x); login answers 200 "Ok." or 204. Re-login once on 403. A qBittorrent
    that skips authentication for this client (whitelisted subnet) works without a cookie."""

    COOKIE = re.compile(r"\b((?:QBT_)?SID(?:_\d+)?)=([^;,\s]+)")

    def __init__(self, url: str, username: str, password: str):
        self.url, self.username, self.password = url.rstrip("/"), username, password
        self.cookie: str | None = None  # "name=value", or "" when no session is needed
        self.timeout = 120.0  # per request; live status reads use a short one
        register_secret(password)

    def login(self) -> None:
        form = urllib.parse.urlencode({"username": self.username, "password": self.password})
        body, headers = _request(
            f"{self.url}/api/v2/auth/login",
            "seed",
            1 << 16,
            {"Content-Type": "application/x-www-form-urlencoded"},
            form.encode(),
            self.timeout,
        )
        if body.strip() == b"Fails.":
            raise MsgError("seed.auth_failed")
        found = self.COOKIE.search(", ".join(headers.get_all("Set-Cookie") or []))
        if found:
            self.cookie = f"{found.group(1)}={found.group(2)}"
            register_secret(found.group(2))
            return
        try:  # no session cookie: only fine if qBittorrent does not ask this client to log in
            _request(f"{self.url}/api/v2/app/version", "seed", 1 << 10, timeout=self.timeout)
        except MsgError:
            raise MsgError("seed.auth_failed") from None
        self.cookie = ""

    def _call(
        self,
        path: str,
        data: bytes | None = None,
        content_type: str | None = None,
        limit: int = 50 << 20,
    ) -> bytes:
        for attempt in (1, 2):
            if self.cookie is None:
                self.login()
            headers = {"Cookie": self.cookie} if self.cookie else {}
            if content_type:
                headers["Content-Type"] = content_type
            try:
                return _request(
                    f"{self.url}/api/v2/{path}", "seed", limit, headers, data, self.timeout
                )[0]
            except MsgError as e:
                if e.msg.code != "seed.auth_failed" or attempt == 2:
                    raise
                self.cookie = None  # session expired: log in again (AC-SEED-01)
        raise AssertionError("unreachable")

    def _form(self, path: str, fields: dict[str, str]) -> bytes:
        return self._call(
            path, urllib.parse.urlencode(fields).encode(), "application/x-www-form-urlencoded"
        )

    def info(self, hashes: list[str]) -> list[dict]:
        if not hashes:
            return []
        body = self._call("torrents/info?" + urllib.parse.urlencode({"hashes": "|".join(hashes)}))
        try:
            data = json.loads(body)
        except ValueError:
            raise MsgError("seed.invalid_response") from None
        return [t for t in data if isinstance(t, dict)] if isinstance(data, list) else []

    def ensure_category(self, name: str) -> None:
        try:
            self._form("torrents/createCategory", {"category": name, "savePath": ""})
        except MsgError as e:
            if e.msg.code != "seed.http_error" or e.msg.params.get("status") != 409:
                raise

    def add(self, torrent_data: bytes, savepath: str, category: str) -> None:
        """AC-SEED-02. Answers are 'Ok.' (before 5.1) or JSON counts (5.1+)."""
        boundary = uuid.uuid4().hex
        fields = {
            "savepath": savepath,
            "category": category,
            "autoTMM": "false",
            "skip_checking": "false",
            "paused": "false",
            "stopped": "false",
        }
        body = b"".join(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
            for k, v in fields.items()
        )
        body += (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="torrents"; '
                f'filename="ref.torrent"\r\nContent-Type: application/x-bittorrent\r\n\r\n'
            ).encode()
            + torrent_data
            + f"\r\n--{boundary}--\r\n".encode()
        )
        answer = self._call("torrents/add", body, f"multipart/form-data; boundary={boundary}")
        if answer.strip() == b"Ok.":
            return
        try:
            counts = json.loads(answer)
        except ValueError:
            counts = None
        if (
            not isinstance(counts, dict)
            or counts.get("failure_count", 1)
            or not (counts.get("success_count") or counts.get("added_torrent_ids"))
        ):
            raise MsgError("seed.add_failed")

    def delete(self, hashes: list[str], delete_files: bool) -> None:
        if hashes:
            self._form(
                "torrents/delete",
                {"hashes": "|".join(hashes), "deleteFiles": "true" if delete_files else "false"},
            )

    def test(self) -> Any:
        try:
            self.login()
            self._call("app/version")
        except MsgError as e:
            return e.msg
        return None


class TMDB:
    """The Movie Database: overview, poster, genres, cast. The user's own key: a v3 API key
    (sent as a query parameter) or a v4 read access token (sent as a Bearer header)."""

    BASE = "https://api.themoviedb.org/3"
    IMAGES = "https://image.tmdb.org/t/p/"

    def __init__(self, api_key: str, base: str | None = None):
        # VODARR_TMDB_URL points demos and tests at a fake TMDB.
        base = base or os.environ.get("VODARR_TMDB_URL") or self.BASE
        self.api_key, self.base = api_key.strip(), base.rstrip("/")
        register_secret(self.api_key)

    def _get(self, path: str, **params: str) -> dict:
        headers = {"Accept": "application/json"}
        if self.api_key.startswith("eyJ"):  # v4 token (JWT)
            headers["Authorization"] = f"Bearer {self.api_key}"
        else:
            params["api_key"] = self.api_key
        body = _get(f"{self.base}{path}?{urllib.parse.urlencode(params)}", headers, "tmdb", 2 << 20)
        try:
            data = json.loads(body)
        except ValueError:
            raise MsgError("tmdb.invalid_response") from None
        if not isinstance(data, dict):
            raise MsgError("tmdb.invalid_response")
        return data

    def find(self, kind: str, title: str, year: str | None, language: str) -> str | None:
        """Best TMDB id for a title (and year), or None."""
        path, year_key = (
            ("/search/tv", "first_air_date_year") if kind == "series" else ("/search/movie", "year")
        )
        params = {"query": title, "language": language}
        if year:
            params[year_key] = year
        results = self._get(path, **params).get("results") or []
        first = next((r for r in results if isinstance(r, dict) and r.get("id")), None)
        return str(first["id"]) if first else None

    def details(self, kind: str, tmdb_id: str, language: str) -> dict:
        section = "tv" if kind == "series" else "movie"
        path = f"/{section}/{urllib.parse.quote(tmdb_id, safe='')}"
        d = self._get(path, language=language, append_to_response="credits")
        date = d.get("first_air_date") or d.get("release_date") or ""

        def image(p: object, size: str) -> str | None:
            return f"{self.IMAGES}{size}{p}" if isinstance(p, str) and p.startswith("/") else None

        cast = ((d.get("credits") or {}).get("cast") or [])[:6]
        return {
            "tmdb_id": str(d.get("id") or tmdb_id),
            "title": d.get("name") or d.get("title"),
            "original_title": d.get("original_name") or d.get("original_title"),
            "overview": d.get("overview") or None,
            "tagline": d.get("tagline") or None,
            "year": date[:4] or None,
            "genres": [g.get("name") for g in d.get("genres") or [] if isinstance(g, dict)],
            "rating": d.get("vote_average"),
            "runtime": d.get("runtime") or (d.get("episode_run_time") or [None])[0],
            "seasons": d.get("number_of_seasons"),
            "poster": image(d.get("poster_path"), "w342"),
            "backdrop": image(d.get("backdrop_path"), "w780"),
            "cast": [c.get("name") for c in cast if isinstance(c, dict) and c.get("name")],
            "url": f"https://www.themoviedb.org/{section}/{d.get('id')}",
        }

    def test(self) -> Any:
        try:
            self._get("/configuration")
        except MsgError as e:
            return e.msg
        return None
