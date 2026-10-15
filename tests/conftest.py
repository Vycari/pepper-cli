"""Shared fixtures for the pepper-cli tests."""

from __future__ import annotations

import pytest

from pepper_cli import credentials


@pytest.fixture(autouse=True)
def _reset_credentials_profile():
    """``cli.main`` selects a process-wide credentials profile; make sure one
    test's ``--profile`` never leaks into the next."""
    credentials.set_profile(None)
    yield
    credentials.set_profile(None)


@pytest.fixture(autouse=True)
def _no_pepper_json(monkeypatch):
    """``PEPPER_JSON=1`` in the developer's shell would flip every ``--json``-capable
    command to JSON; tests that want it set it themselves."""
    monkeypatch.delenv("PEPPER_JSON", raising=False)
