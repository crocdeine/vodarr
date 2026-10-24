"""Fake TMDB API v3: search and details for a couple of fictional titles."""

from tests.fakes.http import FakeServer, Response, json_response

KEY = "tmdb-key-0123456789abcdef"
TOKEN = "eyJ.fake.v4-token"

TITLES = {
    ("tv", "4242"): {
        "id": 4242,
        "name": "Northwind Precinct",
        "original_name": "Northwind Precinct",
        "overview": {"fr-FR": "Des policiers du Nord.", "en-US": "Northern cops."},
        "first_air_date": "2021-03-01",
        "genres": [{"id": 1, "name": "Crime"}],
        "vote_average": 7.9,
        "episode_run_time": [52],
        "number_of_seasons": 2,
        "poster_path": "/np.jpg",
        "backdrop_path": "/nb.jpg",
        "credits": {"cast": [{"name": "Ada Lind"}, {"name": "Bo Strand"}]},
    },
    ("movie", "778"): {  # same title, another film: what a provider may wrongly point at
        "id": 778,
        "title": "Harbour Lights",
        "overview": {"fr-FR": "Un autre film.", "en-US": "Another film."},
        "release_date": "2023-12-08",
        "genres": [],
        "vote_average": 6.5,
        "runtime": 140,
        "poster_path": None,
        "credits": {"cast": []},
    },
    ("movie", "777"): {
        "id": 777,
        "title": "Harbour Lights",
        "overview": {"fr-FR": "Des bateaux.", "en-US": "Boats."},
        "release_date": "2020-05-01",
        "genres": [],
        "vote_average": 6.1,
        "runtime": 101,
        "poster_path": None,
        "credits": {"cast": []},
    },
}


class FakeTMDB(FakeServer):
    def __init__(self):
        super().__init__()
        self.routes[("GET", "*")] = self.handle

    def handle(self, req):
        authed = (
            req.query.get("api_key") == KEY or req.headers.get("authorization") == f"Bearer {TOKEN}"
        )
        if not authed:
            return Response(401, b'{"status_message": "Invalid API key"}')
        if req.path == "/3/configuration":
            return json_response({"images": {}})
        if req.path in ("/3/search/tv", "/3/search/movie"):
            kind = "tv" if req.path.endswith("tv") else "movie"
            year = req.query.get("year") or req.query.get("first_air_date_year") or ""
            hits = [
                {"id": t["id"]}
                for (k, _), t in TITLES.items()
                if k == kind
                and req.query.get("query", "").lower() in (t.get("name") or t.get("title")).lower()
                and (t.get("release_date") or t.get("first_air_date") or "").startswith(year)
            ]
            return json_response({"results": hits})
        parts = req.path.strip("/").split("/")
        if len(parts) == 3 and (parts[1], parts[2]) in TITLES:
            t = dict(TITLES[(parts[1], parts[2])])
            t["overview"] = t["overview"][req.query.get("language", "en-US")]
            return json_response(t)
        return Response(404, b'{"status_message": "not found"}')
