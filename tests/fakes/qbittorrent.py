"""Fake qBittorrent WebUI API v2: login/SID, add (text or JSON answer), info, delete, categories."""

import re
import secrets
import urllib.parse

from tests.fakes.http import FakeServer, Response, json_response
from vodarr.torrent import load_torrent

USER, PASSWORD = "admin", "qbit-pass-42"


def _multipart(req) -> dict[str, bytes]:
    boundary = re.search(r"boundary=(.+)", req.headers.get("content-type", "")).group(1).encode()
    fields = {}
    for part in req.body.split(b"--" + boundary):
        head, _, body = part.partition(b"\r\n\r\n")
        m = re.search(rb'name="([^"]+)"', head)
        if m:
            fields[m.group(1).decode()] = body.removesuffix(b"\r\n")
    return fields


class FakeQbit(FakeServer):
    def __init__(self, json_add: bool = False, v5_login: bool = False, bypass: bool = False):
        super().__init__()
        self.json_add = json_add
        self.v5_login = v5_login  # qBittorrent 5.2: 204 + QBT_SID_<port> cookie
        self.bypass = bypass  # "bypass authentication for clients in whitelisted subnets"
        self.sids: set[str] = set()
        self.torrents: dict[str, dict] = {}  # hash → {state, progress, savepath, category, name}
        self.added: list[dict] = []
        self.categories: set[str] = set()
        self.deleted: list[tuple[str, bool]] = []
        self.next_states: list[str] = []  # states applied to new torrents, one poll at a time
        r = self.routes
        r[("POST", "/api/v2/auth/login")] = self.login
        r[("POST", "/api/v2/torrents/add")] = self.guard(self.add)
        r[("GET", "/api/v2/torrents/info")] = self.guard(self.info)
        r[("POST", "/api/v2/torrents/delete")] = self.guard(self.delete)
        r[("POST", "/api/v2/torrents/createCategory")] = self.guard(self.create_category)
        r[("GET", "/api/v2/app/version")] = self.guard(lambda req: Response(200, b"v5.0.0"))

    def expire_sessions(self):
        self.sids.clear()

    def guard(self, handler):
        def run(req):
            sid = re.search(r"SID(?:_\d+)?=([^;]+)", req.headers.get("cookie", ""))
            if not self.bypass and (not sid or sid.group(1) not in self.sids):
                return Response(403, b"Forbidden")
            return handler(req)

        return run

    def login(self, req):
        form = dict(urllib.parse.parse_qsl(req.body.decode()))
        if self.bypass:
            return Response(204, b"")  # whitelisted: no session, any credentials
        if (form.get("username"), form.get("password")) != (USER, PASSWORD):
            return Response(401, b"") if self.v5_login else Response(200, b"Fails.")
        sid = secrets.token_hex(8)
        self.sids.add(sid)
        if self.v5_login:
            cookie = f"QBT_SID_8080={sid}; HttpOnly; SameSite=Strict; path=/"
            return Response(204, b"", {"Set-Cookie": cookie})
        return Response(200, b"Ok.", {"Set-Cookie": f"SID={sid}; HttpOnly; path=/"})

    def add(self, req):
        f = _multipart(req)
        t = load_torrent(f["torrents"])
        self.added.append(
            {k: v.decode() for k, v in f.items() if k != "torrents"} | {"hash": t.infohash}
        )
        if f.get("category", b"").decode() not in self.categories | {""}:
            return Response(200, b"Fails.")
        self.torrents[t.infohash] = {
            "hash": t.infohash,
            "name": t.name,
            "state": "checkingUP",
            "progress": 0.0,
            "savepath": f["savepath"].decode(),
            "category": f["category"].decode(),
            "ratio": 0.0,
            "uploaded": 0,
            "states": list(self.next_states),
        }
        if self.json_add:
            return json_response(
                {
                    "success_count": 1,
                    "failure_count": 0,
                    "pending_count": 0,
                    "added_torrent_ids": [t.infohash],
                }
            )
        return Response(200, b"Ok.")

    def info(self, req):
        wanted = req.query.get("hashes", "").split("|")
        out = []
        for h in wanted:
            t = self.torrents.get(h)
            if not t:
                continue
            if t["states"]:
                t["state"] = t["states"].pop(0)
                t["progress"] = 1.0 if t["state"] in ("uploading", "stalledUP") else t["progress"]
            out.append({k: v for k, v in t.items() if k != "states"})
        return json_response(out)

    def delete(self, req):
        form = dict(urllib.parse.parse_qsl(req.body.decode()))
        for h in form["hashes"].split("|"):
            self.torrents.pop(h, None)
            self.deleted.append((h, form.get("deleteFiles") == "true"))
        return Response(200, b"")

    def create_category(self, req):
        form = dict(urllib.parse.parse_qsl(req.body.decode()))
        if form["category"] in self.categories:
            return Response(409, b"Category already exists")
        self.categories.add(form["category"])
        return Response(200, b"")

    def seed_existing(self, data: bytes):
        """A torrent already present in qBittorrent before VODarr adds it."""
        h = load_torrent(data).infohash
        self.torrents[h] = {
            "hash": h,
            "name": "x",
            "state": "uploading",
            "progress": 1.0,
            "savepath": "/elsewhere",
            "category": "",
            "ratio": 1.0,
            "uploaded": 1,
            "states": [],
        }
        return h
