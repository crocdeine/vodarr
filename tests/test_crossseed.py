import pytest

from vodarr import crossseed
from vodarr.core import MsgError

CONFIG = """
// cross-seed configuration
module.exports = {
    delay: 30,
    torznab: [process.env.TORZNAB_URL], // not ours
    linkDirs: ["/data/links", '/data/links2'],  // two volumes
    linkType: "hardlink",
    /* flatLinking: true, */
    flatLinking: false,
    linkCategory: 'cross-seed-link',
    action: "inject",
    url: "http://user:pass@qbit:8080", // never read
};
"""


def test_literal_values_extracted():
    """AC-SET-06"""
    out = crossseed.parse(CONFIG)
    assert out == {
        "values": {
            "linkDirs": ["/data/links", "/data/links2"],
            "linkType": "hardlink",
            "flatLinking": False,
            "linkCategory": "cross-seed-link",
        },
        "manual": [],
    }


def test_non_literal_values_are_manual():
    """AC-SET-06: variables, calls and concatenations are never evaluated."""
    text = """module.exports = {
        linkDirs: [process.env.LINKS],
        linkType: TYPE,
        flatLinking: !!process.env.FLAT,
        linkCategory: "cross-" + "seed",
    };"""
    out = crossseed.parse(text)
    assert out == {"values": {}, "manual": ["flatLinking", "linkCategory", "linkDirs", "linkType"]}


def test_template_with_interpolation_is_manual():
    out = crossseed.parse("module.exports = { linkType: `${t}` , linkCategory: `xs` };")
    assert out == {"values": {"linkCategory": "xs"}, "manual": ["linkType"]}


def test_old_single_link_dir():
    out = crossseed.parse('module.exports = { linkDir: "/data/links" };')
    assert out["values"] == {"linkDirs": ["/data/links"]}


def test_comments_inside_strings_kept():
    out = crossseed.parse('module.exports = { linkCategory: "a//b", linkType: "sym/*x*/link" };')
    assert out["values"] == {"linkCategory": "a//b", "linkType": "sym/*x*/link"}


def test_commented_keys_ignored():
    out = crossseed.parse('module.exports = {\n // linkType: "symlink",\n linkType: "reflink" };')
    assert out["values"] == {"linkType": "reflink"}


def test_file_checks(tmp_path):
    """AC-SET-06: ≤ 256 KiB, .js only."""
    big = tmp_path / "config.js"
    big.write_text("x" * (256 * 1024 + 1))
    with pytest.raises(MsgError) as err:
        crossseed.import_file(str(big))
    assert err.value.msg.code == "crossseed.file_too_large"
    with pytest.raises(MsgError):
        crossseed.import_file(str(tmp_path / "missing.js"))
    with pytest.raises(MsgError):
        crossseed.import_file("/etc/passwd")
    ok = tmp_path / "ok.js"
    ok.write_text(CONFIG)
    assert crossseed.import_file(str(ok))["values"]["linkType"] == "hardlink"


def test_top_level_value_wins_over_a_nested_one():
    """Bug hunt: a linkDirs inside a nested object beat the real top-level linkDir."""
    text = """module.exports = {
        nested: { linkDirs: ["/wrong"], linkType: "symlink" },
        linkDir: "/data/old",
        linkType: "hardlink",
    };"""
    out = crossseed.parse(text)["values"]
    assert out["linkDirs"] == ["/data/old"]
    assert out["linkType"] == "hardlink"
