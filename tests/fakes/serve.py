"""Run the fake Xtream, Prowlarr and qBittorrent with a synthetic catalogue, for local demos
and the example compose: python -m tests.fakes.serve [--host 127.0.0.1] [--base-port 9800]

Nothing here is real: titles, trackers and files are generated."""

import argparse
import contextlib
import signal
import threading
from http.server import ThreadingHTTPServer

from tests.fakes import xtream as fx
from tests.fakes.prowlarr import API_KEY, FakeProwlarr
from tests.fakes.qbittorrent import PASSWORD as QPASS
from tests.fakes.qbittorrent import USER as QUSER
from tests.fakes.qbittorrent import FakeQbit
from tests.fakes.tmdb import KEY as TMDB_KEY
from tests.fakes.tmdb import FakeTMDB
from tests.fakes.torrents import make_torrent, random_bytes
from tests.fakes.xtream import FakeXtream

SIZES = (400_000, 380_000, 420_000)


def dataset(xt: FakeXtream, prowl: FakeProwlarr, qbit: FakeQbit) -> None:
    """Scenarios worth seeing in the UI: identical pack on two trackers, a partial season 2,
    a movie with annexes, plus isolated episodes."""
    s1 = [random_bytes(n, i) for i, n in enumerate(SIZES)]
    s2 = [random_bytes(n, 10 + i) for i, n in enumerate(SIZES)]
    for i, data in enumerate(s1, start=1):
        xt.files[f"11{i:02d}"] = data + b"\0" * 6  # NUL padding, like real providers
    for i, data in enumerate(s2, start=1):
        xt.files[f"12{i:02d}"] = data if i < 3 else random_bytes(len(data) - 700, 99)  # E03 differs
    movie = random_bytes(900_000, 50)
    xt.files["500"] = movie
    xt.series_info["100"]["episodes"] = {
        "1": [
            fx.episode(
                1100 + i,
                i,
                f"Northwind Precinct - S01E{i:02d}",
                release=f"Northwind.Precinct.S01E{i:02d}.1080p.WEB-DL-GRP",
            )
            for i in (1, 2, 3)
        ],
        "2": [fx.episode(1200 + i, i, f"Northwind Precinct - S02E{i:02d}") for i in (1, 2, 3)],
    }
    pack1 = "Northwind.Precinct.S01.1080p.WEB-DL-GRP"
    names1 = [f"Northwind.Precinct.S01E{i:02d}.1080p.WEB-DL-GRP.mkv" for i in (1, 2, 3)]
    for tracker, tid in (("Nimbus", "n1"), ("Aurora", "a1")):
        prowl.add(tracker, pack1, sum(SIZES), torrent_id=tid)
        prowl.torrents[tid] = make_torrent(pack1, list(zip(names1, s1, strict=True)))
    prowl.add("Nimbus", "Northwind.Precinct.S01.720p.HDTV-OTHER", sum(SIZES) // 3, torrent_id="n0")
    pack2 = "Northwind.Precinct.S02.1080p.WEB-DL-GRP"
    names2 = [f"Northwind.Precinct.S02E{i:02d}.1080p.WEB-DL-GRP.mkv" for i in (1, 2, 3)]
    prowl.add("Nimbus", pack2, sum(SIZES), torrent_id="n2")
    prowl.torrents["n2"] = make_torrent(pack2, list(zip(names2, s2, strict=True)))
    name = "Harbour.Lights.2020.1080p.BluRay.x264-GRP"
    prowl.add("Cirrus", name, len(movie), torrent_id="c1")
    prowl.torrents["c1"] = make_torrent(
        name, [(f"{name}.mkv", movie), (f"{name}.nfo", b"notes" * 40)]
    )
    qbit.next_states = ["checkingUP", "checkingUP", "uploading"]
    xt.series_info["100"]["info"]["tmdb"] = "4242"  # ids known to the fake TMDB
    xt.vod_info = {
        "500": {**fx.VOD_INFO["500"], "info": {**fx.VOD_INFO["500"]["info"], "tmdb_id": "777"}}
    }


class Fixed(ThreadingHTTPServer):
    allow_reuse_address = True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--base-port", type=int, default=9800)
    args = parser.parse_args()
    servers = [FakeXtream(), FakeProwlarr(), FakeQbit(), FakeTMDB()]
    for offset, srv in enumerate(servers):
        handler = srv.httpd.RequestHandlerClass
        srv.httpd.server_close()
        srv.httpd = Fixed((args.host, args.base_port + offset), handler)
        srv.url = f"http://{args.host}:{args.base_port + offset}"
        srv.thread = threading.Thread(target=srv.httpd.serve_forever, daemon=True)
    xt, prowl, qbit, tmdb = servers
    dataset(xt, prowl, qbit)
    for srv in servers:
        srv.__enter__()
    print(f"Fake Xtream      {xt.url}  user={fx.USER} password={fx.PASSWORD}")
    print(f"Fake Prowlarr    {prowl.url}  apiKey={API_KEY}")
    print(f"Fake qBittorrent {qbit.url}  user={QUSER} password={QPASS}")
    print(
        f"Fake TMDB        {tmdb.url}/3  apiKey={TMDB_KEY}  (run VODarr with VODARR_TMDB_URL={tmdb.url}/3)"
    )
    print("All data is synthetic. Ctrl+C to stop.", flush=True)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    with contextlib.suppress(KeyboardInterrupt):
        stop.wait()
    for srv in servers:
        srv.__exit__(None, None, None)


if __name__ == "__main__":
    main()
