"""AC-UI-05: en.json and fr.json have the same keys; every backend code is translated in both;
every {param} of an English text exists in the French one."""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
EN = json.loads((ROOT / "web" / "i18n" / "en.json").read_text())
FR = json.loads((ROOT / "web" / "i18n" / "fr.json").read_text())
NAMESPACES = (
    "analyse",
    "auth",
    "candidate",
    "crossseed",
    "download",
    "http",
    "pipeline",
    "queue",
    "release",
    "restore",
    "search",
    "seed",
    "settings",
    "source",
    "verify",
    "prowlarr",
)
SERVICES = ("search", "verify", "seed", "prowlarr")  # f"{service}.<reason>" in services.py


def backend_codes() -> set[str]:
    codes = {"internal"}
    for path in (ROOT / "src" / "vodarr").glob("*.py"):
        text = path.read_text()
        codes |= {
            m
            for m in re.findall(r"[\"']([a-z]+\.[a-z_]+)[\"']", text)
            if m.split(".")[0] in NAMESPACES
        }
        for reason in re.findall(r"f[\"']\{service\}\.([a-z_]+)[\"']", text):
            codes |= {f"{s}.{reason}" for s in SERVICES}
    return codes


def test_same_keys():
    assert set(EN) == set(FR)


def test_every_backend_code_is_translated():
    missing = sorted(backend_codes() - set(EN))
    assert missing == []


@pytest.mark.parametrize("key", sorted(EN))
def test_params_match(key):
    assert set(re.findall(r"\{(\w+)\}", EN[key])) == set(re.findall(r"\{(\w+)\}", FR[key]))


def test_no_empty_translation():
    assert [k for k, v in {**EN, **FR}.items() if not v.strip()] == []


def test_every_ui_key_used_in_scripts_exists():
    used = set()
    for path in (ROOT / "web").rglob("*.js"):
        used |= set(re.findall(r"\bt\(\s*[\"'`]([\w.]+)[\"'`]", path.read_text()))
    missing = [k for k in used if k not in EN and not {f"{k}.one", f"{k}.other"} <= set(EN)]
    assert sorted(missing) == []
