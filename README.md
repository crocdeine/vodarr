# VODarr

[Français](README.fr.md) · English

VODarr joins your *arr stack. It downloads a season or a movie from your IPTV VOD source
(Xtream Codes or M3U), finds the original P2P release on your trackers through Prowlarr,
checks **piece by piece** that the content is identical, rebuilds the release with links under
its original names, and seeds it in qBittorrent: **cross-seeding from IPTV files**.

- One container, one port, an English and French web interface (dark and light themes).
- Nothing is uploaded to trackers. Nothing leaves your stack except the requests you configure.
- Downloaded files never move: the rebuilt release is made of hardlinks.

## How it works

```
Explorer ──► download ──► analyse ──► search ──► verify ──► restore ──► seed
 (Xtream/M3U)   (.part,     (ffprobe:   (Prowlarr:  (every      (links in the   (qBittorrent,
                resume)    release name) packs and  piece of    cross-seed      checks then
                                         episodes)  the .torrent) layout)       seeds)
```

1. **Download** — one global queue, one file at a time, resumable (`.part` + HTTP Range,
   5 attempts). Files land in `<downloads>/<source>/<title>/Sxx/` and stay there.
2. **Analyse** — `ffprobe` reads the release name some providers leave in the track tags.
   Unknown names are fine.
3. **Search** — Prowlarr searches all your indexers for the title, then keeps:
   - season packs of the right season (`S01`, `Season 1`, `Saison 1`; never `S12` or
     `Saison 10`);
   - isolated episodes and ranges (`S01E05`, `S01E05-E06`) when you have them locally;
   - the year for a movie; a size within ±15 % of yours.
   Every version of the same season is listed for information.
4. **Verify** — the `.torrent` of each candidate is downloaded and **every piece** is hashed
   against your files, file by file. Per tracker and per scope (the pack, or each episode), the
   closest size is tried first and the first identical one stops the search.
5. **Restore** — for each identical reference, links are created under the original names in
   `<link dir>/<tracker>/<release>/`, like [cross-seed](https://www.cross-seed.org/).
6. **Seed** — the torrent is added to qBittorrent on that folder, with a forced check, in its
   own category. VODarr waits for the check to finish.

A release can end as **done**, **action required** (a partial match: some files differ, see
below), or **error** with a translated reason. Any stage can be retried.

## Install

Requirements: Docker, Prowlarr, qBittorrent ≥ 4.6 (WebUI API v2), and a **single `/data`
volume shared with qBittorrent** (hardlinks only work inside one filesystem).

1. Copy [`docker-compose.example.yml`](docker-compose.example.yml) next to your stack and
   adapt the marked values: `PUID`/`PGID` (the user qBittorrent runs as), the `/data` mount,
   the network, and `VODARR_ALLOWED_HOSTS`.
2. `docker compose up -d vodarr`
3. Read the **setup code** in the log: `docker logs vodarr` →
   `First run: open VODarr and enter the setup code xxxx-xxxx-xxxx`.
4. Open `http://<host>:8863`, create the admin account with that code.
5. In **Settings**: add your source, Prowlarr and qBittorrent (each has a *Test* button), the
   downloads folder (e.g. `/data/vod`) and the link folder (e.g. `/data/cross-seed`). If you
   use cross-seed, *Import cross-seed config.js* fills in its folders and link type.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `PUID` / `PGID` | `1000` | User the app runs as (never root) |
| `TZ` | — | Time zone |
| `VODARR_ALLOWED_HOSTS` | `localhost`, container name and IP | Comma-separated host names you use in the browser; others get HTTP 421 (anti DNS-rebinding) |
| `VODARR_PORT` | `8863` | Listening port |
| `VODARR_SETUP_TOKEN` | random | Fixed setup code instead of the one printed in the log |
| `VODARR_DEV` | — | `1` enables `/docs` (OpenAPI) |

Volumes: `/config` (database, catalogue cache, reference `.torrent` files, M3U files under
`/config/playlists`) and `/data` (your media, shared with qBittorrent).

### Settings worth knowing

- **Path seen by qBittorrent** — if qBittorrent mounts the same folder under another path,
  set *Data root (VODarr)* and *Same root as seen by qBittorrent* (e.g. `/data` → `/downloads`).
- **Link type** — `hardlink` (default), `symlink` or `reflink` (btrfs/xfs). Hardlinks need the
  downloads and the link folder on the same volume; VODarr checks it when you save.
- **Flat layout** — `<link dir>/<release>` instead of `<link dir>/<tracker>/<release>`.
- **Category** — `cross-seed-link` by default.

## Automatic behaviours

VODarr acts on your behalf in these ways; each one is deliberate.

- **`.torrent` downloads from your trackers.** To verify a candidate, VODarr downloads its
  `.torrent` through Prowlarr — only filtered candidates, and it stops at the first identical
  one per tracker and scope. On some private trackers this counts as a *snatch*: check their
  hit-and-run rules before enabling Prowlarr. Reference `.torrent` files stay in
  `/config/torrents` (they contain your passkey) and are never served by the API.
- **±15 % size tolerance.** Results far from your size are listed but not verified; inside the
  tolerance, the piece check decides. When in doubt, VODarr verifies.
- **Downloaded files never move.** They are not moved, renamed or copied. The rebuilt release
  is made of links pointing to them, so nothing is duplicated on disk.
- **In-place NUL padding removal — the only change ever made to a downloaded file.** Some IPTV
  providers append a few NUL bytes to the end of files. A hardlink *is* the same file, so it
  has the same size; for the link to match the original release, VODarr truncates the
  downloaded file to its original length. The file stays where it is with its name; only NUL
  bytes are removed, and only after checking there is nothing else (otherwise the file is
  reported as *different* and left untouched). The video is bit-for-bit unchanged. The only
  alternative would be a copy, i.e. a duplicate on disk.
- **Season packs and isolated episodes.** Both are searched and verified; one episode can be
  seeded on several trackers, in a pack on one and alone on another.
- **Annexes completed by qBittorrent.** When only small extras are missing (`.nfo`, samples,
  non-video files under 1 MiB), the match is *identical, annexes to complete* and qBittorrent
  downloads them itself (a negligible ratio cost).
- **Partial matches wait for you.** If a video differs (for instance one episode was provided
  in another format), the release shows *action required* with the file-by-file report and a
  **Complete from \<tracker\>** button: only the identical files are linked, qBittorrent
  downloads the rest into the link folder.
- **qBittorrent category.** Torrents use the `cross-seed-link` category by default: exclude it
  from cleaning tools such as Cleanuparr, or they may remove your seeds.
- **cross-seed compatibility.** Same layout as cross-seed, so both can share folders. Importing
  `config.js` reads literal values only; the file is **never executed**.
- **Deleting a release** removes what VODarr created: its torrents in qBittorrent (with their
  files, i.e. the links and what qBittorrent completed), the links and the record. Downloaded
  files are deleted only if you tick the box in the confirmation. A torrent that was already
  in qBittorrent before VODarr is never removed.

## Security

- **Admin account** created at first run with the one-time code from the log (no one else on
  your network can claim a fresh instance). Passwords: scrypt, 12 characters minimum.
- **Sessions** in an `HttpOnly`, `SameSite=Strict` cookie, 30 days, sliding. **API key** for
  scripts (`X-Api-Key` header): masked in Settings, shown after typing the admin password,
  regenerable.
- **Brute force:** 5 failures in 5 minutes lock that address for 5 minutes; 50 failures from
  all addresses lock everyone. Behind a reverse proxy every client shares the proxy's address.
- **No local-network exemption**, a `Host` allowlist, same-origin checks on every change, a
  strict Content Security Policy, 1 MiB request limit.
- **Secrets** (source passwords, M3U URL, Prowlarr key, qBittorrent password) are stored in
  `/config/vodarr.db` (mode 600, folder 700), never sent back to the browser (shown as `••••`),
  never written to the logs. A masked secret is only reused if its server address is unchanged.
- **Posters** in the Explorer are loaded by your browser straight from the provider's image
  hosts (so they see your IP address), like in any IPTV app.
- **No TLS** inside the container: for remote access, put VODarr behind your reverse proxy or
  VPN. The proxy must keep the `Host` header and that name must be in `VODARR_ALLOWED_HOSTS`.

## Development

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest -q                                   # ~700 tests, no network
ruff check . && ruff format --check .
python -m tests.fakes.serve                 # fake Xtream, Prowlarr, qBittorrent (synthetic data)
VODARR_CONFIG=.dev-config VODARR_ALLOWED_HOSTS=localhost \
  uvicorn vodarr.app:create_app --factory --port 8863
```

Design decisions in [`docs/adr/`](docs/adr/) (in
French). The interface is plain HTML, CSS and JavaScript modules, with no build step; the
design system reference is at `#/design`.

## Licence

[MIT](LICENSE). VODarr is not affiliated with Prowlarr, qBittorrent, cross-seed or any IPTV
provider. Use it with content you are entitled to access, and respect your trackers' rules.
