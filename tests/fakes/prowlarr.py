"""Fake Prowlarr: /api/v1/search and .torrent downloads behind an apikey."""

from tests.fakes.http import FakeServer, Response, json_response

API_KEY = "prowlarr-key-0123456789"


class FakeProwlarr(FakeServer):
    def __init__(self):
        super().__init__()
        self.results: list[dict] = []
        self.torrents: dict[str, bytes] = {}  # id → .torrent bytes
        self.mode = "ok"  # ok | rate_limited | bad_key
        self.routes[("GET", "/api/v1/search")] = self.search
        self.routes[("GET", "/api/v1/indexer")] = self.indexers
        self.routes[("GET", "*")] = self.download

    def add(self, indexer, title, size, torrent_id=None, seeders=5):
        """Register a search result; `torrent_id` makes its .torrent downloadable."""
        url = f"{self.url}/dl/{torrent_id or title}?apikey={API_KEY}"
        self.results.append(
            {
                "indexer": indexer,
                "title": title,
                "size": size,
                "downloadUrl": url,
                "infoUrl": f"https://{indexer.lower().replace(' ', '')}.invalid/t/{torrent_id}",
                "seeders": seeders,
                "protocol": "torrent",
            }
        )

    def search(self, req):
        if self.mode == "rate_limited":
            return Response(429, b"Too Many Requests")
        if req.headers.get("x-api-key") != API_KEY or self.mode == "bad_key":
            return Response(401, b"Unauthorized")
        return json_response(self.results)

    def indexers(self, req):
        if req.headers.get("x-api-key") != API_KEY:
            return Response(401, b"Unauthorized")
        return json_response([{"id": 1, "name": "Nimbus"}, {"id": 2, "name": "Aurora"}])

    def download(self, req):
        parts = req.path.strip("/").split("/")
        ok = len(parts) == 2 and parts[0] == "dl" and req.query.get("apikey") == API_KEY
        if ok and parts[1] in self.torrents:
            headers = {"Content-Type": "application/x-bittorrent"}
            return Response(200, self.torrents[parts[1]], headers)
        return Response(404, b"not found")

    def downloaded(self) -> list[str]:
        return [r.path.split("/")[-1] for r in self.requests if r.path.startswith("/dl/")]
