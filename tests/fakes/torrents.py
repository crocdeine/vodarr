"""Synthetic .torrent factory: random content, v1 multi/single-file, v2-only, padding."""

import hashlib
import random
from pathlib import Path


def bencode(value) -> bytes:
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, list):
        return b"l" + b"".join(bencode(v) for v in value) + b"e"
    if isinstance(value, dict):
        items = sorted((k.encode() if isinstance(k, str) else k, v) for k, v in value.items())
        return b"d" + b"".join(bencode(k) + bencode(v) for k, v in items) + b"e"
    raise TypeError(type(value))


def random_bytes(size: int, seed: int) -> bytes:
    return random.Random(seed).randbytes(size)


def pieces(data: bytes, piece_length: int) -> bytes:
    return b"".join(
        hashlib.sha1(data[i : i + piece_length]).digest() for i in range(0, len(data), piece_length)
    )


def make_info(name: str, files: list[tuple[str, bytes]], piece_length: int = 16384) -> dict:
    """`files` = [(path inside the torrent, content)]; one file whose path == name → single-file."""
    blob = b"".join(content for _, content in files)
    info = {"name": name, "piece length": piece_length, "pieces": pieces(blob, piece_length)}
    if len(files) == 1 and files[0][0] == name:
        info["length"] = len(files[0][1])
    else:
        info["files"] = [{"path": p.split("/"), "length": len(c)} for p, c in files]
    return info


def make_torrent(name: str, files: list[tuple[str, bytes]], piece_length: int = 16384) -> bytes:
    return bencode(
        {
            "announce": "http://tracker.invalid/announce",
            "info": make_info(name, files, piece_length),
        }
    )


def make_v2_only_torrent(name: str) -> bytes:
    info = {"name": name, "piece length": 16384, "meta version": 2, "file tree": {}}
    return bencode({"info": info, "piece layers": {}})


def write_local(path: Path, content: bytes, padding: bytes = b"") -> Path:
    """Write a 'downloaded' file: original content plus optional trailing padding."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content + padding)
    return path
