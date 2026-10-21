"""The installed package version and the ``User-Agent`` every request sends."""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from packaging.version import Version

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


def parse_version(v: str) -> Version:
    """Parse version string according to PEP 440."""
    from packaging.version import Version

    return Version(v)


def is_git_install() -> bool:
    """Return True if installed from git, False if from PyPI/wheel."""
    try:
        import json
        from importlib.metadata import distribution

        dist = distribution(DISTRIBUTION)
        raw = dist.read_text("direct_url.json")
        if raw:
            data = json.loads(raw)
            return "vcs_info" in data or "git" in str(data.get("url", ""))
    except Exception:
        return False
    return False


def check_latest_version(*, timeout: float = 3.0) -> str | None:
    """Check PyPI or GitHub for the latest release of ``vycari-pepper``.

    Returns the latest version string if newer than current, else ``None``.
    Never raises: network failures, timeouts, and unexpected responses return ``None``.
    """
    import os

    if os.environ.get("PEPPER_NO_UPDATE_CHECK", "").strip().lower() in ("1", "true", "yes"):
        return None
    current = get_version()
    if current == UNKNOWN_VERSION:
        return None
    try:
        import contextlib

        import httpx

        latest: str | None = None
        if is_git_install():
            resp = httpx.get(
                "https://api.github.com/repos/Vycari/pepper-cli/tags",
                timeout=timeout,
                headers={"User-Agent": USER_AGENT},
            )
            if resp.status_code == 200:
                tags = resp.json()
                if isinstance(tags, list):
                    best_ver = None
                    best_str = None
                    for t in tags:
                        name = str(t.get("name", "")).lstrip("v")
                        if _VERSION_RE.fullmatch(name):
                            with contextlib.suppress(Exception):
                                v_obj = parse_version(name)
                                if best_ver is None or v_obj > best_ver:
                                    best_ver = v_obj
                                    best_str = name
                    latest = best_str
        else:
            resp = httpx.get(
                f"https://pypi.org/pypi/{DISTRIBUTION}/json",
                timeout=timeout,
                headers={"User-Agent": USER_AGENT},
            )
            if resp.status_code == 200:
                data = resp.json()
                latest = str(data.get("info", {}).get("version") or "")

        if not latest or not _VERSION_RE.fullmatch(latest):
            return None
        if parse_version(latest) > parse_version(current):
            return latest
    except Exception:
        return None
    return None
