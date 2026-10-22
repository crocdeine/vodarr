"""AC-UI-06 (only tokens outside tokens.css), AC-UI-08 (AA contrast computed on token pairs)."""

import re
from pathlib import Path

import pytest

WEB = Path(__file__).parent.parent / "web"
TOKENS = (WEB / "styles" / "tokens.css").read_text()


def theme(selector: str) -> dict[str, str]:
    block = re.search(re.escape(selector) + r"\s*\{(.*?)\n\}", TOKENS, re.S).group(1)
    return dict(re.findall(r"--([\w-]+):\s*([^;]+);", block))


DARK = theme(":root")
LIGHT = {**DARK, **theme('[data-theme="light"]')}


def rgba(value: str) -> tuple[float, float, float, float]:
    value = value.strip()
    if value.startswith("#"):
        h = value[1:]
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0
    r, g, b, *a = (float(x) for x in re.findall(r"[\d.]+", value))
    return r, g, b, a[0] if a else 1.0


def over(fg, bg):
    r, g, b, a = fg
    return tuple(a * c + (1 - a) * d for c, d in zip((r, g, b), bg[:3], strict=True)) + (1.0,)


def mix(color, base, pct):
    return over((*color[:3], pct), base)


def luminance(c):
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (ch(v) for v in c[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


PAIRS = [
    ("text", "bg"),
    ("text", "surface"),
    ("text", "surface-2"),
    ("text-2", "bg"),
    ("text-2", "surface"),
    ("text-2", "surface-2"),
    ("text-3", "bg"),
    ("text-3", "surface"),
    ("accent-ink", "accent"),
    ("accent", "surface"),
    ("accent", "bg"),
]
STATUSES = ["ok", "running", "action", "error", "skipped"]


@pytest.mark.parametrize("name, t", [("dark", DARK), ("light", LIGHT)])
@pytest.mark.parametrize("fg, bg", PAIRS)
def test_text_pairs_are_aa(name, t, fg, bg):
    base = rgba(t[bg]) if bg not in ("bg",) else rgba(t["bg"])
    ratio = contrast(over(rgba(t[fg]), base), base)
    assert ratio >= 4.5, f"{name}: {fg} on {bg} = {ratio:.2f}"


@pytest.mark.parametrize("name, t", [("dark", DARK), ("light", LIGHT)])
@pytest.mark.parametrize("status", STATUSES)
def test_status_pills_are_aa(name, t, status):
    """A pill is the status colour on a 16 % tint of it over the surface."""
    surface = rgba(t["surface"])
    color = rgba(t[status])
    tint = mix(color, surface, float(t["tint"].rstrip("%")) / 100)
    assert contrast(color, tint) >= 4.5, f"{name}: {status} pill = {contrast(color, tint):.2f}"
    assert contrast(color, surface) >= 4.5, f"{name}: {status} on surface"


LITERAL = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(|\b\d+(\.\d+)?(px|rem|em|ms|s)\b")


@pytest.mark.parametrize(
    "path", sorted(p for p in (WEB / "styles").glob("*.css") if p.name != "tokens.css")
)
def test_components_use_only_tokens(path):
    """AC-UI-06"""
    css = re.sub(r"/\*.*?\*/", "", path.read_text(), flags=re.S)
    css = re.sub(r"@media[^{]*", "", css)  # media queries cannot read custom properties
    found = [m.group() for m in LITERAL.finditer(css)]
    assert found == [], f"hard-coded values in {path.name}: {found[:5]}"


@pytest.mark.parametrize("path", sorted(WEB.rglob("*.js")))
def test_scripts_set_no_inline_values(path):
    """AC-UI-06: styling stays in CSS; scripts never write colours or sizes."""
    js = path.read_text()
    assert not re.search(r"\.style\.(color|background|width|height|fontSize|margin|padding)\b", js)
    assert not re.search(r"#[0-9a-fA-F]{6}\b", js)


def test_every_used_token_is_defined():
    used, local = set(), set()
    for p in (WEB / "styles").glob("*.css"):
        used |= set(re.findall(r"var\(--([\w-]+)", p.read_text()))
        if p.name != "tokens.css":
            local |= set(re.findall(r"--([\w-]+):", p.read_text()))  # component-scoped (--c)
    for p in WEB.rglob("*.js"):
        used |= set(re.findall(r"var\(--([\w-]+)", p.read_text()))
    assert used - set(DARK) - local == set()
