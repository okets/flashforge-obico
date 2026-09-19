"""The version the agent reports.

It was hardcoded here while pyproject.toml carried its own copy, so bumping the release left the
running agent introducing itself as the previous version -- in its startup log and in the page
footer, which is exactly where you look to find out what is running.
"""

import tomllib
from pathlib import Path

from flashforge_obico import VERSION


def test_version_matches_the_packaging_metadata():
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text())["project"]["version"]
    assert VERSION == declared
