"""Settings: services, paths, cross-seed layout and UI preferences; secrets masked (ADR-0005)."""

import copy
import os
import sqlite3
from pathlib import Path
from typing import Any

from vodarr import services
from vodarr.core import MASK, Msg, MsgError, get_setting, register_secret, set_setting
from vodarr.pipeline import locked
from vodarr.stages import CROSSSEED_DEFAULTS

LINK_TYPES = ("hardlink", "symlink", "reflink")
LANGUAGES = ("auto", "en", "fr")
THEMES = ("auto", "dark", "light")
DEFAULTS: dict[str, Any] = {
    "services": {
        "prowlarr": {"url": "", "apiKey": ""},
        "qbittorrent": {"url": "", "username": "", "password": ""},
        "tmdb": {"apiKey": ""},
    },
    "paths": {"downloads": "", "dataRoot": "/data", "qbitDataRoot": "/data"},
    "crossseed": CROSSSEED_DEFAULTS,
    "ui": {"language": "auto", "theme": "auto"},
}
# (section, service, secret field, field naming the server the secret belongs to)
SECRETS = [
    ("services", "prowlarr", "apiKey", "url"),
    ("services", "qbittorrent", "password", "url"),
    ("services", "tmdb", "apiKey", None),  # fixed server: a masked key always means "keep"
]


def load(conn: sqlite3.Connection) -> dict[str, Any]:
    out = copy.deepcopy(DEFAULTS)
    for section, default in DEFAULTS.items():
        stored = get_setting(conn, section, {}) or {}
        if section == "services":
            for name in default:
                out[section][name].update(stored.get(name) or {})
        else:
            out[section].update(stored)
    return out


def masked(conn: sqlite3.Connection) -> dict[str, Any]:
    """Settings as sent to the browser: secrets replaced by •••• (AC-SET-02)."""
    out = load(conn)
    for section, name, field, _ in SECRETS:
        if out[section][name][field]:
            out[section][name][field] = MASK
    return out


def _check_dir(path: str, code_prefix: str = "settings") -> Path:
    """An absolute, writable folder. A missing one is created with its parents, but only under
    an existing top-level folder (e.g. /data): a typo or an unmounted volume is reported, not
    silently created inside the container."""
    p = Path(path)
    if not p.is_absolute() or ".." in p.parts:
        raise MsgError(f"{code_prefix}.path_missing", path=path)
    if not p.is_dir():
        top = Path(p.anchor, *p.parts[1:2])
        if p.exists() or not top.is_dir() or top == p:
            raise MsgError(f"{code_prefix}.path_missing", path=path)
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise MsgError(f"{code_prefix}.path_not_writable", path=path) from None
    if not os.access(p, os.W_OK | os.X_OK):
        raise MsgError(f"{code_prefix}.path_not_writable", path=path)
    return p


def validate(new: dict[str, Any]) -> None:
    paths, cs, ui = new["paths"], new["crossseed"], new["ui"]
    if cs["linkType"] not in LINK_TYPES:
        raise MsgError("settings.bad_link_type")
    if ui["language"] not in LANGUAGES or ui["theme"] not in THEMES:
        raise MsgError("settings.bad_ui")
    for name in ("dataRoot", "qbitDataRoot"):
        if paths[name] and not paths[name].startswith("/"):
            raise MsgError("settings.path_missing", path=paths[name])
    downloads = _check_dir(paths["downloads"]) if paths["downloads"] else None
    link_dirs = [_check_dir(d) for d in cs["linkDirs"]]
    if downloads and link_dirs and cs["linkType"] != "symlink":
        dev = downloads.stat().st_dev
        if not any(d.stat().st_dev == dev for d in link_dirs):
            raise MsgError("settings.not_same_filesystem")  # AC-SET-04: hardlinks need it
    for svc in new["services"].values():
        url = svc.get("url") or ""  # TMDB has none
        if url and not url.startswith(("http://", "https://")):
            raise MsgError("settings.bad_url")


def save(conn: sqlite3.Connection, body: dict[str, Any]) -> dict[str, Any]:
    """Store settings. A masked secret keeps the old value only if its server is unchanged
    (AC-SET-03): a secret never follows a changed URL."""
    old = load(conn)
    new = copy.deepcopy(old)
    for section, values in body.items():
        if section not in DEFAULTS:
            raise MsgError("settings.unknown_section", section=section)
        if section == "services":
            for name, fields in values.items():
                if name not in DEFAULTS["services"]:
                    raise MsgError("settings.unknown_section", section=name)
                new["services"][name].update(fields)
        else:
            new[section].update(values)
    for section, name, field, server in SECRETS:
        entry, previous = new[section][name], old[section][name]
        if entry[field] == MASK:
            if server and entry[server] != previous[server]:
                raise MsgError("settings.secret_required", service=name)
            entry[field] = previous[field]
        register_secret(entry[field])
    validate(new)
    with locked(conn):  # all sections or none
        for section in DEFAULTS:
            set_setting(conn, section, new[section])
    return masked(conn)


def test_service(conn: sqlite3.Connection, service: str, values: dict | None = None) -> Msg | None:
    """None if the service answers, else the error message (AC-SET-07). `values` are the
    form's unsaved fields; a masked secret stands for the stored one only if the address is
    unchanged, as when saving."""
    stored = load(conn)["services"].get(service)
    if stored is None:
        raise MsgError("settings.unknown_section", section=service)
    cfg = {**stored, **{k: v for k, v in (values or {}).items() if v is not None}}
    secret = "password" if service == "qbittorrent" else "apiKey"
    if service == "tmdb":
        key = stored["apiKey"] if cfg["apiKey"] == MASK else cfg["apiKey"]
        if not key:
            return Msg("settings.not_configured", service=service)
        return services.TMDB(key).test()
    if cfg[secret] == MASK:
        if cfg["url"] != stored["url"]:
            raise MsgError("settings.secret_required", service=service)
        cfg[secret] = stored[secret]
    if not cfg["url"]:
        return Msg("settings.not_configured", service=service)
    if not cfg["url"].startswith(("http://", "https://")):
        raise MsgError("settings.bad_url")
    if service == "prowlarr":
        return services.Prowlarr(cfg["url"], cfg["apiKey"]).test()
    return services.QBittorrent(cfg["url"], cfg["username"], cfg["password"]).test()
