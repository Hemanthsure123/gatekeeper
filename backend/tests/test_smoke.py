"""Smoke test: the package imports and its version matches the installed metadata."""

from importlib.metadata import version

import gatekeeper


def test_package_version_matches_metadata() -> None:
    assert gatekeeper.__version__ == "0.0.0"
    assert version("gatekeeper") == gatekeeper.__version__
