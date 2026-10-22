"""Fake Xtream Codes server (player_api.php + stream files) built on FakeServer."""

import copy

from tests.fakes.http import FakeServer, FileRoute, Response, json_response

USER, PASSWORD = "alice", "s3cret-pass"


def episode(eid, num, title, ext="mkv", release=None):
    info = {"video": {"tags": {"title": release}}} if release else {}
    return {
        "id": str(eid),
        "episode_num": num,
        "title": title,
        "container_extension": ext,
        "info": info,
    }


CATALOGUE = {
    "get_series_categories": [
        {"category_id": "1", "category_name": "NORDIC | Crime"},
        {"category_id": "2", "category_name": "Documentaries"},
    ],
    "get_vod_categories": [{"category_id": "10", "category_name": "NORDIC | Films"}],
    "get_series": [
        {
            "series_id": 100,
            "name": "Northwind Precinct",
            "category_id": "1",
            "cover": "https://img.invalid/n.jpg",
            "releaseDate": "2021-03-01",
        },
        {
            "series_id": 101,
            "name": "Élan Vital",
            "category_id": "2",
            "cover": None,
            "releaseDate": "",
        },
    ],
    "get_vod_streams": [
        {
            "stream_id": 500,
            "name": "Harbour Lights (2020)",
            "category_id": "10",
            "stream_icon": "",
            "container_extension": "mp4",
            "year": "2020",
        },
    ],
}
SERIES_INFO = {
    "100": {
        "info": {
            "name": "Northwind Precinct",
            "plot": "Cops.",
            "cover": "https://img.invalid/n.jpg",
            "releaseDate": "2021-03-01",
        },
        "episodes": {
            "2": [episode(1201, 1, "Northwind Precinct - S02E01")],
            "1": [
                episode(
                    1102,
                    2,
                    "Northwind Precinct - S01E02",
                    release="Northwind.Precinct.S01E02.1080p.WEB-DL-GRP",
                ),
                episode(
                    1101,
                    1,
                    "Northwind Precinct - S01E01",
                    release="Northwind.Precinct.S01E01.1080p.WEB-DL-GRP",
                ),
                episode(1103, 3, "Bad/../Name: S01E03", ext="exe"),
            ],
        },
    }
}
VOD_INFO = {
    "500": {
        "info": {
            "plot": "Boats.",
            "movie_image": "",
            "releasedate": "2020-05-01",
            "audio": {"tags": {"title": "Harbour.Lights.2020.1080p.BluRay.x264-GRP"}},
        },
        "movie_data": {
            "stream_id": 500,
            "name": "Harbour Lights (2020)",
            "container_extension": "mp4",
        },
    }
}


class FakeXtream(FakeServer):
    def __init__(self, user=USER, password=PASSWORD):
        super().__init__()
        self.user, self.password = user, password
        self.mode = "ok"  # ok | auth_fail | html | not_list
        self.files: dict[str, bytes] = {}
        self.series_info = copy.deepcopy(SERIES_INFO)  # per instance: tests and demos edit it
        self.routes[("GET", "/player_api.php")] = self.api
        self.routes[("GET", "*")] = self.stream

    def api(self, req):
        if self.mode == "html":
            return Response(200, b"<html>maintenance</html>", {"Content-Type": "text/html"})
        if self.mode == "auth_fail" or (req.query.get("username"), req.query.get("password")) != (
            self.user,
            self.password,
        ):
            return json_response({"user_info": {"auth": 0}})
        action = req.query.get("action")
        if self.mode == "not_list" and action in CATALOGUE:
            return json_response({"unexpected": True})
        if action in CATALOGUE:
            return json_response(CATALOGUE[action])
        if action == "get_series_info":
            return json_response(self.series_info.get(req.query.get("series_id"), []))
        if action == "get_vod_info":
            return json_response(VOD_INFO.get(req.query.get("vod_id"), []))
        return json_response({"user_info": {"auth": 1}})

    def stream(self, req):
        parts = req.path.strip("/").split("/")
        if (
            len(parts) == 4
            and parts[0] in ("series", "movie")
            and parts[1:3] == [self.user, self.password]
        ):
            stream_id = parts[3].split(".")[0]
            if stream_id in self.files:
                return FileRoute(self.files[stream_id])(req)
        return Response(404, b"not found")
