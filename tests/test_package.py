import vodarr


def test_package_exposes_version():
    assert vodarr.__version__ == "0.3.0"
