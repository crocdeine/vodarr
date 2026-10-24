"""Title details from TMDB with the user's own key, cached 7 days on disk (0.1.1)."""

import json
import sqlite3
import time
from pathlib import Path

from vodarr import services, settings
from vodarr.core import MsgError, atomic_write

TTL = 7 * 86400
LANGUAGES = {"fr": "fr-FR", "en": "en-US"}


def client(conn: sqlite3.Connection) -> services.TMDB | None:
    key = settings.load(conn)["services"]["tmdb"]["apiKey"]
    return services.TMDB(key) if key else None


def _year_off(details: dict, year: str | None) -> bool:
    """Providers sometimes attach the TMDB id of another film with the same title."""
    if not (year and details.get("year")):
        return False
    return abs(int(details["year"]) - int(year)) > 1


def _details(tmdb, config_dir: Path, kind: str, tmdb_id: str, lang: str) -> dict | None:
    path = config_dir / "cache" / "tmdb" / f"{kind}-{tmdb_id}-{lang}.json"
    if path.exists() and time.time() - path.stat().st_mtime < TTL:
        try:
            return json.loads(path.read_text())
        except ValueError:
            pass
    try:
        details = tmdb.details(kind, tmdb_id, lang)
    except MsgError as e:
        if e.msg.code == "tmdb.http_error" and e.msg.params.get("status") == 404:
            return None  # an id TMDB does not know
        raise
    atomic_write(path, json.dumps(details).encode())
    return details


def lookup(
    conn: sqlite3.Connection,
    config_dir: Path,
    kind: str,
    tmdb_id: str | None,
    title: str | None,
    year: str | None,
    language: str,
) -> dict | None:
    """Details for a title: by the provider's TMDB id, unless TMDB's year contradicts the
    provider's (wrong id), else by searching title and year. None when TMDB is not configured
    or nothing matches."""
    tmdb = client(conn)
    if tmdb is None:
        return None
    lang = LANGUAGES.get(language, "en-US")
    if tmdb_id:
        details = _details(tmdb, config_dir, kind, tmdb_id, lang)
        if details and not (title and _year_off(details, year)):
            return details
    if not title:
        return None
    found = tmdb.find(kind, title, year, lang)
    return _details(tmdb, config_dir, kind, found, lang) if found else None
