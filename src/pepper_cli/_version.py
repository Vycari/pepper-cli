"""The installed package version and the ``User-Agent`` every request sends."""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

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
    """Return True if installed from an unpinned git branch/commit, False otherwise."""
    try:
        import json
        from importlib.metadata import distribution

        dist = distribution(DISTRIBUTION)
        raw = dist.read_text("direct_url.json")
        if not raw:
            return False
        data = json.loads(raw)
        if not isinstance(data, dict):
            return False
        if "dir_info" in data:
            return False
        vcs_info = data.get("vcs_info")
        if not isinstance(vcs_info, dict):
            return False
        if vcs_info.get("requested_revision"):
            return False
        return vcs_info.get("vcs") == "git"
    except Exception:
        return False


def uv_tool_requirement() -> str | None:
    """The requirement ``uv tool install`` recorded for this package, pin included.

    ``None`` when this is not a uv tool install, the receipt cannot be read, or it lists no
    requirement for this distribution.
    """
    import sys
    import tomllib
    from pathlib import Path

    from packaging.utils import canonicalize_name

    receipt = Path(sys.prefix) / "uv-receipt.toml"
    try:
        with receipt.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    tool = data.get("tool")
    requirements = tool.get("requirements") if isinstance(tool, dict) else None
    if not isinstance(requirements, list):
        return None
    for entry in requirements:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        if canonicalize_name(entry["name"].split("[")[0]) != canonicalize_name(DISTRIBUTION):
            continue
        specifier = entry.get("specifier")  # singular: uv's receipt and lock serialization
        return entry["name"] + (specifier if isinstance(specifier, str) else "")
    return None


def is_pinned(requirement: str) -> bool:
    """Whether ``uv tool upgrade`` would stay where it is.

    A floor (``>=0.3``) and an exclusion (``!=0.2``) both still let it move up, so neither is
    a pin; anything that names a version to stay at, or a range to stay in, is.
    """
    from packaging.requirements import InvalidRequirement, Requirement

    try:
        return any(spec.operator not in (">=", "!=") for spec in Requirement(requirement).specifier)
    except InvalidRequirement:
        return False


def pinned_below(latest: str) -> str | None:
    """The recorded requirement that keeps this install off ``latest``, or ``None``.

    A specifier that admits ``latest`` can take it with ``uv tool upgrade``; one that excludes
    it makes the offer a loop, because the upgrade keeps the pin from the receipt (#127).
    """
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.version import InvalidVersion

    requirement = uv_tool_requirement()
    if requirement is None:
        return None
    try:
        return (
            None
            if Requirement(requirement).specifier.contains(latest, prereleases=True)
            else requirement
        )
    except (InvalidRequirement, InvalidVersion):
        return None


def _cache_path() -> Path:
    import os
    from pathlib import Path

    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "pepper" / "update-check.json"


def _read_cached_version(*, cache_ttl: float) -> tuple[bool, str | None]:
    """Read cached latest version if within TTL.

    Returns (hit, version) where hit is True if within TTL, False if missing/expired.
    """
    import json
    import time

    cache_file = _cache_path()
    try:
        if cache_file.is_file():
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            last_checked = float(data.get("last_checked", 0))
            if 0 <= time.time() - last_checked < cache_ttl:
                ver = data.get("latest_version")
                return True, (str(ver) if ver else None)
    except Exception:
        return False, None
    return False, None


def _write_cached_version(version: str) -> None:
    """Write check timestamp and detected version to cache."""
    import contextlib
    import json
    import time

    with contextlib.suppress(Exception):
        cache_file = _cache_path()
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"last_checked": time.time(), "latest_version": version})
        cache_file.write_text(payload, encoding="utf-8")


def check_latest_version(
    *,
    timeout: float = 3.0,
    cache_ttl: float = 86400.0,
    should_stop: Callable[[], bool] | None = None,
) -> str | None:
    """Check PyPI or GitHub for the latest release of ``vycari-pepper``.

    Returns the latest version string if newer than current, else ``None``.
    Never raises: network failures, timeouts, and unexpected responses return ``None``.
    ``should_stop`` abandons the check between requests when the caller is closing.
    """
    import os

    if should_stop is not None and should_stop():
        return None
    if os.environ.get("PEPPER_NO_UPDATE_CHECK", "").strip().lower() in ("1", "true", "yes"):
        return None
    current = get_version()
    if current == UNKNOWN_VERSION:
        return None

    if cache_ttl > 0:
        hit, cached = _read_cached_version(cache_ttl=cache_ttl)
        if hit:
            if (
                cached is not None
                and _VERSION_RE.fullmatch(cached)
                and parse_version(cached) > parse_version(current)
            ):
                return cached
            return None

    try:
        import contextlib

        import httpx

        latest: str | None = None
        if is_git_install():
            if should_stop is not None and should_stop():
                return None
            resp = httpx.get(
                "https://api.github.com/repos/Vycari/pepper-cli/releases/latest",
                timeout=timeout,
                headers={"User-Agent": USER_AGENT},
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    name = str(data.get("tag_name") or "").lstrip("v")
                    if _VERSION_RE.fullmatch(name):
                        latest = name
            if not latest:
                if should_stop is not None and should_stop():
                    return None
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
            if not latest:
                if should_stop is not None and should_stop():
                    return None
                resp = httpx.get(
                    f"https://pypi.org/pypi/{DISTRIBUTION}/json",
                    timeout=timeout,
                    headers={"User-Agent": USER_AGENT},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    latest = str(data.get("info", {}).get("version") or "")
        else:
            if should_stop is not None and should_stop():
                return None
            resp = httpx.get(
                f"https://pypi.org/pypi/{DISTRIBUTION}/json",
                timeout=timeout,
                headers={"User-Agent": USER_AGENT},
            )
            if resp.status_code == 200:
                data = resp.json()
                latest = str(data.get("info", {}).get("version") or "")

        if should_stop is not None and should_stop():
            return None
        if latest and _VERSION_RE.fullmatch(latest):
            if cache_ttl > 0:
                _write_cached_version(latest)
            if parse_version(latest) > parse_version(current):
                return latest
            return None
    except Exception:
        return None
    return None
