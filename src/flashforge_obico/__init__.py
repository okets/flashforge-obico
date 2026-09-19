"""The agent's version, taken from the installed package metadata.

pyproject.toml is the only place it is written. It used to be repeated here too, so a release bump
left the agent reporting the previous version in its startup log and in the console footer. The
fallback covers running from a source tree that was never installed.
"""

from importlib.metadata import PackageNotFoundError, version as _version

try:
    VERSION = _version("flashforge-obico")
except PackageNotFoundError:  # a source checkout with no install
    VERSION = "0.0.0+dev"
