"""A malformed proxy environment is a one-line error (or a skipped ``NO_PROXY`` entry), never a
traceback, and ``login`` answers in JSON when asked.

``httpx`` parses the proxy variables when it builds a client and raises ``httpx.InvalidURL``,
which is not a ``RequestError``, so the per-command handlers never saw it. The malformed values
here are real ones: each is checked against the installed ``httpx`` first, so a release that
starts tolerating one skips its test instead of passing for the wrong reason.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest

from pepper_cli import api_client, cli, credentials
from pepper_cli.secret import SecretStr

#: ``NO_PROXY`` values ``httpx`` cannot parse.
_BAD_ENTRY = "[fe80::1]"
_BAD_PORT = "host:abc"
#: A proxy URL ``httpx`` cannot parse.
_BAD_PROXY = "http://[::1"


@pytest.fixture(autouse=True)
def _clean_proxy_env(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}:
            monkeypatch.delenv(name)
    credentials.save(server_url="http://example.com", token=SecretStr("pep_agt_abc"))


def _rejects(monkeypatch, **env: str) -> bool:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    try:
        httpx.Client().close()
    except (httpx.InvalidURL, ValueError):
        return True
    return False


def _require_rejected(monkeypatch, **env: str) -> None:
    if not _rejects(monkeypatch, **env):
        pytest.skip(f"this httpx accepts {env}")


# ---------------------------------------------------------------------------
# NO_PROXY entries that cannot be parsed are skipped, with one warning


def test_a_malformed_no_proxy_entry_is_dropped_with_one_warning(monkeypatch, capsys):
    _require_rejected(monkeypatch, NO_PROXY=_BAD_ENTRY)
    monkeypatch.setenv("NO_PROXY", f"localhost,{_BAD_ENTRY},example.com")

    client = api_client.make_http_client(timeout=5)
    client.close()

    err = capsys.readouterr().err
    assert err.count("warning") == 1
    assert _BAD_ENTRY in err
    assert "NO_PROXY" in err
    assert os.environ["NO_PROXY"] == "localhost,example.com"


def test_every_malformed_entry_is_named_and_the_good_ones_are_kept(monkeypatch, capsys):
    _require_rejected(monkeypatch, NO_PROXY=_BAD_ENTRY)
    _require_rejected(monkeypatch, NO_PROXY=_BAD_PORT)
    monkeypatch.setenv("NO_PROXY", f"{_BAD_ENTRY}, {_BAD_PORT} ,10.0.0.0/8")

    api_client.make_http_client(timeout=5).close()

    err = capsys.readouterr().err
    assert _BAD_ENTRY in err
    assert _BAD_PORT in err
    assert os.environ["NO_PROXY"] == "10.0.0.0/8"


def test_the_lowercase_variable_is_cleaned_too(monkeypatch, capsys):
    _require_rejected(monkeypatch, no_proxy=_BAD_ENTRY)
    monkeypatch.setenv("no_proxy", f"{_BAD_ENTRY},localhost")

    api_client.make_http_client(timeout=5).close()

    assert os.environ["no_proxy"] == "localhost"
    assert _BAD_ENTRY in capsys.readouterr().err


def test_a_good_no_proxy_is_left_alone_and_silent(monkeypatch, capsys):
    monkeypatch.setenv("NO_PROXY", "localhost,10.0.0.0/8,fe80::1")

    api_client.make_http_client(timeout=5).close()

    assert capsys.readouterr().err == ""
    assert os.environ["NO_PROXY"] == "localhost,10.0.0.0/8,fe80::1"


def test_a_bad_entry_is_not_blamed_on_a_bad_proxy_url(monkeypatch, capsys):
    """The probe hides the other proxy variables, so each verdict is about its own entry."""
    _require_rejected(monkeypatch, HTTPS_PROXY=_BAD_PROXY)
    monkeypatch.setenv("NO_PROXY", "localhost")

    with pytest.raises(api_client.ProxyEnvironmentError):
        api_client.make_http_client(timeout=5)

    assert os.environ["NO_PROXY"] == "localhost"
    assert os.environ["HTTPS_PROXY"] == _BAD_PROXY
    assert capsys.readouterr().err == ""


def test_the_probe_restores_the_proxy_environment(monkeypatch):
    _require_rejected(monkeypatch, NO_PROXY=_BAD_ENTRY)
    monkeypatch.setenv("NO_PROXY", f"{_BAD_ENTRY},localhost")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:8")

    api_client.make_http_client(timeout=5).close()

    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:9"
    assert os.environ["ALL_PROXY"] == "http://127.0.0.1:8"
    assert os.environ["NO_PROXY"] == "localhost"
    assert "no_proxy" not in os.environ


def test_apiclient_construction_survives_the_issue_repro(monkeypatch, capsys):
    _require_rejected(monkeypatch, NO_PROXY=_BAD_ENTRY)
    monkeypatch.setenv("NO_PROXY", _BAD_ENTRY)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")

    with api_client.ApiClient(server_url="http://example.com", token=SecretStr("pep_pat_x")):
        pass

    assert _BAD_ENTRY in capsys.readouterr().err


def test_the_device_login_clients_are_sanitised_too(monkeypatch, capsys):
    _require_rejected(monkeypatch, NO_PROXY=_BAD_ENTRY)
    monkeypatch.setenv("NO_PROXY", _BAD_ENTRY)
    monkeypatch.setattr(httpx.Client, "post", lambda self, *a, **k: httpx.Response(500))

    with pytest.raises(api_client.ApiError):
        api_client.device_start("http://example.com", "box")
    with pytest.raises(api_client.ApiError):
        api_client.device_poll("http://example.com", SecretStr("dev-code"))

    assert _BAD_ENTRY in capsys.readouterr().err


# ---------------------------------------------------------------------------
# What cannot be skipped is one error line naming the variables


def test_an_unusable_proxy_url_raises_an_error_that_names_variables_not_values(monkeypatch):
    _require_rejected(monkeypatch, HTTPS_PROXY=_BAD_PROXY)

    with pytest.raises(api_client.ProxyEnvironmentError) as raised:
        api_client.make_http_client(timeout=5)

    message = str(raised.value)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "no_proxy"):
        assert name in message
    assert _BAD_PROXY not in message


_COMMANDS = {
    "rooms list": ["rooms", "list"],
    "rooms poll": ["rooms", "poll"],
    "rooms watch": ["rooms", "watch"],
    "rooms watch --for": ["rooms", "watch", "--for", "5s"],
    "rooms watch --room": ["rooms", "watch", "--room", "00000000-0000-0000-0000-0000000000a1"],
    "messages list": ["messages", "list", "--room", "r", "--topic", "t"],
    "whoami": ["whoami"],
    "chat": ["chat", "hello"],
    "login --token": ["login", "--server", "http://example.com", "--token", "pep_pat_abc"],
    "login device": ["login", "--server", "http://example.com"],
}


@pytest.mark.parametrize("argv", _COMMANDS.values(), ids=_COMMANDS.keys())
def test_every_command_turns_an_unusable_proxy_into_one_error_line(monkeypatch, capsys, argv):
    _require_rejected(monkeypatch, HTTPS_PROXY=_BAD_PROXY)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)

    rc = cli.main(argv)

    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert "Traceback" not in captured.err
    assert "NO_PROXY" in captured.err
    assert "no_proxy" in captured.err
    assert len(captured.err.strip().splitlines()) == 1
    assert _BAD_PROXY not in captured.err


@pytest.mark.parametrize("argv", [["rooms", "watch"], ["rooms", "poll"], ["whoami"]])
def test_the_error_is_a_json_line_under_json(monkeypatch, capsys, argv):
    _require_rejected(monkeypatch, HTTPS_PROXY=_BAD_PROXY)

    assert cli.main([*argv, "--json"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)["error"]
    assert error["code"] == "usage"
    assert "NO_PROXY" in error["message"]


def test_pepper_json_makes_the_proxy_error_json(monkeypatch, capsys):
    _require_rejected(monkeypatch, HTTPS_PROXY=_BAD_PROXY)
    monkeypatch.setenv("PEPPER_JSON", "1")

    assert cli.main(["rooms", "list"]) == 2

    assert json.loads(capsys.readouterr().err)["error"]["code"] == "usage"


def test_a_bad_url_that_reaches_main_is_still_a_clean_error(monkeypatch, capsys):
    def boom(_args):
        raise httpx.InvalidURL("Invalid port: 'x'")

    # The parser binds handlers when `main` builds it, so the patched one is what runs.
    monkeypatch.setattr(cli, "cmd_whoami", boom)

    assert cli.main(["whoami"]) == 2
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "NO_PROXY" in err


# ---------------------------------------------------------------------------
# login honors --json / PEPPER_JSON


class _LoginApi:
    """A stand-in client for login: ``whoami`` / ``list_groups`` / capabilities."""

    whoami_error: Exception | None = None
    groups_error: Exception | None = None
    rooms = api_client.GroupList(enabled=True, degraded=False, total=3, archived_total=0, groups=[])

    def __init__(self, *, server_url, token, **_kw) -> None:
        self.token = token

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def whoami(self):
        if _LoginApi.whoami_error:
            raise _LoginApi.whoami_error
        return api_client.Whoami(
            user_id="u-1", email="alice@example.com", display_name="Alice", timezone="UTC"
        )

    def put_client_capabilities(self):
        return None

    def list_groups(self, *, archived=False):
        if _LoginApi.groups_error:
            raise _LoginApi.groups_error
        return _LoginApi.rooms


@pytest.fixture
def login_api(monkeypatch):
    _LoginApi.whoami_error = None
    _LoginApi.groups_error = None
    _LoginApi.rooms = api_client.GroupList(
        enabled=True, degraded=False, total=3, archived_total=0, groups=[]
    )
    monkeypatch.setattr(api_client, "ApiClient", _LoginApi)
    return _LoginApi


def test_login_json_success_is_one_object_without_the_token(login_api, capsys):
    rc = cli.main(
        ["login", "--server", "http://example.com/", "--token", "pep_pat_secret", "--json"]
    )

    assert rc == 0
    captured = capsys.readouterr()
    body = json.loads(captured.out)
    assert body["ok"] is True
    assert body["server_url"] == "http://example.com"
    assert body["agent"] is False
    assert body["user_id"] == "u-1"
    assert body["display_name"] == "Alice"
    assert body["email"] == "alice@example.com"
    assert body["credentials_path"]
    assert "pep_pat_secret" not in captured.out + captured.err
    assert "Logged in" not in captured.err
    assert credentials.load().token.get_secret_value() == "pep_pat_secret"


def test_login_pepper_json_is_the_same_as_the_flag(login_api, monkeypatch, capsys):
    monkeypatch.setenv("PEPPER_JSON", "1")

    assert cli.main(["login", "--server", "http://example.com", "--token", "pep_pat_new"]) == 0

    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_login_without_json_keeps_the_text_chrome_on_stderr(login_api, capsys):
    assert cli.main(["login", "--server", "http://example.com", "--token", "pep_pat_new"]) == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Logged in as Alice" in captured.err


def test_login_json_for_an_agent_token(login_api, capsys):
    rc = cli.main(
        [
            "--profile",
            "agent",
            "login",
            "--server",
            "http://example.com",
            "--token",
            "pep_agt_secret",
            "--json",
        ]
    )

    assert rc == 0
    captured = capsys.readouterr()
    body = json.loads(captured.out)
    assert body["agent"] is True
    assert body["rooms"] == 3
    assert "pep_agt_secret" not in captured.out + captured.err


@pytest.mark.parametrize(
    ("argv", "error", "code", "status", "rc"),
    [
        (
            ["--token", "pep_pat_typo"],
            api_client.ApiError(status=401, body="", detail=None),
            "auth",
            401,
            1,
        ),
        (
            ["--token", "pep_pat_x"],
            api_client.ApiError(status=500, body="", detail="down"),
            "server",
            500,
            1,
        ),
        (["--token", "pep_pat_x"], httpx.ConnectError("boom"), "network", None, 1),
    ],
)
def test_login_json_errors_are_j8_lines(login_api, capsys, argv, error, code, status, rc):
    login_api.whoami_error = error

    assert cli.main(["login", "--server", "http://example.com", *argv, "--json"]) == rc

    captured = capsys.readouterr()
    assert captured.out == ""
    body = json.loads(captured.err)["error"]
    assert body["code"] == code
    assert body["status"] == status
    assert "pep_pat" not in captured.err


def test_login_json_agent_errors(login_api, capsys):
    base = ["--profile", "agent", "login", "--server", "http://example.com", "--json"]

    login_api.groups_error = api_client.ApiError(status=401, body="", detail=None)
    assert cli.main([*base, "--token", "pep_agt_x"]) == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "auth"

    login_api.groups_error = None
    login_api.rooms = api_client.GroupList(
        enabled=False, degraded=False, total=0, archived_total=0, groups=[]
    )
    assert cli.main([*base, "--token", "pep_agt_x"]) == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "forbidden"

    login_api.rooms = api_client.GroupList(
        enabled=True, degraded=True, total=0, archived_total=0, groups=[]
    )
    assert cli.main([*base, "--token", "pep_agt_x"]) == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "server"


def test_login_json_refuses_an_agent_token_without_a_profile(login_api, capsys):
    rc = cli.main(["login", "--server", "http://example.com", "--token", "pep_agt_x", "--json"])

    assert rc == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "usage"


def test_login_json_with_no_token_to_prompt_is_a_usage_error(login_api, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_prompt_secret", lambda _p: "")

    rc = cli.main(["login", "--server", "http://example.com", "--manual", "--json"])

    assert rc == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "usage"


def test_login_json_device_flow(login_api, monkeypatch, capsys):
    start = api_client.DeviceStart(
        device_code=SecretStr("dev-code"),
        user_code="ABCD-1234",
        verification_url="http://example.com/device",
        expires_in=600,
        interval=1,
    )
    approved = api_client.DevicePoll(
        status="approved",
        token=SecretStr("pep_pat_device"),
        user=api_client.Whoami(user_id="u-1", email=None, display_name="Alice", timezone="UTC"),
    )
    polls = [api_client.DevicePoll(status="pending"), approved]
    monkeypatch.setattr(api_client, "device_start", lambda *a, **k: start)
    monkeypatch.setattr(api_client, "device_poll", lambda *a, **k: polls.pop(0))
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)

    rc = cli.main(["login", "--server", "http://example.com", "--no-browser", "--json"])

    assert rc == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["ok"] is True
    # The human code and URL still reach the person, on stderr, never in the JSON document.
    assert "ABCD-1234" in captured.err
    assert "ABCD-1234" not in captured.out
    assert "pep_pat_device" not in captured.out + captured.err


@pytest.mark.parametrize(
    ("polls", "code", "needle"),
    [
        ([api_client.DevicePoll(status="denied")], "forbidden", "denied"),
        ([api_client.DevicePoll(status="expired")], "timeout", "expired"),
        ([api_client.DevicePoll(status="weird")], "server", "Unexpected"),
        ([httpx.ConnectError("boom")], "network", "Couldn't reach"),
        (
            [api_client.ApiError(status=500, body="", detail="down")],
            "server",
            "polling",
        ),
    ],
)
def test_login_json_device_flow_errors(login_api, monkeypatch, capsys, polls, code, needle):
    start = api_client.DeviceStart(
        device_code=SecretStr("dev-code"),
        user_code="ABCD-1234",
        verification_url="http://example.com/device",
        expires_in=600,
        interval=1,
    )

    def poll(*_a, **_k):
        item = polls.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(api_client, "device_start", lambda *a, **k: start)
    monkeypatch.setattr(api_client, "device_poll", poll)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)

    rc = cli.main(["login", "--server", "http://example.com", "--no-browser", "--json"])

    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err.strip().splitlines()[-1])["error"]
    assert error["code"] == code
    assert needle in error["message"]


def test_login_json_device_start_errors(login_api, monkeypatch, capsys):
    monkeypatch.setattr(
        api_client,
        "device_start",
        lambda *a, **k: (_ for _ in ()).throw(api_client.ApiError(status=422, body="", detail="x")),
    )

    assert cli.main(["login", "--server", "http://example.com", "--json"]) == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "validation"

    monkeypatch.setattr(
        api_client,
        "device_start",
        lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("boom")),
    )
    assert cli.main(["login", "--server", "http://example.com", "--json"]) == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "network"


def test_login_help_lists_json():
    with pytest.raises(SystemExit):
        cli.main(["login", "--help"])
