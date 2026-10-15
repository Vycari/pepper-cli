"""End-to-end tests for the ``pepper takeout`` subcommand.

Same approach as ``test_cli_todos.py``: invoke ``cli.main([...])`` with
a monkeypatched ``api_client.ApiClient`` so commands run without the
network, and exercise credentials through the real (tmp-home) module.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from pepper_cli import api_client, cli, credentials
from pepper_cli.secret import SecretStr


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return tmp_path


class _FakeTakeoutApi:
    """Records calls and returns a canned ``out_path`` (or raises)."""

    instances: list[_FakeTakeoutApi] = []

    def __init__(self, *, server_url, token, **canned) -> None:
        self.server_url = server_url
        self.token = token
        self._canned = canned
        self.calls: list[tuple] = []
        _FakeTakeoutApi.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def download_takeout(self, *, out_path=None):
        self.calls.append(("download_takeout", out_path))
        result = self._canned.get("result")
        if isinstance(result, Exception):
            raise result
        if result is None:
            raise AssertionError("test forgot to set result")
        Path(result).write_bytes(b"zip")
        return result


@pytest.fixture
def patch_api(monkeypatch):
    _FakeTakeoutApi.instances.clear()

    def install(**canned):
        def factory(**kwargs):
            return _FakeTakeoutApi(**canned, **kwargs)

        monkeypatch.setattr(api_client, "ApiClient", factory)

    return install


def _login(tmp_home: Path) -> None:
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))


def test_takeout_writes_to_disk_and_reports_path(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(result=str(tmp_home / "pepper-takeout-alice-20260721T000000Z.zip"))
    rc = cli.main(["takeout"])
    assert rc == 0
    assert _FakeTakeoutApi.instances[-1].calls == [("download_takeout", None)]
    # The console wraps a long path at the terminal width (80 columns in CI).
    err = "".join(capsys.readouterr().err.split())
    assert "pepper-takeout-alice-20260721T000000Z.zip" in err


def test_takeout_passes_out_flag(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(result=str(tmp_home / "mine.zip"))
    rc = cli.main(["takeout", "--out", "mine.zip"])
    assert rc == 0
    assert _FakeTakeoutApi.instances[-1].calls == [("download_takeout", "mine.zip")]


def test_takeout_without_credentials_errors(tmp_home, patch_api) -> None:
    patch_api(result="pepper-takeout.zip")
    rc = cli.main(["takeout"])
    assert rc == 1
    assert _FakeTakeoutApi.instances == []


def test_takeout_http_error_reports_status(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(result=api_client.ApiError(status=404, body="", detail="not found"))
    rc = cli.main(["takeout"])
    assert rc == 1
    assert "404" in capsys.readouterr().err


def test_takeout_handles_unreachable_server(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(result=httpx.ConnectError("boom"))
    rc = cli.main(["takeout"])
    assert rc == 1
    assert "Couldn't reach" in capsys.readouterr().err
