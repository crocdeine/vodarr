"""Rebuild a release as links to the downloaded files, cross-seed layout (ADR-0006).

Downloaded files never move: the only write allowed on them is trimming trailing NUL padding.
"""

import fcntl
import filecmp
import os
import re
from dataclasses import dataclass
from pathlib import Path

from vodarr.core import MsgError
from vodarr.torrent import Torrent

FICLONE = 0x40049409  # Linux ioctl: copy-on-write clone (btrfs, xfs)


@dataclass(frozen=True)
class Restored:
    save_path: Path  # what qBittorrent gets (before translating to its own view)
    root: Path  # <save_path>/<torrent name>: everything this reference created lives under it
    linked: int
    reused: int


def _dev(path: Path) -> int:
    return path.stat().st_dev


def pick_link_dir(link_dirs: list[Path], downloads: Path, link_type: str) -> Path:
    existing = [d for d in link_dirs if d.is_dir()]
    if link_type == "symlink":
        if existing:
            return existing[0]
        raise MsgError("restore.no_linkdir")
    dev = _dev(downloads)
    for d in existing:
        if _dev(d) == dev:
            return d
    raise MsgError("restore.no_linkdir_same_fs")


def tracker_dir_name(indexer: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", indexer).strip().rstrip(".")
    return name or "_"


def strip_padding(path: Path, length: int) -> None:
    """Re-check that the excess is NUL-only, then truncate in place (validated exception).
    Check and truncate go through one descriptor opened without following symlinks."""
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    with os.fdopen(fd, "r+b") as f:
        extra = os.fstat(fd).st_size - length
        if extra < 0:
            raise MsgError("verify.too_short", file=path.name)
        f.seek(length)
        while chunk := f.read(1 << 20):
            if chunk.strip(b"\0"):
                raise MsgError("verify.non_null_padding", file=path.name)
        if extra:
            f.truncate(length)


def _real(path: Path) -> Path:
    return Path(os.path.realpath(path))  # never raises on symlink loops, unlike resolve()


def _same(src: Path, dst: Path, link_type: str) -> bool:
    if link_type == "symlink":
        return dst.is_symlink() and _real(dst) == _real(src)
    if link_type == "hardlink":
        return not dst.is_symlink() and dst.exists() and os.path.samefile(src, dst)
    # Known limit: reflink target compared byte by byte, slow on huge files; only hit on re-runs
    return dst.is_file() and not dst.is_symlink() and filecmp.cmp(src, dst, shallow=False)


def _reflink(src: Path, dst: Path) -> None:
    with src.open("rb") as s, dst.open("xb") as d:
        try:
            fcntl.ioctl(d.fileno(), FICLONE, s.fileno())
        except BaseException as e:
            dst.unlink(missing_ok=True)
            if isinstance(e, OSError):
                raise MsgError("restore.reflink_unsupported") from e
            raise


def make_link(src: Path, dst: Path, link_type: str) -> bool:
    """Create dst → src. Returns False if an identical link was already there. Never overwrites."""
    if os.path.lexists(dst):
        if _same(src, dst, link_type):
            return False
        raise MsgError("restore.target_exists", file=dst.name)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if link_type == "hardlink":
        os.link(src, dst)
    elif link_type == "symlink":
        os.symlink(_real(src), dst)
    elif link_type == "reflink":
        _reflink(src, dst)
    else:
        raise ValueError(link_type)
    return True


def restore(
    t: Torrent,
    report: list[dict],
    downloads: Path,
    link_dir: Path,
    indexer: str,
    flat: bool,
    link_type: str,
) -> Restored:
    """Link every `identical` file of the verification report under its original name.
    Sources must be regular files under `downloads`; targets must stay under `link_dir`."""
    save_path = link_dir if flat else link_dir / tracker_dir_name(indexer)
    root = save_path / t.name
    if not _real(root).is_relative_to(_real(link_dir)):
        raise MsgError("restore.unsafe_path")
    local = {e["path"]: e["local"] for e in report if e["status"] == "identical" and e["local"]}
    linked = reused = 0
    for f in t.files:  # destinations come from the torrent, never from stored JSON
        if f.path not in local:
            continue
        src, dst = Path(local[f.path]), root / f.path if t.multi else root
        if src.is_symlink() or not _real(src).is_relative_to(_real(downloads)):
            raise MsgError("restore.unsafe_path", file=src.name)
        try:
            strip_padding(src, f.length)
            created = make_link(src, dst, link_type)
        except OSError as e:
            raise MsgError("restore.failed", file=dst.name, error=e.strerror or "") from e
        linked += created
        reused += not created
    return Restored(save_path, root, linked, reused)
