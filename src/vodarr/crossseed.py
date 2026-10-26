"""Read cross-seed's config.js without executing it: literal values only (ADR-0006, AC-SET-06)."""

import re
from pathlib import Path

from vodarr.core import MsgError

MAX_SIZE = 256 * 1024
KEYS = ("linkDirs", "linkDir", "linkType", "flatLinking", "linkCategory")
STRING = r"""'(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*"|`[^`$]*`"""
VALUE = re.compile(
    rf"\s*(?P<v>{STRING}|true|false|\[\s*(?:(?:{STRING})\s*,\s*)*(?:{STRING})?\s*,?\s*\])\s*[,}}\n]"
)


def strip_comments(text: str) -> str:
    """Remove // and /* */ comments, leaving string contents alone."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c in "'\"`":
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            out.append(text[i : j + 1])
            i = j + 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            out.append(" ")
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _string(literal: str) -> str:
    body = literal[1:-1]
    return re.sub(r"\\(.)", r"\1", body) if literal[0] != "`" else body


def _literal(raw: str):
    if raw in ("true", "false"):
        return raw == "true"
    if raw.startswith("["):
        return [_string(m.group()) for m in re.finditer(STRING, raw)]
    return _string(raw)


def _depths(code: str) -> list[int]:
    """Brace depth at each position, string contents ignored."""
    out, depth, i, n = [0] * (len(code) + 1), 0, 0, len(code)
    while i < n:
        c = code[i]
        out[i] = depth
        if c in "'\"`":
            j = i + 1
            while j < n and code[j] != c:
                j += 2 if code[j] == "\\" else 1
            for k in range(i, min(j + 1, n)):
                out[k] = depth
            i = j + 1
            continue
        depth += (c == "{") - (c == "}")
        i += 1
    return out


def parse(text: str) -> dict:
    """{"values": {key: value}, "manual": [keys present but not literal]}. A key set in a
    nested object (a per-tracker override, an example) loses to the top-level one."""
    code = strip_comments(text)
    depth = _depths(code)
    values, manual, level = {}, [], {}
    for key in KEYS:
        found = list(re.finditer(rf"(?<![\w$.])['\"]?{key}['\"]?\s*:", code))
        if not found:
            continue
        m = min(found, key=lambda f: depth[f.start()])  # shallowest, first on ties
        level[key] = depth[m.start()]
        lit = VALUE.match(code, m.end())
        if lit:
            values[key] = _literal(lit.group("v"))
        else:
            manual.append(key)
    if "linkDir" in values:  # older cross-seed: a single linkDir
        if level["linkDir"] < level.get("linkDirs", 1 << 30):
            values["linkDirs"] = [values["linkDir"]]
            manual = [k for k in manual if k != "linkDirs"]
        del values["linkDir"]
    if "linkDir" in manual:
        manual = [k for k in manual if k != "linkDir"] + (
            ["linkDirs"] if "linkDirs" not in values else []
        )
    return {"values": values, "manual": sorted(set(manual))}


def import_file(path: str) -> dict:
    p = Path(path)
    if p.suffix != ".js" or not p.is_file():
        raise MsgError("crossseed.file_not_found", path=path)
    if p.stat().st_size > MAX_SIZE:
        raise MsgError("crossseed.file_too_large", limitKb=MAX_SIZE // 1024)
    return parse(p.read_text(encoding="utf-8", errors="replace"))
