"""The installed package version and the ``User-Agent`` every request sends."""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError, version

#: Distribution name on PyPI (the import package is ``pepper_cli``).
DISTRIBUTION = "vycari-pepper"

#: Reported when package metadata is missing, e.g. running from a bare source tree.
UNKNOWN_VERSION = "0+unknown"

# The characters a version string is made of. The version goes into an HTTP
# header, so anything else (whitespace, a line break, non-ASCII) is refused.
_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+!_-]{0,63}")


def get_version() -> str:
    """Installed ``vycari-pepper`` version, or :data:`UNKNOWN_VERSION`.

    Also :data:`UNKNOWN_VERSION` when the metadata holds something that is not
    shaped like a version, so the value is always safe in a header.
    """
    try:
        _found = version(DISTRIBUTION)
    except PackageNotFoundError:
        return UNKNOWN_VERSION
    return _found if _VERSION_RE.fullmatch(_found) else UNKNOWN_VERSION


#: Sent as ``User-Agent`` on every HTTP request the CLI makes.
USER_AGENT = f"pepper-cli/{get_version()}"


def parse_version(v: str) -> tuple[int, ...]:
    """Extract integer version components for tuple comparison."""
    parts = [int(p) for p in re.findall(r"\d+", v)]
    return tuple(parts) if parts else (0,)


def check_latest_version(*, timeout: float = 3.0) -> str | None:
    """Check PyPI for the latest release of ``vycari-pepper``.

    Returns the latest version string if newer than current, else ``None``.
    Never raises: network failures, timeouts, and unexpected responses return ``None``.
    """
    import os

    if os.environ.get("PEPPER_NO_UPDATE_CHECK", "").strip().lower() in ("1", "true", "yes"):
        return None
    current = os.environ.get("PEPPER_FORCE_CURRENT_VERSION") or get_version()
    if current == UNKNOWN_VERSION:
        return None
    try:
        import httpx

        resp = httpx.get(
            f"https://pypi.org/pypi/{DISTRIBUTION}/json",
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        latest = str(data.get("info", {}).get("version") or "")
        if not latest:
            return None
        if parse_version(latest) > parse_version(current):
            return latest
    except Exception:
        return None
    return None
