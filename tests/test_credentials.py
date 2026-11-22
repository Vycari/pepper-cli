"""Tests for the on-disk credentials store.

Mostly POSIX-only behaviors (perm enforcement, file mode after save).
The Windows skip path is exercised by an explicit guard test.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from pepper_cli import credentials
from pepper_cli.secret import SecretStr


def test_credentials_path_is_isolated_to_tmp_home(tmp_home: Path) -> None:
    """Ensure Path.home() and credentials path are safely within tmp_home on all platforms."""
    assert Path.home() == tmp_home
    assert str(credentials.credentials_path()).startswith(str(tmp_home))


def test_save_then_load_roundtrip(tmp_home: Path) -> None:
    path = credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    assert path == credentials.credentials_path()
    creds = credentials.load()
    assert creds.server_url == "http://example.com"
    assert creds.token.get_secret_value() == "pep_pat_abc"


def test_save_strips_trailing_slash_on_load(tmp_home: Path) -> None:
    # ``save`` writes whatever it gets; ``load`` strips the trailing
    # slash so the caller doesn't build ``//api/v1/...``.
    credentials.save(server_url="http://example.com/", token=SecretStr("pep_pat_abc"))
    creds = credentials.load()
    assert creds.server_url == "http://example.com"


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX-only perms check")
def test_save_writes_0600(tmp_home: Path) -> None:
    path = credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX-only perms check")
def test_load_refuses_world_readable(tmp_home: Path) -> None:
    path = credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    # Widen perms past 0600 — simulates a leaked dotfile.
    path.chmod(0o644)
    with pytest.raises(credentials.CredentialsError, match="0600"):
        credentials.load()


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX-only perms check")
def test_load_refuses_group_readable(tmp_home: Path) -> None:
    path = credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    path.chmod(0o640)
    with pytest.raises(credentials.CredentialsError):
        credentials.load()


def test_load_missing_file_errors(tmp_home: Path) -> None:
    with pytest.raises(credentials.CredentialsError, match="No credentials"):
        credentials.load()


def test_load_malformed_toml_errors(tmp_home: Path) -> None:
    path = credentials.credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this = is = not = toml\n")
    path.chmod(0o600)
    with pytest.raises(credentials.CredentialsError, match="not valid TOML"):
        credentials.load()


def test_load_invalid_utf8_at_beginning_errors(tmp_home: Path) -> None:
    path = credentials.credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xffserver_url = 'http://example.com'\n")
    path.chmod(0o600)
    with pytest.raises(credentials.CredentialsError, match="not valid UTF-8") as exc_info:
        credentials.load()
    assert isinstance(exc_info.value.__cause__, UnicodeDecodeError)


def test_load_invalid_utf8_inside_toml_errors(tmp_home: Path) -> None:
    path = credentials.credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'server_url = "http://example.com"\ntoken = "secret_\xff_pat"\n')
    path.chmod(0o600)
    with pytest.raises(credentials.CredentialsError, match="not valid UTF-8") as exc_info:
        credentials.load()
    assert isinstance(exc_info.value.__cause__, UnicodeDecodeError)
    assert "secret_" not in str(exc_info.value)


def test_load_missing_keys_errors(tmp_home: Path) -> None:
    path = credentials.credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('server_url = "http://example.com"\n')
    path.chmod(0o600)
    with pytest.raises(credentials.CredentialsError, match="missing 'server_url' and/or 'token'"):
        credentials.load()


def test_delete_removes_file(tmp_home: Path) -> None:
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    assert credentials.delete() is True
    assert not credentials.credentials_path().exists()
    # Second delete is a no-op (False), not an error.
    assert credentials.delete() is False


def test_save_replaces_existing(tmp_home: Path) -> None:
    credentials.save(server_url="http://old.example.com", token=SecretStr("pep_pat_old"))
    credentials.save(server_url="http://new.example.com", token=SecretStr("pep_pat_new"))
    creds = credentials.load()
    assert creds.server_url == "http://new.example.com"
    assert creds.token.get_secret_value() == "pep_pat_new"


def test_xdg_config_home_honored(tmp_path, monkeypatch) -> None:
    """When ``$XDG_CONFIG_HOME`` is set, credentials live there, not
    in ``~/.config``. Important for users who relocate their config
    tree (and for hermetic CI environments)."""
    xdg = tmp_path / "xdg_config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv("HOME", str(tmp_path / "home_should_be_ignored"))
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    assert credentials.credentials_path() == xdg / "pepper" / "credentials.toml"
    assert credentials.credentials_path().exists()


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX-only perms check")
def test_save_special_chars_in_token_round_trip(tmp_home: Path) -> None:
    """Backslash and quote escape paths in the hand-written TOML
    serializer — tokens shouldn't contain these in practice but
    defensive coverage stops a future format change from silently
    corrupting existing tokens."""
    weird = 'pep_pat_with"\\quotes'
    credentials.save(server_url="http://example.com", token=SecretStr(weird))
    creds = credentials.load()
    assert creds.token.get_secret_value() == weird


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX-only perms check")
def test_save_creates_parent_dir_with_700(tmp_home: Path) -> None:
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    parent_mode = credentials.credentials_path().parent.stat().st_mode & 0o777
    # Either 0700 (if our chmod succeeded) or whatever umask gave us
    # plus our chmod — the contract is "owner-only or tighter."
    assert parent_mode & stat.S_IRWXG == 0
    assert parent_mode & stat.S_IRWXO == 0


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows-only behavior")
def test_load_does_not_check_perms_on_windows(tmp_home: Path) -> None:
    """On Windows the POSIX perm bits are meaningless — ``_check_perms``
    is a no-op. The test guards against accidentally re-introducing
    the check there."""
    path = credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    os.chmod(path, 0o644)  # would fail on POSIX
    creds = credentials.load()  # must succeed
    assert creds.token.get_secret_value() == "pep_pat_abc"


def test_profile_path_nests_under_profiles(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    default = credentials.credentials_path()
    assert default == tmp_path / "xdg" / "pepper" / "credentials.toml"
    credentials.set_profile("claude-code")
    assert credentials.active_profile() == "claude-code"
    assert (
        credentials.credentials_path()
        == tmp_path / "xdg" / "pepper" / "profiles" / "claude-code" / "credentials.toml"
    )
    credentials.set_profile(None)
    assert credentials.credentials_path() == default


def test_profile_save_load_delete_leave_default_untouched(tmp_home: Path) -> None:
    credentials.save(server_url="http://own.example", token=SecretStr("pep_pat_own"))
    default = credentials.credentials_path()
    credentials.set_profile("agent")
    path = credentials.save(server_url="http://agent.example", token=SecretStr("pep_agt_x"))
    assert path != default
    assert credentials.load().token.get_secret_value() == "pep_agt_x"
    if not sys.platform.startswith("win"):
        assert path.stat().st_mode & 0o777 == 0o600
    assert credentials.delete() is True
    assert not path.exists()
    credentials.set_profile(None)
    assert credentials.load().token.get_secret_value() == "pep_pat_own"


@pytest.mark.parametrize(
    "name",
    ["", ".", "..", "../x", "a/b", "a\\b", "a b", "x\n", "/abs", "é", "-", "-x", "--profile"],
)
def test_invalid_profile_names_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="Invalid profile name"):
        credentials.set_profile(name)
    assert credentials.active_profile() is None


@pytest.mark.parametrize("name", ["claude-code", "a.b", "A_1", "...", ".hidden", "a-", "_-x"])
def test_valid_profile_names_accepted(name: str) -> None:
    credentials.set_profile(name)
    assert credentials.active_profile() == name


# ---------------------------------------------------------------------------
# Atomic replace (#295): a failed write never costs the last good login.


def _old_login_survives() -> None:
    creds = credentials.load()
    assert creds.server_url == "https://old.example.com"
    assert creds.token.get_secret_value() == "pep_pat_old"


def test_save_keeps_old_credentials_when_the_write_fails(tmp_home: Path, monkeypatch) -> None:
    credentials.save(server_url="https://old.example.com", token=SecretStr("pep_pat_old"))

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(credentials, "_serialize", boom)
    with pytest.raises(OSError, match="disk full"):
        credentials.save(server_url="https://new.example.com", token=SecretStr("pep_pat_new"))

    _old_login_survives()
    assert [p.name for p in credentials.credentials_path().parent.iterdir()] == ["credentials.toml"]


def test_save_keeps_old_credentials_when_the_replace_fails(tmp_home: Path, monkeypatch) -> None:
    credentials.save(server_url="https://old.example.com", token=SecretStr("pep_pat_old"))

    def no_replace(src, dst):
        raise OSError("rename refused")

    monkeypatch.setattr(credentials.os, "replace", no_replace)
    with pytest.raises(OSError, match="rename refused"):
        credentials.save(server_url="https://new.example.com", token=SecretStr("pep_pat_new"))

    _old_login_survives()
    assert [p.name for p in credentials.credentials_path().parent.iterdir()] == ["credentials.toml"]


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX-only perms check")
def test_save_is_0600_under_a_permissive_umask(tmp_home: Path) -> None:
    old = os.umask(0)
    try:
        path = credentials.save(server_url="https://pepper.example", token=SecretStr("pep_pat_x"))
        path = credentials.save(server_url="https://pepper.example", token=SecretStr("pep_pat_y"))
    finally:
        os.umask(old)
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX-only perms check")
def test_save_tightens_a_replaced_world_readable_file(tmp_home: Path) -> None:
    path = credentials.save(server_url="https://pepper.example", token=SecretStr("pep_pat_x"))
    path.chmod(0o644)
    credentials.save(server_url="https://pepper.example", token=SecretStr("pep_pat_y"))
    assert path.stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------------------
# Server URL validation (#296): the token only goes to a canonical origin.


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("https://pepper.example.com", "https://pepper.example.com"),
        ("https://pepper.example.com/", "https://pepper.example.com"),
        ("HTTPS://Pepper.Example.com:8443", "https://pepper.example.com:8443"),
        ("http://localhost:8000", "http://localhost:8000"),
        ("http://app.localhost:8000/", "http://app.localhost:8000"),
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
        ("http://127.1.2.3", "http://127.1.2.3"),
        ("http://[::1]:8000", "http://[::1]:8000"),
        ("https://[2001:db8::1]", "https://[2001:db8::1]"),
    ],
)
def test_validate_server_url_accepts(raw: str, canonical: str, monkeypatch) -> None:
    monkeypatch.delenv(credentials.ALLOW_INSECURE_HTTP_ENV, raising=False)
    assert credentials.validate_server_url(raw) == canonical


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        ("", "empty"),
        ("pepper.example.com", "https://"),
        ("ftp://pepper.example.com", "https://"),
        ("javascript:alert(1)", "https://"),
        ("https://", "no host"),
        ("https:///path", "no host"),
        ("https://alice:hunter2@pepper.example.com", "user name or password"),
        ("https://alice@pepper.example.com", "user name or password"),
        ("https://pepper.example.com?x=1", "query"),
        ("https://pepper.example.com/#frag", "fragment"),
        ("https://pepper.example.com/api", "no path"),
        ("https://pepper.example.com:notaport", "Invalid server URL"),
        ("https://pepper.example.com:99999", "Invalid server URL"),
        ("https://pepper.example .com", "spaces"),
        ("https://pepper.example.com\n", "control"),
        ("http://pepper.example.com", "plain http"),
        ("http://192.168.1.10:8000", "plain http"),
        ("http://localhost.evil.example", "plain http"),
    ],
)
def test_validate_server_url_rejects(raw: str, why: str, monkeypatch) -> None:
    monkeypatch.delenv(credentials.ALLOW_INSECURE_HTTP_ENV, raising=False)
    with pytest.raises(ValueError, match=why) as excinfo:
        credentials.validate_server_url(raw)
    assert "hunter2" not in str(excinfo.value)


def test_validate_server_url_plain_http_needs_the_opt_in(monkeypatch) -> None:
    monkeypatch.setenv(credentials.ALLOW_INSECURE_HTTP_ENV, "1")
    assert credentials.validate_server_url("http://pepper.lan:8000/") == "http://pepper.lan:8000"
    # The opt-in relaxes only the scheme rule; the URL must still be a bare origin.
    with pytest.raises(ValueError, match="user name or password"):
        credentials.validate_server_url("http://a:b@pepper.lan")


def _write_creds_file(server_url: str) -> None:
    path = credentials.credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'server_url = "{server_url}"\ntoken = "pep_pat_supersecret"\n')
    path.chmod(0o600)


@pytest.mark.parametrize(
    "server_url",
    [
        "https://alice:hunter2@pepper.example.com",
        "https://pepper.example.com/?next=evil",
        "file:///etc/passwd",
        "http://pepper.example.com",
    ],
)
def test_load_refuses_an_unsafe_server_url(server_url: str, monkeypatch) -> None:
    monkeypatch.delenv(credentials.ALLOW_INSECURE_HTTP_ENV, raising=False)
    _write_creds_file(server_url)
    with pytest.raises(credentials.CredentialsError) as excinfo:
        credentials.load()
    message = str(excinfo.value)
    assert "pep_pat_supersecret" not in message
    assert "hunter2" not in message


def test_load_accepts_a_self_hosted_https_url(monkeypatch) -> None:
    monkeypatch.delenv(credentials.ALLOW_INSECURE_HTTP_ENV, raising=False)
    _write_creds_file("https://pepper.selfhosted.example:8443/")
    assert credentials.load().server_url == "https://pepper.selfhosted.example:8443"
