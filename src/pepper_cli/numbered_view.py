"""A numbered listing remembered per profile, so ``show 3`` means "the third row I just saw".

``events_view`` and ``dash_view`` each keep their own copy of this idea; the account commands
(``artifacts``, ``booking-pages``) share this one. The listing is saved next to the credentials
of the active profile (``credentials.credentials_path``), one small JSON file per noun, so two
profiles never see each other's numbers. A reference that is not a short number is an id, a
unique prefix of an id the listing held, or (where the noun has one) another unique key such as
a slug; an id the listing never held is passed through as given.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, NamedTuple

from pepper_cli import credentials

_MAX_N_DIGITS = 4  # a longer all-digit ref is (a prefix of) an id, not a number


class RefError(Exception):
    """A bad reference. ``code`` is the J8 error code."""

    def __init__(self, message: str, code: str = "usage") -> None:
        super().__init__(message)
        self.code = code


class SaveProblem(NamedTuple):
    """A listing that could not be saved. ``stale`` is True when an older listing is still on
    disk (it could not be removed either), so a number would resolve against that one."""

    message: str
    stale: bool


def write_state(path: Path, doc: dict[str, Any]) -> SaveProblem | None:
    """Write ``doc`` to ``path`` atomically, owner-only, through a temp file no other process
    shares. On failure the previous file is removed, so a later number cannot resolve against
    a list that was not the one just shown; ``None`` means saved."""
    tmp: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        Path(tmp).chmod(0o600)
        Path(tmp).replace(path)
        return None
    except OSError as exc:
        if tmp:
            with contextlib.suppress(OSError):
                Path(tmp).unlink()
        why = f"couldn't save the numbered list to {path} ({exc.strerror or exc})"
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return SaveProblem(f"{why}, and the previous list there could not be removed", True)
        return SaveProblem(why, False)


def forget(path: Path) -> None:
    """Remove a saved listing (sign-out, sign-in); a missing file is fine."""
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


class NumberedView:
    def __init__(self, state_name: str, noun: str, list_command: str) -> None:
        self.state_name = state_name
        self.noun = noun  # singular, for messages: "artifact"
        self.list_command = list_command  # "pepper artifacts list"

    def state_path(self) -> Path:
        return credentials.credentials_path().with_name(self.state_name)

    def save(
        self, rows: list[dict[str, Any]], meta: dict[str, Any] | None = None
    ) -> SaveProblem | None:
        """Remember ``rows`` (each with an ``id``) and ``meta`` (what the listing was scoped to).
        See :func:`write_state` for what a failure leaves behind."""
        return write_state(self.state_path(), {"rows": rows, "meta": meta or {}})

    def load(self) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
        try:
            state = json.loads(self.state_path().read_text(encoding="utf-8"))
            rows, meta = state["rows"], state.get("meta") or {}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None
        if not isinstance(rows, list) or not isinstance(meta, dict):
            return None
        return [r for r in rows if isinstance(r, dict) and r.get("id")], meta

    @staticmethod
    def is_number(ref: str) -> bool:
        ref = ref.strip()
        return ref.isdigit() and len(ref) <= _MAX_N_DIGITS

    def resolve(
        self, ref: str, *, alt_keys: tuple[str, ...] = ()
    ) -> tuple[str, dict[str, Any] | None, dict[str, Any]]:
        """``(id, row, meta)``: ``row`` is what the last listing held (``None`` for an id it
        never held, which is passed on as given); ``meta`` is how that listing was scoped."""
        ref = ref.strip()
        if not ref:
            raise RefError(f"name a {self.noun}: a number from '{self.list_command}', or an id")
        loaded = self.load()
        rows, meta = loaded if loaded is not None else (None, {})
        if self.is_number(ref):
            if rows is None:
                raise RefError(
                    f"no numbered list to follow: run '{self.list_command}' first (got {ref})",
                    "not_found",
                )
            n = int(ref)
            if not 1 <= n <= len(rows):
                raise RefError(
                    f"no {self.noun} {n} in the last '{self.list_command}' (it listed {len(rows)})",
                    "not_found",
                )
            return str(rows[n - 1]["id"]), rows[n - 1], meta
        held = rows or []
        for key in ("id", *alt_keys):
            exact = [r for r in held if str(r.get(key)) == ref]
            if len(exact) == 1:
                return str(exact[0]["id"]), exact[0], meta
            if len(exact) > 1:
                raise RefError(
                    f"{ref!r} matches more than one {self.noun} in the last listing", "ambiguous"
                )
        prefixed = [r for r in held if str(r["id"]).startswith(ref)]
        if len(prefixed) > 1:
            raise RefError(
                f"{ref!r} matches more than one {self.noun} in the last listing", "ambiguous"
            )
        if prefixed:
            return str(prefixed[0]["id"]), prefixed[0], meta
        return ref, None, {}


def number(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copies of ``rows`` with ``n`` (1-based) first."""
    return [{"n": i, **row} for i, row in enumerate(rows, 1)]
