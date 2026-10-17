"""``pepper following search/create``: the numbered candidates the server found.

``search`` runs the app's ``search`` action (``{"yours": [...], "new": [...]}``); the numbered
list is saved per profile in ``following-search.json`` next to the credentials, so
``following create N`` can follow candidate N later. The file holds only what ``follow`` needs
(``ref``, ``payload``) and what the list showed. Nothing secret is in it. The CLI never picks
a candidate for typed text: ``create`` takes the number the person chose.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pepper_cli import credentials

STATE_NAME = "following-search.json"


def state_path() -> Path:
    return credentials.credentials_path().parent / STATE_NAME


def numbered(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Your existing follows first, then new matches, numbered from 1 across both."""
    rows = [
        c
        for key in ("yours", "new")
        for c in (result.get(key) or [])
        if isinstance(c, dict) and isinstance(c.get("ref"), str)
    ]
    return [{"n": n, **c} for n, c in enumerate(rows, 1)]


def save(query: str, candidates: list[dict[str, Any]]) -> None:
    """Write the list atomically with owner-only permissions; failure to save is not fatal."""
    path = state_path()
    tmp: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=STATE_NAME, suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            json.dump({"query": query, "candidates": candidates}, fh)
        Path(tmp).chmod(0o600)
        Path(tmp).replace(path)
    except OSError:
        if tmp:
            with contextlib.suppress(OSError):
                Path(tmp).unlink()


def load() -> tuple[str, list[dict[str, Any]]] | None:
    """The saved ``(query, candidates)``, or ``None`` when absent or unreadable."""
    try:
        doc = json.loads(state_path().read_text())
        rows = [c for c in doc["candidates"] if isinstance(c, dict) and c.get("n")]
        return str(doc.get("query") or ""), rows
    except (OSError, ValueError, KeyError, TypeError):
        return None


def pick(candidates: list[dict[str, Any]], n: int) -> dict[str, Any] | None:
    return next((c for c in candidates if c.get("n") == n), None)
