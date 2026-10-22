import json
import urllib.request

from tests.fakes import serve
from tests.fakes.prowlarr import API_KEY, FakeProwlarr
from tests.fakes.qbittorrent import FakeQbit
from tests.fakes.xtream import FakeXtream


def test_dataset_is_consistent():
    with FakeXtream() as xt, FakeProwlarr() as prowl, FakeQbit() as qbit:
        serve.dataset(xt, prowl, qbit)
        req = urllib.request.Request(
            f"{prowl.url}/api/v1/search?query=x", headers={"X-Api-Key": API_KEY}
        )
        results = json.load(urllib.request.urlopen(req, timeout=5))
        assert {r["indexer"] for r in results} == {"Nimbus", "Aurora", "Cirrus"}
        assert len(xt.files) == 7
