"""Shared fixtures for the pepper-cli tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from pepper_cli import credentials, render


@pytest.fixture(autouse=True)
def _reset_credentials_profile():
    """``cli.main`` selects a process-wide credentials profile; make sure one
    test's ``--profile`` never leaks into the next."""
    credentials.set_profile(None)
    yield
    credentials.set_profile(None)


@pytest.fixture(autouse=True)
def _no_pepper_json(monkeypatch: pytest.MonkeyPatch):
    """``PEPPER_JSON=1`` in the developer's shell would flip every ``--json``-capable
    command to JSON; tests that want it set it themselves."""
    monkeypatch.delenv("PEPPER_JSON", raising=False)


@pytest.fixture(autouse=True)
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``~`` and ``$XDG_CONFIG_HOME`` to a temp dir across all platforms.

    On POSIX, ``Path.home()`` checks ``$HOME``; on Windows it checks
    ``$USERPROFILE``. Both are redirected so no test reads or writes the user's
    real credentials file.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return tmp_path


@pytest.fixture(autouse=True)
def _patch_prompt_session_io(monkeypatch: pytest.MonkeyPatch):
    """Provide dummy IO on PromptSession so headless/Windows test runs don't require a console."""
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.input import DummyInput
        from prompt_toolkit.output import DummyOutput
    except ImportError:
        return

    orig_init = PromptSession.__init__

    def init_with_dummy_io(self: PromptSession, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("output", DummyOutput())
        kwargs.setdefault("input", DummyInput())
        orig_init(self, *args, **kwargs)

    monkeypatch.setattr(PromptSession, "__init__", init_with_dummy_io)


@pytest.fixture
def machine_zone(monkeypatch: pytest.MonkeyPatch):
    """Pin the zone timestamps render in, without touching the process ``TZ``.

    Patches ``render._local_tz`` (the one place the local zone is learned) to
    a ``zoneinfo.ZoneInfo``, so it works on Windows, where ``time.tzset`` does
    not exist (it needs the ``tzdata`` package there, a win32 runtime
    dependency). A UTC test machine can't tell "no zone conversion" from "a
    conversion that lands on the same offset"; a non-UTC zone can.
    """

    def install(name: str) -> None:
        zone = ZoneInfo(name)
        monkeypatch.setattr(render, "_local_tz", lambda: zone)

    return install
