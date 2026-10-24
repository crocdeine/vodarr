# Changelog

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
