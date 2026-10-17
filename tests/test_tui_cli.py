"""``pepper tui`` at the command line: terminal checks, exit codes, and the lazy import."""

from __future__ import annotations

import subprocess
import sys
import textwrap

import httpx
import pytest

from pepper_cli import cli, credentials, tui
from pepper_cli.provider.fs import FilesystemProvider
from pepper_cli.secret import SecretStr


def _tty(monkeypatch: pytest.MonkeyPatch, *, stdin: bool, stdout: bool) -> None:
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: stdin)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: stdout, raising=False)


@pytest.mark.parametrize(("stdin", "stdout"), [(False, True), (True, False), (False, False)])
def test_without_a_terminal_it_exits_2_with_one_stderr_line(
    monkeypatch, capsys, stdin, stdout
) -> None:
    ran = []
    monkeypatch.setattr(tui, "run", lambda **kw: ran.append(kw) or 0)
    _tty(monkeypatch, stdin=stdin, stdout=stdout)
    monkeypatch.setenv("PEPPER_JSON", "1")  # the message is for a person, never JSON
    assert cli.main(["tui"]) == 2
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1
    assert "interactive terminal" in err[0]
    assert not err[0].startswith("{")
    assert ran == []


def test_without_credentials_it_exits_1(monkeypatch, capsys) -> None:
    _tty(monkeypatch, stdin=True, stdout=True)
    monkeypatch.setattr(tui, "run", lambda **kw: pytest.fail("must not start"))
    assert cli.main(["tui"]) == 1
    assert capsys.readouterr().err.strip()


def test_it_starts_the_app_with_the_stored_credentials(monkeypatch) -> None:
    _tty(monkeypatch, stdin=True, stdout=True)
    creds = credentials.Credentials(server_url="http://example.com", token=SecretStr("pep_pat_x"))
    monkeypatch.setattr(credentials, "load", lambda: creds)
    seen = {}

    def fake_run(*, server_url: str, token: SecretStr, provider: object = None) -> int:
        seen.update(server_url=server_url, token=token, provider=provider)
        return 0

    monkeypatch.setattr(tui, "run", fake_run)
    assert cli.main(["tui"]) == 0
    assert seen == {"server_url": "http://example.com", "token": creds.token, "provider": None}
    monkeypatch.setattr(tui, "run", lambda **kw: 1)
    assert cli.main(["tui"]) == 1


@pytest.mark.parametrize("flag", ["--root", "--tools-root"])
def test_root_shares_a_folder_with_the_dm_turns(monkeypatch, tmp_path, flag) -> None:
    _tty(monkeypatch, stdin=True, stdout=True)
    creds = credentials.Credentials(server_url="http://example.com", token=SecretStr("pep_pat_x"))
    monkeypatch.setattr(credentials, "load", lambda: creds)
    seen = {}
    monkeypatch.setattr(tui, "run", lambda **kw: seen.update(kw) or 0)
    assert cli.main(["tui", flag, str(tmp_path)]) == 0
    provider = seen["provider"]
    assert isinstance(provider, FilesystemProvider)
    assert provider.manifest()  # the tools ride on each DM turn


def test_a_refused_root_is_one_line_and_exit_2(monkeypatch, tmp_path, capsys) -> None:
    _tty(monkeypatch, stdin=True, stdout=True)
    creds = credentials.Credentials(server_url="http://example.com", token=SecretStr("pep_pat_x"))
    monkeypatch.setattr(credentials, "load", lambda: creds)
    monkeypatch.setattr(tui, "run", lambda **kw: pytest.fail("must not start"))
    assert cli.main(["tui", "--root", str(tmp_path / "missing")]) == 2
    # One error, which Rich wraps at the console's width (80 columns off a terminal), so
    # where the line breaks depends on the length of the temporary path: compare it unwrapped.
    err = " ".join(capsys.readouterr().err.split())
    assert err.startswith("error: --root:")
    assert err.endswith("missing is not a directory")


def test_a_network_error_is_one_line_and_exit_1(monkeypatch, capsys) -> None:
    _tty(monkeypatch, stdin=True, stdout=True)
    creds = credentials.Credentials(server_url="http://example.com", token=SecretStr("pep_pat_x"))
    monkeypatch.setattr(credentials, "load", lambda: creds)

    def boom(**_: object) -> int:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(tui, "run", boom)
    assert cli.main(["tui"]) == 1
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1
    assert "pep_pat_x" not in err[0]


def test_importing_the_cli_does_not_import_textual() -> None:
    code = textwrap.dedent(
        """
        import sys
        import pepper_cli.cli
        import pepper_cli.tui
        assert "textual" not in sys.modules, "textual was imported eagerly"
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
