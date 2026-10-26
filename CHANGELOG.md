# Changelog

## 0.2.0 — 2026-10-26

### Added
- **"No match" status**: a release that was checked but has nothing linked or seeded (no
  identical release on your trackers) is no longer shown as "Done".
- The releases list is paginated (no more 200-release cap) and filtered by status on the
  server; the filter is a drop-down list.
- A browser reaching VODarr under a name missing from `VODARR_ALLOWED_HOSTS` gets a readable
  page naming the setting to change (the API keeps its JSON error).
- Explorer: titles already downloaded show their releases ("S4 · Done"), each season links to
  its release, and a release links back to the exact catalogue entry it came from ("Open in
  the Explorer") — useful when several providers carry the same series.

### Fixed
- One torrent returned twice by Prowlarr (two download URLs) is listed and checked once.
- Importing a cross-seed `config.js`: top-level settings win over the same keys in nested
  objects.
- A torrent cross-posted on several trackers (same infohash) gets one link folder, the one
  qBittorrent seeds.
- A poster that fails to load is no longer retried (and shown broken) at every refresh.
- Downloads drawer: with many files the cards were squeezed and unreadable. Files are now
  grouped by release ("Lupin · Season 3", "1/8 done") with one compact line per episode, and
  the drawer scrolls.

## 0.1.3 — 2026-10-26

### Added
- Settings › Prowlarr: **maximum .torrent checks per tracker** for one release (0 = no limit).
  Each check downloads a .torrent, which some private trackers count as a snatch; candidates
  beyond the limit are listed as not checked.

### Changed
- The settings icon is a cog. The "Design system" page is removed (development only).

### Fixed
- Posters in lists: the releases list now shows the title's poster (the catalogue's, or TMDB's
  for releases queued before this version), and posters served over plain http are no longer
  blocked.
- When VODarr cannot be reached (stopped, network down), the interface says so instead of
  failing silently.

## 0.1.2 — 2026-10-25

### Changed
- READMEs: a prominent legal disclaimer (no content provided, users responsible for the
  legality of their IPTV source and downloads, no affiliation, no warranty).
- READMEs redesigned: banner, screenshots (synthetic data, dark and light), pipeline diagram,
  feature and compatibility tables, quick start, troubleshooting.

### Fixed
Found by an exploratory test session against fake services:
- **Pressing Esc in a confirmation dialog confirmed it** (delete a release or a source,
  regenerate the API key). Esc now cancels.
- Provider and TMDB HTTP errors showed "Something went wrong" instead of their message.
- A title without Latin letters or digits (e.g. Cyrillic) searched Prowlarr with an empty
  query, and unrelated releases became candidates. Such a title is now skipped (add a TMDB key
  to search its original or English title).
- Real title prefixes were taken for provider tags: "CSI: Miami", "FBI - Most Wanted".
- An unresponsive qBittorrent froze the whole interface: the live seed status now uses a short
  timeout and pauses after a failure.
- Episodes added while a stage ran could stay unseeded; "retry from download" on a finished
  release, or removing its last file, left it downloading forever.
- A waiting seed stage can now be retried or deleted instead of holding the pipeline for hours.
- Cancelling a download while the provider is down takes effect at once, and a download still
  being written can no longer be removed (no stray `.part`).
- Explorer: picking another title while one was loading could show, and queue, the wrong one.
- M3U playlists starting with a UTF-8 BOM, and Xtream series with one malformed episode, are
  read instead of rejected.
- Releases can be deleted after the link folder setting changed; links outside the current
  link folders are left untouched.
- Saving settings or a source through the API: a changed server without its secret is refused;
  `null` values keep the stored setting; an M3U source can switch from a file to a URL.
- Deleting a release while qBittorrent is unreachable is refused before anything changes.
- Error messages keep their details ("HTTP 404", number of attempts).
- `.torrent` files of replaced candidates and empty title folders are removed.
- Release page: pipeline stages stay aligned whatever the length of their message, and the
  retry buttons sit on one line.

### Known limits
- The releases list shows the 200 most recent releases.
- Importing a cross-seed `config.js` reads the first `linkDirs` it finds, even in a nested
  object.
- The same torrent found on two trackers is seeded from one link folder only.

## 0.1.1 — 2026-10-25

Fixes found by the first run on a real stack, and two additions.

### Added
- **TMDB title details** (optional, your own key in Settings › Services): synopsis, poster,
  genres, rating, runtime or seasons and cast in the Explorer and on releases, in the interface
  language, cached 7 days. A provider's TMDB id is ignored when its year contradicts the source.
- **Technical metadata** on the release page, read by ffprobe after download: resolution, codec
  and profile, HDR, frame rate, audio tracks (codec, channels, language), subtitles, duration.
- Missing download and link folders are created when saving Settings (under an existing
  top-level folder such as `/data`).

### Fixed
- SQLite now uses a rollback journal instead of WAL: with `/config` bind-mounted from macOS into
  the Docker VM, WAL lost committed rows and failed with "disk I/O error". Databases created by
  0.1.0 are converted on start.
- qBittorrent 5.2: login answers 204 with a `QBT_SID_<port>` cookie, and a whitelisted subnet may
  skip authentication; both now work.
- The Settings *Test* buttons check what is typed in the form, saved or not.
- Search: provider tags before titles (`FR - `, `┃FR┃`, `TOP - `, `[4K]`…) no longer prevent every
  match; with TMDB, original and English titles are searched too.

### Changed
- CI actions moved to their Node 24 majors; runners pinned to `ubuntu-24.04`.
- The Starlette test client uses `httpx2`.

## 0.1.0 — 2026-10-22

First release.
