"""Read .torrent files, match their files to downloaded files and verify every piece."""

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from vodarr.core import MsgError

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".webm"}
ANNEX_LIMIT = 1024 * 1024  # non-video files totalling less than this are annexes
SIZE_SLACK = 64  # IPTV padding tolerated when matching by size
EPISODE = re.compile(r"S\d{1,2}E\d{1,3}", re.I)
SAMPLE = re.compile(r"(?i)(^|[\W_])sample([\W_]|$)")
INT = re.compile(rb"0|-?[1-9]\d*")
MAX_PIECE = 1 << 30  # sanity bound; pieces are hashed in chunks anyway
CHUNK = 1 << 20


def bdecode(data: bytes, i: int = 0, depth: int = 0) -> tuple[object, int]:
    if depth > 64:
        raise ValueError("too deep")
    c = data[i : i + 1]
    if c == b"i":
        j = data.index(b"e", i)
        if not INT.fullmatch(data[i + 1 : j]):
            raise ValueError("bad integer")
        return int(data[i + 1 : j]), j + 1
    if c in (b"l", b"d"):
        i += 1
        out: list | dict = [] if c == b"l" else {}
        while data[i : i + 1] != b"e":
            key, i = bdecode(data, i, depth + 1)
            if isinstance(out, list):
                out.append(key)
            else:
                out[key], i = bdecode(data, i, depth + 1)
        return out, i + 1
    j = data.index(b":", i)
    if not data[i:j].isdigit():
        raise ValueError("bad string length")
    n = int(data[i:j])
    if j + 1 + n > len(data):
        raise ValueError("bad string length")
    return data[j + 1 : j + 1 + n], j + 1 + n


@dataclass(frozen=True)
class TorrentFile:
    path: str  # relative to the torrent root (multi-file) or the file name (single-file)
    length: int
    offset: int


@dataclass(frozen=True)
class Torrent:
    name: str
    multi: bool
    files: list[TorrentFile]
    piece_length: int
    pieces: bytes
    infohash: str


def _safe_part(raw: bytes) -> str:
    part = raw.decode("utf-8")
    if part in ("", ".", "..") or "/" in part or "\\" in part or "\0" in part:
        raise ValueError(f"unsafe path component {part!r}")
    return part


def _info_bytes(data: bytes) -> bytes:
    """The raw bencoded `info` value: the infohash must be computed on the original bytes."""
    if data[:1] != b"d":
        raise ValueError("not a dict")
    i = 1
    while data[i : i + 1] != b"e":
        key, i = bdecode(data, i)
        start = i
        _, i = bdecode(data, i)
        if key == b"info":
            return data[start:i]
    raise ValueError("no info")


def _int(value: object) -> int:
    if not isinstance(value, int) or value < 0:
        raise ValueError("bad integer field")
    return value


def load_torrent(data: bytes) -> Torrent:
    try:
        raw_info = _info_bytes(data)
        info, _ = bdecode(raw_info)
        if not isinstance(info, dict):
            raise ValueError("info is not a dict")
        if b"pieces" not in info:
            if b"file tree" in info:
                raise MsgError("verify.v2_only")
            raise ValueError("no pieces")
        name = _safe_part(info[b"name"])
        if b"files" in info:
            files, offset = [], 0
            for f in info[b"files"]:
                path = "/".join(_safe_part(p) for p in f[b"path"])
                if not path:
                    raise ValueError("empty path")
                files.append(TorrentFile(path, _int(f[b"length"]), offset))
                offset += files[-1].length
            paths = {f.path for f in files}
            if not files or len(paths) != len(files):
                raise ValueError("empty or duplicate paths")
            if any(f.path.rsplit("/", 1)[0] in paths for f in files if "/" in f.path):
                raise ValueError("file/directory collision")
        else:
            files = [TorrentFile(name, _int(info[b"length"]), 0)]
        piece_length, pieces = _int(info[b"piece length"]), info[b"pieces"]
        total = files[-1].offset + files[-1].length
        if not isinstance(pieces, bytes) or not 0 < piece_length <= MAX_PIECE:
            raise ValueError("bad pieces")
        if len(pieces) != 20 * -(-total // piece_length):  # every byte must be covered
            raise ValueError("piece count does not match length")
    except MsgError:
        raise
    except (ValueError, KeyError, TypeError, IndexError, AttributeError, UnicodeDecodeError) as e:
        raise MsgError("verify.unreadable") from e
    return Torrent(
        name, b"files" in info, files, piece_length, pieces, hashlib.sha1(raw_info).hexdigest()
    )


def match(files: list[TorrentFile], local: list[Path]) -> dict[str, Path | None]:
    """Map each torrent file to a downloaded file: by SxxExx tag, else by size within 64 bytes.
    Ambiguous → None."""
    sizes = {p: p.stat().st_size for p in local}
    result: dict[str, Path | None] = {}
    for f in files:
        tag = EPISODE.search(PurePosixPath(f.path).name)
        found = [p for p in local if tag and _same_tag(tag.group(), p.name)] if tag else []
        if not found:
            found = [p for p in local if 0 <= sizes[p] - f.length <= SIZE_SLACK]
        result[f.path] = found[0] if len(found) == 1 else None
    claimed = [p for p in result.values() if p]
    # One downloaded file can back only one torrent file.
    return {k: None if claimed.count(v) > 1 else v for k, v in result.items()}


def _same_tag(tag: str, name: str) -> bool:
    return any(_norm(m.group()) == _norm(tag) for m in EPISODE.finditer(name))


def _norm(tag: str) -> tuple[int, int]:
    season, episode = re.findall(r"\d+", tag)
    return int(season), int(episode)


def check_padding(path: Path, length: int) -> str | None:
    """None if the extra bytes past `length` are all NUL, else an error code."""
    extra = path.stat().st_size - length
    if extra < 0:
        return "verify.too_short"
    if extra:
        with path.open("rb") as f:
            f.seek(length)
            while chunk := f.read(min(extra, 1 << 20)):
                if chunk.strip(b"\0"):  # IPTV providers append NUL padding only
                    return "verify.non_null_padding"
                extra -= len(chunk)
    return None


def is_annex(t: Torrent) -> dict[str, bool]:
    """Annexes: samples (a 'sample' word in the path, or a video < 1 % of the biggest one), and
    non-video files when they total less than 1 MiB."""
    videos = [f for f in t.files if PurePosixPath(f.path).suffix.lower() in VIDEO_EXTS]
    biggest = max((f.length for f in videos), default=0)
    others = [f for f in t.files if f not in videos]
    small_others = sum(f.length for f in others) < ANNEX_LIMIT
    return {
        f.path: bool(SAMPLE.search(f.path))
        or (f in videos and f.length < biggest / 100)
        or (f in others and small_others)
        for f in t.files
    }


def verify(
    t: Torrent,
    mapping: dict[str, Path | None],
    progress: Callable[[int, int], None] | None = None,
) -> tuple[list[dict], str]:
    """Hash every piece and give each file a status:
    identical | missing | too_short | different | unverified (no piece of it could be checked).
    Returns (per-file report, verdict: identical | identical_annex | partial | different)."""
    status: dict[str, str] = {}
    for f in t.files:
        local = mapping.get(f.path)
        if local is None:
            status[f.path] = "missing"
        elif err := check_padding(local, f.length):
            status[f.path] = "too_short" if err == "verify.too_short" else "different"
    readable = {f.path for f in t.files if f.path not in status}
    checked = dict.fromkeys(readable, 0)

    total = t.files[-1].offset + t.files[-1].length
    count = len(t.pieces) // 20
    handles: dict[Path, BinaryIO] = {}
    try:
        for k in range(count):
            if progress and k % 64 == 0:
                progress(k, count)
            start, end = k * t.piece_length, min((k + 1) * t.piece_length, total)
            spanned = [f for f in t.files if f.offset < end and start < f.offset + f.length]
            # A piece touching an unreadable file cannot be checked; its readable neighbours are
            # judged on their other pieces (qBittorrent re-checks the boundary piece anyway).
            if any(f.path not in readable for f in spanned):
                continue
            digest = hashlib.sha1()
            for f in spanned:
                path = mapping[f.path]
                fh = handles.get(path) or handles.setdefault(path, path.open("rb"))
                a, b = max(start, f.offset), min(end, f.offset + f.length)
                fh.seek(a - f.offset)
                while a < b and (chunk := fh.read(min(CHUNK, b - a))):
                    digest.update(chunk)
                    a += len(chunk)
                checked[f.path] += 1
            if digest.digest() != t.pieces[20 * k : 20 * k + 20]:
                for f in spanned:
                    status[f.path] = "different"
    finally:
        for fh in handles.values():
            fh.close()
    if progress:
        progress(count, count)
    for path, n in checked.items():
        if not n and path not in status:
            status[path] = "unverified"

    annex = is_annex(t)
    report = [
        {
            "path": f.path,
            "size": f.length,
            "status": status.get(f.path, "identical") if f.length else "identical",
            "annex": annex[f.path],
            "local": str(mapping[f.path]) if mapping.get(f.path) else None,
        }
        for f in t.files
    ]
    return report, _verdict(report)


def _verdict(report: list[dict]) -> str:
    main = [f for f in report if not f["annex"]]
    if main and all(f["status"] == "identical" for f in main):
        return "identical" if all(f["status"] == "identical" for f in report) else "identical_annex"
    if any(f["status"] == "identical" for f in main):
        return "partial"
    return "different"
