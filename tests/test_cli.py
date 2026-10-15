"""Tests for the ``pepper`` CLI dispatch.

We invoke ``cli.main([...])`` directly with a monkeypatched
``api_client.ApiClient`` so commands run end-to-end without the
network. The credentials module is exercised through the real
on-disk path, redirected to a temp dir via ``HOME``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Self

import httpx
import pytest

from pepper_cli import api_client, cli, credentials
from pepper_cli.secret import SecretStr


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return tmp_path


class _FakeApi:
    """Stand-in for :class:`api_client.ApiClient` used by the cli tests.

    Captures the constructor args so a test can assert what the CLI
    handed to the client (server URL, token), then returns canned
    responses from ``whoami`` and ``chat_send`` (or raises whatever
    the test injected).
    """

    instances: list[_FakeApi] = []

    def __init__(
        self,
        *,
        server_url: str,
        token: str,
        whoami_response: api_client.Whoami | Exception | None = None,
        chat_response: api_client.ChatReply | Exception | None = None,
        trajectory_response: api_client.TrajectoryDetail | Exception | None = None,
        trajectory_raw_response: dict | Exception | None = None,
        trajectory_list_response: list[api_client.TrajectoryListEntry] | Exception | None = None,
        groups_response: api_client.GroupList | Exception | None = None,
    ) -> None:
        self.server_url = server_url
        self.token = token
        self.whoami_response = whoami_response
        self.chat_response = chat_response
        self.trajectory_response = trajectory_response
        self.trajectory_raw_response = trajectory_raw_response
        self.trajectory_list_response = trajectory_list_response
        self.groups_response = groups_response
        self.closed = False
        _FakeApi.instances.append(self)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.closed = True

    def whoami(self) -> api_client.Whoami:
        if isinstance(self.whoami_response, Exception):
            raise self.whoami_response
        if self.whoami_response is None:
            raise AssertionError("test forgot to set whoami_response")
        return self.whoami_response

    def list_groups(self, *, archived: bool = False) -> api_client.GroupList:
        if isinstance(self.groups_response, Exception):
            raise self.groups_response
        if self.groups_response is None:
            raise AssertionError("test forgot to set groups_response")
        return self.groups_response

    def chat_send(self, message: str) -> api_client.ChatReply:
        self.sent_message = message
        if isinstance(self.chat_response, Exception):
            raise self.chat_response
        if self.chat_response is None:
            raise AssertionError("test forgot to set chat_response")
        return self.chat_response

    def chat_send_stream(self, message: str, *, client_tools=None):
        self.sent_message = message
        self.client_tools = client_tools
        if isinstance(self.chat_response, Exception):
            raise self.chat_response
        if self.chat_response is None:
            raise AssertionError("test forgot to set chat_response")
        yield api_client.StreamEvent(kind="reply", payload={"reply": self.chat_response.reply})

    def trajectory_get(self, trajectory_id: str) -> api_client.TrajectoryDetail:
        self.requested_trajectory_id = trajectory_id
        if isinstance(self.trajectory_response, Exception):
            raise self.trajectory_response
        if self.trajectory_response is None:
            raise AssertionError("test forgot to set trajectory_response")
        return self.trajectory_response

    def trajectory_get_raw(self, trajectory_id: str) -> dict:
        self.requested_trajectory_id = trajectory_id
        if isinstance(self.trajectory_raw_response, Exception):
            raise self.trajectory_raw_response
        if self.trajectory_raw_response is None:
            raise AssertionError("test forgot to set trajectory_raw_response")
        return self.trajectory_raw_response

    def trajectory_list(
        self, *, limit: int = 20, offset: int = 0
    ) -> list[api_client.TrajectoryListEntry]:
        self.requested_limit = limit
        self.requested_offset = offset
        if isinstance(self.trajectory_list_response, Exception):
            raise self.trajectory_list_response
        if self.trajectory_list_response is None:
            raise AssertionError("test forgot to set trajectory_list_response")
        return self.trajectory_list_response


@pytest.fixture
def patch_api(monkeypatch):
    """Install a factory the CLI calls instead of constructing a real
    ``ApiClient``. Tests pre-program responses via the kwargs the
    factory binds into the ``_FakeApi`` constructor."""
    _FakeApi.instances.clear()

    def install(
        *,
        whoami=None,
        chat=None,
        trajectory=None,
        trajectory_raw=None,
        trajectory_list=None,
        groups=None,
    ):
        def factory(**kwargs: Any) -> _FakeApi:
            return _FakeApi(
                whoami_response=whoami,
                chat_response=chat,
                trajectory_response=trajectory,
                trajectory_raw_response=trajectory_raw,
                trajectory_list_response=trajectory_list,
                groups_response=groups,
                **kwargs,
            )

        monkeypatch.setattr(api_client, "ApiClient", factory)

    return install


# ---------------------------------------------------------------------------
# login


def test_login_with_flags_writes_credentials(tmp_home: Path, patch_api, capsys) -> None:
    patch_api(
        whoami=api_client.Whoami(
            user_id="u", email="alice@example.com", display_name="Alice", timezone="UTC"
        )
    )
    rc = cli.main(["login", "--server", "http://example.com", "--token", "pep_pat_abc"])
    assert rc == 0

    # In-memory boundary: cmd_login must hand a SecretStr to the API
    # client, not the raw string. Without this assertion, a future
    # refactor that drops the wrap could leave the persisted file
    # correct (credentials.save runs separately) while still leaking
    # the plaintext into ``repr(api)`` and tracebacks.
    api_token = _FakeApi.instances[0].token
    assert isinstance(api_token, SecretStr)
    assert api_token.get_secret_value() == "pep_pat_abc"

    creds = credentials.load()
    assert creds.server_url == "http://example.com"
    assert creds.token.get_secret_value() == "pep_pat_abc"


def test_login_with_an_agent_token_validates_against_the_rooms_list(
    tmp_home: Path, patch_api, capsys
) -> None:
    """An agent token is not a Pepper account, so ``whoami`` would refuse it;
    login checks it on the one read every agent may make instead."""
    patch_api(groups=api_client.GroupList(enabled=True, degraded=False, total=2, archived_total=0))
    rc = cli.main(
        ["--profile", "agent", "login", "--server", "http://example.com", "--token", "pep_agt_abc"]
    )
    assert rc == 0
    assert credentials.load().token.get_secret_value() == "pep_agt_abc"
    assert "agent member of 2 room(s)" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("enabled", "degraded"),
    [(False, False), (True, True)],
    ids=["disabled", "degraded"],
)
def test_login_refuses_an_agent_token_whose_rooms_cannot_be_read(
    tmp_home: Path, patch_api, enabled: bool, degraded: bool
) -> None:
    """A 200 that says the rooms surface is off or unreadable is not a
    working login — no credentials are saved."""
    patch_api(
        groups=api_client.GroupList(enabled=enabled, degraded=degraded, total=0, archived_total=0)
    )
    rc = cli.main(
        ["--profile", "agent", "login", "--server", "http://example.com", "--token", "pep_agt_abc"]
    )
    assert rc == 1
    assert not credentials.credentials_path().exists()


def test_login_rejects_a_dead_agent_token(tmp_home: Path, patch_api) -> None:
    patch_api(groups=api_client.ApiError(status=401, body="x", detail="invalid bearer token"))
    rc = cli.main(
        ["--profile", "agent", "login", "--server", "http://example.com", "--token", "pep_agt_abc"]
    )
    assert rc == 1
    assert not credentials.credentials_path().exists()


def test_login_rejects_typo_token(tmp_home: Path, patch_api) -> None:
    patch_api(whoami=api_client.ApiError(status=401, body="invalid", detail="invalid bearer token"))
    rc = cli.main(["login", "--server", "http://example.com", "--token", "pep_pat_typo"])
    assert rc == 1
    # No credentials file written when validation fails — protects
    # against a follow-up 'pepper chat' silently using the bad token.
    assert not credentials.credentials_path().exists()


def test_login_handles_unreachable_server(tmp_home: Path, patch_api) -> None:
    patch_api(whoami=httpx.ConnectError("nope"))
    rc = cli.main(["login", "--server", "http://nowhere.example", "--token", "pep_pat_abc"])
    assert rc == 1
    assert not credentials.credentials_path().exists()


def test_login_strips_trailing_slash_from_server(tmp_home: Path, patch_api) -> None:
    patch_api(whoami=api_client.Whoami(user_id="u", email=None, display_name="A", timezone=None))
    rc = cli.main(["login", "--server", "http://example.com/", "--token", "pep_pat_abc"])
    assert rc == 0
    # ``ApiClient`` constructor received the stripped form.
    assert _FakeApi.instances[0].server_url == "http://example.com"


# ---------------------------------------------------------------------------
# login — device-authorization flow (the new default)


@pytest.fixture
def patch_device_flow(monkeypatch):
    """Install fake ``api_client.device_start`` / ``device_poll``.

    ``start`` is a single :class:`api_client.DeviceStart` (or an
    exception to raise). ``poll_sequence`` is a list consumed in order,
    one entry per call to ``device_poll`` — a :class:`api_client.DevicePoll`
    or an exception. Returns a dict of the calls made, so a test can
    assert e.g. that ``device_poll`` was never reached (timeout before
    first poll) or that ``device_start`` wasn't called at all (manual
    login path).
    """

    def install(
        *,
        start: api_client.DeviceStart | Exception,
        poll_sequence: list[api_client.DevicePoll | Exception] | None = None,
    ) -> dict[str, list]:
        calls: dict[str, list] = {"start": [], "poll": []}
        remaining = list(poll_sequence or [])

        def fake_device_start(server_url, device_name, **kwargs):
            calls["start"].append((server_url, device_name))
            if isinstance(start, Exception):
                raise start
            return start

        def fake_device_poll(server_url, device_code, **kwargs):
            calls["poll"].append((server_url, device_code))
            if not remaining:
                raise AssertionError("test ran out of programmed poll responses")
            item = remaining.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(api_client, "device_start", fake_device_start)
        monkeypatch.setattr(api_client, "device_poll", fake_device_poll)
        return calls

    return install


@pytest.fixture
def fake_browser(monkeypatch):
    """Stand in for ``webbrowser.open`` and record what it was called with."""
    opened: list[str] = []
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)
    return opened


@pytest.fixture
def no_sleep(monkeypatch):
    """Stand in for ``time.sleep`` so the poll loop doesn't actually
    wait, and record the requested durations."""
    sleeps: list[float] = []
    monkeypatch.setattr(cli.time, "sleep", sleeps.append)
    return sleeps


def _device_start(*, user_code="ABCD-EFGH", expires_in=600, interval=5) -> api_client.DeviceStart:
    return api_client.DeviceStart(
        device_code=SecretStr("pep_dev_abc123"),
        user_code=user_code,
        verification_url=f"http://example.com/auth/activate?user_code={user_code}",
        expires_in=expires_in,
        interval=interval,
    )


def test_login_device_flow_happy_path(
    tmp_home: Path, patch_api, patch_device_flow, fake_browser, no_sleep, capsys
) -> None:
    start = _device_start()
    calls = patch_device_flow(
        start=start,
        poll_sequence=[
            api_client.DevicePoll(status="pending"),
            api_client.DevicePoll(
                status="approved",
                token=SecretStr("pep_pat_freshlyminted"),
                user=api_client.Whoami(
                    user_id="u", email="alice@example.com", display_name="Alice", timezone="UTC"
                ),
            ),
        ],
    )
    patch_api(
        whoami=api_client.Whoami(
            user_id="u", email="alice@example.com", display_name="Alice", timezone="UTC"
        )
    )

    rc = cli.main(["login", "--server", "http://example.com"])
    assert rc == 0

    # Device-code + verification URL printed prominently.
    err = capsys.readouterr().err
    assert "ABCD-EFGH" in err
    assert start.verification_url in err

    # Browser opened with the verification URL.
    assert fake_browser == [start.verification_url]

    # Two poll attempts (pending, then approved), one sleep before each.
    assert len(calls["poll"]) == 2
    assert no_sleep == [5, 5]

    # Validated via whoami exactly like the manual path, then saved.
    api_token = _FakeApi.instances[0].token
    assert isinstance(api_token, SecretStr)
    assert api_token.get_secret_value() == "pep_pat_freshlyminted"
    creds = credentials.load()
    assert creds.server_url == "http://example.com"
    assert creds.token.get_secret_value() == "pep_pat_freshlyminted"


def test_login_device_flow_no_browser_flag(
    tmp_home: Path, patch_api, patch_device_flow, fake_browser, no_sleep, capsys
) -> None:
    start = _device_start()
    patch_device_flow(
        start=start,
        poll_sequence=[
            api_client.DevicePoll(
                status="approved",
                token=SecretStr("pep_pat_x"),
                user=api_client.Whoami(user_id="u", email=None, display_name="A", timezone=None),
            ),
        ],
    )
    patch_api(whoami=api_client.Whoami(user_id="u", email=None, display_name="A", timezone=None))

    rc = cli.main(["login", "--server", "http://example.com", "--no-browser"])
    assert rc == 0

    # Browser never opened...
    assert fake_browser == []
    # ...but the URL is still printed.
    err = capsys.readouterr().err
    assert start.verification_url in err


def test_login_device_flow_denied(
    tmp_home: Path, patch_device_flow, fake_browser, no_sleep, capsys
) -> None:
    patch_device_flow(start=_device_start(), poll_sequence=[api_client.DevicePoll(status="denied")])

    rc = cli.main(["login", "--server", "http://example.com"])
    assert rc == 1
    assert "denied" in capsys.readouterr().err.lower()
    assert not credentials.credentials_path().exists()


def test_login_device_flow_expired_status(
    tmp_home: Path, patch_device_flow, fake_browser, no_sleep, capsys
) -> None:
    patch_device_flow(
        start=_device_start(), poll_sequence=[api_client.DevicePoll(status="expired")]
    )

    rc = cli.main(["login", "--server", "http://example.com"])
    assert rc == 1
    err = capsys.readouterr().err.lower()
    assert "expired" in err
    assert "try again" in err or "pepper login" in err
    assert not credentials.credentials_path().exists()


def test_login_device_flow_hard_timeout(
    tmp_home: Path, patch_device_flow, fake_browser, no_sleep, capsys
) -> None:
    """``expires_in=0`` means the deadline has already passed by the
    time the loop checks it — the CLI must bail out with a friendly
    message and must never even attempt a poll."""
    calls = patch_device_flow(start=_device_start(expires_in=0), poll_sequence=[])

    rc = cli.main(["login", "--server", "http://example.com"])
    assert rc == 1
    assert calls["poll"] == []
    assert "expired" in capsys.readouterr().err.lower()
    assert not credentials.credentials_path().exists()


def test_login_device_flow_slow_down_backs_off(
    tmp_home: Path, patch_api, patch_device_flow, fake_browser, no_sleep
) -> None:
    start = _device_start(interval=5)
    patch_device_flow(
        start=start,
        poll_sequence=[
            api_client.DevicePoll(status="slow_down", interval=5),
            api_client.DevicePoll(
                status="approved",
                token=SecretStr("pep_pat_x"),
                user=api_client.Whoami(user_id="u", email=None, display_name="A", timezone=None),
            ),
        ],
    )
    patch_api(whoami=api_client.Whoami(user_id="u", email=None, display_name="A", timezone=None))

    rc = cli.main(["login", "--server", "http://example.com"])
    assert rc == 0
    # First sleep uses the starting interval (5); after slow_down the
    # CLI adds 5 before the next poll (10).
    assert no_sleep == [5, 10]


def test_login_device_flow_start_api_error(
    tmp_home: Path, patch_device_flow, fake_browser, no_sleep, capsys
) -> None:
    patch_device_flow(start=api_client.ApiError(status=422, body="bad", detail="bad device_name"))

    rc = cli.main(["login", "--server", "http://example.com"])
    assert rc == 1
    assert "bad device_name" in capsys.readouterr().err
    assert not credentials.credentials_path().exists()


def test_login_device_flow_start_network_error(
    tmp_home: Path, patch_device_flow, fake_browser, no_sleep, capsys
) -> None:
    patch_device_flow(start=httpx.ConnectError("nope"))

    rc = cli.main(["login", "--server", "http://example.com"])
    assert rc == 1
    assert "example.com" in capsys.readouterr().err


def test_login_manual_flag_skips_device_flow(
    tmp_home: Path, patch_api, patch_device_flow, monkeypatch
) -> None:
    """``--manual`` forces the paste-a-token prompt and must never touch
    the device-flow functions."""
    calls = patch_device_flow(start=AssertionError("device flow should not run"))
    monkeypatch.setattr(cli, "_prompt_secret", lambda prompt: "pep_pat_manual")
    patch_api(whoami=api_client.Whoami(user_id="u", email=None, display_name="A", timezone=None))

    rc = cli.main(["login", "--server", "http://example.com", "--manual"])
    assert rc == 0
    assert calls["start"] == []
    creds = credentials.load()
    assert creds.token.get_secret_value() == "pep_pat_manual"


def test_login_token_flag_skips_device_flow(tmp_home: Path, patch_api, patch_device_flow) -> None:
    """Regression guard: passing ``--token`` (the pre-existing scripting
    path) must not invoke the new device-flow functions at all."""
    calls = patch_device_flow(start=AssertionError("device flow should not run"))
    patch_api(whoami=api_client.Whoami(user_id="u", email=None, display_name="A", timezone=None))

    rc = cli.main(["login", "--server", "http://example.com", "--token", "pep_pat_abc"])
    assert rc == 0
    assert calls["start"] == []


# ---------------------------------------------------------------------------
# whoami


def test_whoami_prints_user_fields(tmp_home: Path, patch_api, capsys) -> None:
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    patch_api(
        whoami=api_client.Whoami(
            user_id="00000000-0000-0000-0000-000000000001",
            email="alice@example.com",
            display_name="Alice",
            timezone="America/Los_Angeles",
        )
    )
    rc = cli.main(["whoami"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "00000000-0000-0000-0000-000000000001" in out
    assert "Alice" in out
    assert "alice@example.com" in out
    assert "America/Los_Angeles" in out


def test_whoami_without_credentials_errors(tmp_home: Path, patch_api) -> None:
    rc = cli.main(["whoami"])
    assert rc == 1


def test_whoami_renews_message_on_401(tmp_home: Path, patch_api, capsys) -> None:
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_revoked"))
    patch_api(whoami=api_client.ApiError(status=401, body="", detail="invalid bearer token"))
    rc = cli.main(["whoami"])
    assert rc == 1
    err = " ".join(capsys.readouterr().err.split()).lower()
    # 401 must surface the "log in again" hint, not the raw status.
    assert "pepper login" in err


# ---------------------------------------------------------------------------
# chat


def test_chat_sends_and_prints_reply(tmp_home: Path, patch_api, capsys) -> None:
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    patch_api(
        chat=api_client.ChatReply(
            reply="hi from pepper",
            trajectory_id="t",
            user_message_id="u",
            assistant_message_id="a",
            steps=[],
        )
    )
    rc = cli.main(["chat", "hello there"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "hi from pepper" in out
    assert _FakeApi.instances[0].sent_message == "hello there"
    assert _FakeApi.instances[0].client_tools is None


def test_chat_without_credentials_errors(tmp_home: Path, patch_api) -> None:
    rc = cli.main(["chat", "hi"])
    assert rc == 1


def test_chat_blank_message_errors(tmp_home: Path, patch_api) -> None:
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    rc = cli.main(["chat", "   "])
    assert rc == 2  # usage-style exit code, not API error


def test_chat_surfaces_502(tmp_home: Path, patch_api, capsys) -> None:
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    patch_api(chat=api_client.ApiError(status=502, body="bad gateway", detail=None))
    rc = cli.main(["chat", "hi"])
    assert rc == 1
    err = " ".join(capsys.readouterr().err.split())
    assert "502" in err


def test_chat_timeout_warns_before_resubmission(tmp_home: Path, patch_api, capsys) -> None:
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    patch_api(chat=httpx.ReadTimeout("read timed out"))
    assert cli.main(["chat", "long research turn"]) == 1
    err = " ".join(capsys.readouterr().err.split())
    assert "check the web chat" in err.lower()
    assert _FakeApi.instances[0].sent_message == "long research turn"


# ---------------------------------------------------------------------------
# logout


def test_logout_removes_file(tmp_home: Path, capsys) -> None:
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    assert credentials.credentials_path().exists()
    rc = cli.main(["logout"])
    assert rc == 0
    assert not credentials.credentials_path().exists()


def test_logout_no_file_is_not_an_error(tmp_home: Path, capsys) -> None:
    """A no-op logout is a clean exit — running ``pepper logout``
    twice or after a fresh checkout shouldn't return nonzero."""
    rc = cli.main(["logout"])
    assert rc == 0
    # Rich may hard-wrap mid-word at narrow terminal widths in CI; flatten
    # whitespace before the substring check so the assertion isn't terminal-
    # width-dependent.
    err = " ".join(capsys.readouterr().err.split())
    assert "nothing to remove" in err


# ---------------------------------------------------------------------------
# parser plumbing


def test_version_flag(capsys) -> None:
    with pytest.raises(SystemExit) as ei:
        cli.main(["--version"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "pepper" in out.lower()


def test_main_sets_the_process_title(monkeypatch) -> None:
    """Every invocation renames the process before doing anything else —
    ``--version`` is the cheapest path through ``main`` to prove it."""
    calls: list[str] = []
    monkeypatch.setattr(cli.proctitle, "set_process_title", calls.append)
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert calls == ["pepper"]


def test_help_flag(capsys) -> None:
    """``pepper --help`` must keep printing top-level help even with
    the default-chat dispatch — argparse's flag handling fires before
    our preprocessing reroutes to ``chat``."""
    with pytest.raises(SystemExit) as ei:
        cli.main(["--help"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "login" in out
    assert "whoami" in out


# ---------------------------------------------------------------------------
# Implicit-chat dispatch (bare ``pepper`` → REPL, ``pepper <words>`` → one-shot)


def test_bare_pepper_opens_repl(monkeypatch) -> None:
    """``pepper`` with no args drops into the REPL via the chat
    subcommand. We monkeypatch ``cmd_chat`` to capture what argparse
    handed it — for bare invocation it should see an empty
    ``message`` list, which is the signal the chat handler uses to
    open the REPL."""
    seen: dict = {}

    def fake_cmd_chat(args):
        seen["message"] = args.message
        return 0

    monkeypatch.setattr(cli, "cmd_chat", fake_cmd_chat)
    rc = cli.main([])
    assert rc == 0
    assert seen["message"] == []


def test_pepper_with_single_message_routes_to_chat(monkeypatch) -> None:
    """``pepper hi`` is shorthand for ``pepper chat hi``."""
    seen: dict = {}

    def fake_cmd_chat(args):
        seen["message"] = args.message
        return 0

    monkeypatch.setattr(cli, "cmd_chat", fake_cmd_chat)
    rc = cli.main(["hi"])
    assert rc == 0
    assert seen["message"] == ["hi"]


def test_pepper_with_unquoted_multi_word_joins(monkeypatch) -> None:
    """``pepper hi how are you`` should send a single message — the
    chat handler joins the words, so an unquoted multi-word shorthand
    works without the user remembering to quote."""
    seen: dict = {}

    def fake_cmd_chat(args):
        seen["message"] = args.message
        return 0

    monkeypatch.setattr(cli, "cmd_chat", fake_cmd_chat)
    rc = cli.main(["hi", "how", "are", "you"])
    assert rc == 0
    assert seen["message"] == ["hi", "how", "are", "you"]


def test_pepper_login_still_dispatches_to_login(monkeypatch) -> None:
    """Regression guard: known subcommand names must still route
    through argparse normally — ``pepper login --token X`` should
    hit ``cmd_login``, not ``cmd_chat(['login', '--token', 'X'])``."""
    seen: dict = {}

    def fake_cmd_login(args):
        seen["server"] = args.server
        seen["token"] = args.token
        return 0

    def fake_cmd_chat(args):
        seen["chat_message"] = args.message
        return 0

    monkeypatch.setattr(cli, "cmd_login", fake_cmd_login)
    monkeypatch.setattr(cli, "cmd_chat", fake_cmd_chat)
    rc = cli.main(["login", "--server", "http://x", "--token", "pep_pat_abc"])
    assert rc == 0
    assert seen["server"] == "http://x"
    assert seen["token"] == "pep_pat_abc"
    assert "chat_message" not in seen


def test_pepper_chat_explicit_still_works(monkeypatch) -> None:
    """``pepper chat <message>`` (the original form) keeps working —
    explicit and implicit paths route through the same handler."""
    seen: dict = {}

    def fake_cmd_chat(args):
        seen["message"] = args.message
        return 0

    monkeypatch.setattr(cli, "cmd_chat", fake_cmd_chat)
    rc = cli.main(["chat", "hello", "world"])
    assert rc == 0
    assert seen["message"] == ["hello", "world"]


# ---------------------------------------------------------------------------
# --profile


def _profile_path(tmp_home: Path, name: str) -> Path:
    return tmp_home / ".config" / "pepper" / "profiles" / name / "credentials.toml"


def test_profile_login_writes_profile_file_and_leaves_default_untouched(
    tmp_home: Path, patch_api
) -> None:
    credentials.save(server_url="http://own.example", token=SecretStr("pep_pat_own"))
    default = credentials.credentials_path()
    before = default.read_text()
    patch_api(groups=api_client.GroupList(enabled=True, degraded=False, total=1, archived_total=0))
    rc = cli.main(
        [
            "--profile",
            "claude-code",
            "login",
            "--server",
            "http://a.example",
            "--token",
            "pep_agt_x",
        ]
    )
    assert rc == 0
    assert _profile_path(tmp_home, "claude-code").exists()
    assert default.read_text() == before


def test_profile_equals_form_and_logout_only_removes_the_profile(tmp_home: Path, patch_api) -> None:
    credentials.save(server_url="http://own.example", token=SecretStr("pep_pat_own"))
    default = credentials.credentials_path()
    credentials.set_profile("agent")
    credentials.save(server_url="http://a.example", token=SecretStr("pep_agt_x"))
    credentials.set_profile(None)
    assert cli.main(["--profile=agent", "logout"]) == 0
    assert not _profile_path(tmp_home, "agent").exists()
    assert default.exists()


@pytest.mark.parametrize("argv", [["login", "--token"], ["login", "--manual"]])
def test_agent_token_without_profile_is_refused_and_writes_nothing(
    tmp_home: Path, patch_api, monkeypatch, capsys, argv: list[str]
) -> None:
    patch_api(groups=api_client.GroupList(enabled=True, degraded=False, total=1, archived_total=0))
    monkeypatch.setattr(cli, "_prompt_secret", lambda _p: "pep_agt_abc")
    if argv[-1] == "--token":
        argv = [*argv, "pep_agt_abc"]
    rc = cli.main([*argv, "--server", "http://a.example"])
    assert rc != 0
    assert not (tmp_home / ".config").exists()
    assert "--profile" in capsys.readouterr().err
    assert _FakeApi.instances == []


def test_agent_token_manual_prompt_with_profile_succeeds(
    tmp_home: Path, patch_api, monkeypatch
) -> None:
    patch_api(groups=api_client.GroupList(enabled=True, degraded=False, total=1, archived_total=0))
    monkeypatch.setattr(cli, "_prompt_secret", lambda _p: "pep_agt_abc")
    rc = cli.main(["--profile", "p", "login", "--manual", "--server", "http://a.example"])
    assert rc == 0
    assert _profile_path(tmp_home, "p").exists()
    assert not credentials.credentials_path().parent.parent.joinpath("credentials.toml").exists()


@pytest.mark.parametrize("name", ["../x", "..", ".", "a/b", "", "x y", "-x", "--help", "--prof"])
def test_invalid_profile_exits_2(tmp_home: Path, monkeypatch, capsys, name: str) -> None:
    called = []
    monkeypatch.setattr(cli, "cmd_whoami", lambda _a: called.append(1) or 0)
    monkeypatch.setattr(cli, "cmd_chat", lambda _a: called.append(1) or 0)
    assert cli.main(["--profile", name, "whoami"]) == 2
    assert "Invalid profile name" in capsys.readouterr().err
    assert called == []
    assert credentials.active_profile() is None


def test_profile_without_value_exits_2(capsys) -> None:
    assert cli.main(["--profile"]) == 2
    assert "requires a profile name" in capsys.readouterr().err


def test_leading_profile_reaches_subcommand_not_implicit_chat(monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(cli, "cmd_whoami", lambda _a: seen.setdefault("whoami", 1) and 0)
    monkeypatch.setattr(cli, "cmd_chat", lambda a: seen.setdefault("chat", a.message) and 0)
    assert cli.main(["--profile", "x", "whoami"]) == 0
    assert "whoami" in seen
    assert "chat" not in seen
    assert credentials.active_profile() == "x"


def test_leading_profile_then_words_is_implicit_chat(monkeypatch) -> None:
    seen: dict = {}

    def fake_chat(a):
        seen["message"] = a.message
        return 0

    monkeypatch.setattr(cli, "cmd_chat", fake_chat)
    assert cli.main(["--profile", "x", "hello", "there"]) == 0
    assert seen["message"] == ["hello", "there"]
    assert credentials.active_profile() == "x"


def test_bare_profile_opens_the_repl(monkeypatch) -> None:
    seen: dict = {}

    def fake_chat(a):
        seen["message"] = a.message
        return 0

    monkeypatch.setattr(cli, "cmd_chat", fake_chat)
    assert cli.main(["--profile", "x"]) == 0
    assert seen["message"] == []


def test_profile_does_not_leak_into_the_next_invocation(monkeypatch) -> None:
    monkeypatch.setattr(cli, "cmd_whoami", lambda _a: 0)
    cli.main(["--profile", "x", "whoami"])
    cli.main(["whoami"])
    assert credentials.active_profile() is None


def test_profile_flag_is_documented_in_help(capsys) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "--profile" in capsys.readouterr().out


def test_unparsed_profile_after_subcommand_is_an_error(capsys) -> None:
    """Only a *leading* ``--profile`` is honored; later it is just an
    unknown flag, never silently ignored."""
    with pytest.raises(SystemExit) as exc:
        cli.main(["whoami", "--profile", "x"])
    assert exc.value.code == 2


def _seed_default_and_profiles(*names: str) -> dict[Path, str]:
    """Save a default login plus one per profile; return each file's text."""
    credentials.save(server_url="http://own.example", token=SecretStr("pep_pat_own"))
    seeded = {credentials.credentials_path(): ""}
    for name in names:
        credentials.set_profile(name)
        credentials.save(server_url=f"http://{name}.example", token=SecretStr(f"pep_agt_{name}"))
        seeded[credentials.credentials_path()] = ""
    credentials.set_profile(None)
    return {path: path.read_text() for path in seeded}


@pytest.mark.parametrize(
    "argv",
    [
        # argparse prefix abbreviations of ``--profile``
        ["--prof", "agent", "logout"],
        ["--pro=agent", "logout"],
        ["--p", "agent", "logout"],
        # duplicates, in every spelling pair
        ["--profile", "a", "--profile", "b", "logout"],
        ["--profile=a", "--profile=b", "logout"],
        ["--profile", "a", "--profile=b", "logout"],
        ["--profile", "a", "--prof", "b", "logout"],
        ["--profile", "a", "--profile", "logout"],
        ["--profile", "a", "--profile"],
        # not leading: after ``--``, after the subcommand, abbreviated there
        ["--", "--profile", "agent", "logout"],
        ["logout", "--profile", "agent"],
        ["logout", "--prof", "agent"],
        ["logout", "--profile=agent"],
        # near-miss spellings argparse does not know at all
        ["-profile", "agent", "logout"],
        ["--Profile", "agent", "logout"],
        ["--profiles", "agent", "logout"],
        # an option where the name belongs
        ["--profile", "--prof", "agent", "logout"],
        ["--profile", "-a", "logout"],
        ["--profile", "--profile", "agent", "logout"],
    ],
)
def test_profile_option_that_is_not_applied_is_rejected_before_any_command(
    tmp_home: Path, capsys, argv: list[str]
) -> None:
    """No spelling of a profile option may be accepted and then dropped: the
    real ``logout`` must never run against a login the caller did not name."""
    del tmp_home
    seeded = _seed_default_and_profiles("a", "b", "agent")
    try:
        rc = cli.main(argv)
    except SystemExit as exc:
        rc = exc.code
    assert rc == 2
    assert capsys.readouterr().err.strip()
    for path, text in seeded.items():
        assert path.read_text() == text


@pytest.mark.parametrize(
    "argv",
    [
        ["--prof", "x", "whoami"],
        ["--profile", "x", "--profile", "y", "whoami"],
        ["--profile", "x", "--prof", "y", "hello"],
        ["hello", "--profile", "x"],
        ["chat", "hi", "--prof", "x"],
    ],
)
def test_unapplied_profile_option_runs_no_handler(monkeypatch, capsys, argv: list[str]) -> None:
    called: list[str] = []
    monkeypatch.setattr(cli, "cmd_whoami", lambda _a: called.append("whoami") or 0)
    monkeypatch.setattr(cli, "cmd_chat", lambda _a: called.append("chat") or 0)
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
    assert called == []
    assert "profile" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--prof", "x", "whoami"], ["--profile", "x", "--p=y", "whoami"]])
def test_unapplied_profile_error_says_how_to_spell_it(capsys, argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(argv)
    err = capsys.readouterr().err
    assert "was not applied" in err
    assert "pepper --profile NAME <command>" in err


def test_applied_profile_leaves_nothing_in_the_parsed_namespace(monkeypatch) -> None:
    """The parser's ``--profile`` exists for ``--help`` only; a handler must
    read the selection from ``credentials``, never from a stale ``args`` slot."""
    seen: dict = {}
    monkeypatch.setattr(cli, "cmd_whoami", lambda a: seen.update(vars(a)) or 0)
    assert cli.main(["--profile", "x", "whoami"]) == 0
    assert "profile" not in seen
    assert credentials.active_profile() == "x"


def test_profile_then_help_still_prints_help(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--profile", "x", "--help"])
    assert exc.value.code == 0
    assert "--profile NAME" in capsys.readouterr().out


def test_double_dash_message_is_chat_text_not_a_profile(monkeypatch) -> None:
    """After ``--`` the words are the message; the default login is the one
    the caller chose by not giving a leading ``--profile``."""
    seen: dict = {}
    monkeypatch.setattr(cli, "cmd_chat", lambda a: seen.update(message=a.message) or 0)
    assert cli.main(["chat", "--", "--profile", "x"]) == 0
    assert seen["message"] == ["--profile", "x"]
    assert credentials.active_profile() is None


# ---------------------------------------------------------------------------
# trajectory show / list


_VALID_TRAJ_ID = "11111111-1111-1111-1111-111111111111"


def _make_detail() -> api_client.TrajectoryDetail:
    return api_client.TrajectoryDetail(
        trajectory=api_client.TrajectoryRef(
            id=_VALID_TRAJ_ID,
            user_id="22222222-2222-2222-2222-222222222222",
            channel="client_api",
            created_at="2026-05-31T12:00:00Z",
            closed_at=None,
            meta={"code_sha": "abcdef1234567890", "code_dirty": False},
        ),
        events=[
            api_client.TrajectoryEvent(
                id="aaaa1111-1111-1111-1111-111111111111",
                seq=0,
                event_type="user_message",
                payload={"text": "hi there"},
                created_at="2026-05-31T12:00:01Z",
            ),
            api_client.TrajectoryEvent(
                id="aaaa2222-2222-2222-2222-222222222222",
                seq=1,
                event_type="turn_end",
                payload={"status": "ok"},
                created_at="2026-05-31T12:00:02Z",
            ),
        ],
    )


def test_trajectory_show_prints_events(tmp_home: Path, patch_api, capsys) -> None:
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    patch_api(trajectory=_make_detail())
    rc = cli.main(["trajectories", "show", _VALID_TRAJ_ID])
    assert rc == 0
    out = capsys.readouterr().out
    # Header line carries the trajectory id and channel.
    assert _VALID_TRAJ_ID in out
    assert "client_api" in out
    # Code SHA truncated to 12 chars.
    assert "abcdef123456" in out
    # One line per event, in seq order.
    assert "[0] user_message" in out
    assert "[1] turn_end" in out
    assert _FakeApi.instances[0].requested_trajectory_id == _VALID_TRAJ_ID


def test_trajectory_show_json_dumps_raw_body(tmp_home: Path, patch_api, capsys) -> None:
    """``--json`` prints the wire dict to stdout. The access-trajectory
    skill pipes this into ``jq`` for full payloads."""
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    raw = {"trajectory": {"id": _VALID_TRAJ_ID, "channel": "client_api"}, "events": []}
    patch_api(trajectory_raw=raw)
    rc = cli.main(["trajectories", "show", _VALID_TRAJ_ID, "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    # Parseable JSON, not Rich-formatted output.
    import json as _json

    assert _json.loads(out) == raw


def test_trajectory_show_rejects_non_uuid(tmp_home: Path, patch_api, capsys) -> None:
    """A malformed id never reaches the server — local validation
    matches the bash-script behaviour and returns the usage exit code."""
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    patch_api()  # no responses needed; we shouldn't hit the API
    rc = cli.main(["trajectories", "show", "not-a-uuid"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "UUID" in err
    # No client was constructed because we bailed before opening one.
    assert _FakeApi.instances == []


def test_trajectory_show_returns_1_on_404(tmp_home: Path, patch_api, capsys) -> None:
    """404 (or any non-401 API error) returns exit code 1 with the
    server's detail surfaced. The access-trajectory skill watches for
    this exit code to fall back to the service-token script."""
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    patch_api(trajectory=api_client.ApiError(status=404, body="", detail="trajectory not found"))
    rc = cli.main(["trajectories", "show", _VALID_TRAJ_ID])
    assert rc == 1
    err = " ".join(capsys.readouterr().err.split())
    assert "404" in err
    assert "trajectory not found" in err


def test_trajectory_show_without_credentials_errors(tmp_home: Path, patch_api) -> None:
    rc = cli.main(["trajectories", "show", _VALID_TRAJ_ID])
    assert rc == 1


def test_trajectory_list_prints_rows(tmp_home: Path, patch_api, capsys) -> None:
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    patch_api(
        trajectory_list=[
            api_client.TrajectoryListEntry(
                id=_VALID_TRAJ_ID,
                channel="client_api",
                created_at="2026-05-31T12:00:00Z",
                closed_at=None,
                meta={},
                event_count=3,
                first_user_message="hi there",
                group_id=None,
                group_name=None,
            )
        ]
    )
    rc = cli.main(["trajectories", "list", "--limit", "5"])
    assert rc == 0
    out = capsys.readouterr().out
    assert _VALID_TRAJ_ID in out
    assert "client_api" in out
    assert "hi there" in out
    assert _FakeApi.instances[0].requested_limit == 5


def test_trajectory_list_empty_message(tmp_home: Path, patch_api, capsys) -> None:
    """An empty result set prints a friendly message, not nothing."""
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    patch_api(trajectory_list=[])
    rc = cli.main(["trajectories", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no trajectories" in out.lower()


def _entry(i: int) -> api_client.TrajectoryListEntry:
    return api_client.TrajectoryListEntry(
        id=f"00000000-0000-0000-0000-{i:012d}",
        channel="client_api",
        created_at="2026-05-31T12:00:00Z",
        closed_at=None,
        meta={},
        event_count=1,
        first_user_message=f"msg {i}",
        group_id=None,
        group_name=None,
    )


def test_trajectories_list_json_is_the_envelope_with_a_cursor(
    tmp_home: Path, patch_api, capsys
) -> None:
    import json as _json

    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    patch_api(trajectory_list=[_entry(1), _entry(2)])
    assert cli.main(["trajectories", "list", "--limit", "2", "--json"]) == 0
    body = _json.loads(capsys.readouterr().out)
    assert [r["id"][-1] for r in body["items"]] == ["1", "2"]
    assert body["next_before"] == "2"
    assert _FakeApi.instances[0].requested_offset == 0


def test_trajectories_list_short_page_has_no_cursor(tmp_home: Path, patch_api, capsys) -> None:
    import json as _json

    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    patch_api(trajectory_list=[_entry(1)])
    assert cli.main(["trajectories", "list", "--limit", "5", "--json"]) == 0
    assert _json.loads(capsys.readouterr().out)["next_before"] is None


def test_trajectories_list_before_requests_that_offset(tmp_home: Path, patch_api, capsys) -> None:
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    patch_api(trajectory_list=[_entry(1), _entry(2)])
    assert cli.main(["trajectories", "list", "--limit", "2", "--before", "40"]) == 0
    assert _FakeApi.instances[0].requested_offset == 40
    assert "--before 42" in capsys.readouterr().err


def test_trajectories_list_rejects_offset_bad_cursor_and_zero_limit(
    tmp_home: Path, patch_api, capsys
) -> None:
    import json as _json

    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    patch_api(trajectory_list=[])
    with pytest.raises(SystemExit) as ei:
        cli.main(["trajectories", "list", "--offset", "5"])
    assert ei.value.code == 2
    capsys.readouterr()
    assert cli.main(["trajectories", "list", "--before", "abc"]) == 2
    assert "--before expects a cursor" in capsys.readouterr().err
    assert cli.main(["trajectories", "list", "--limit", "0", "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and _json.loads(captured.err)["error"]["code"] == "usage"
    assert _FakeApi.instances == []


def test_trajectories_list_json_error_is_j8(tmp_home: Path, patch_api, capsys) -> None:
    import json as _json

    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    patch_api(trajectory_list=api_client.ApiError(status=500, body="", detail="boom"))
    assert cli.main(["trajectories", "list", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert _json.loads(captured.err)["error"]["status"] == 500


def test_trajectories_show_json_error_is_j8(tmp_home: Path, patch_api, capsys) -> None:
    import json as _json

    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    patch_api()
    assert cli.main(["trajectories", "show", "nope", "--json"]) == 2
    assert _json.loads(capsys.readouterr().err)["error"]["code"] == "usage"


@pytest.mark.parametrize("argv", [["trajectory", "list"], ["trajectory", "show", "x", "--json"]])
def test_trajectory_noun_is_a_stub(tmp_home: Path, patch_api, capsys, argv) -> None:
    patch_api()
    assert cli.main(argv) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == (
        "pepper: 'trajectory' was renamed in 0.2.0 — see 'pepper trajectories --help'"
    )
    assert _FakeApi.instances == []


def test_trajectory_subcommand_recognized_for_implicit_chat_dispatch(monkeypatch) -> None:
    """Regression guard: ``pepper trajectory show <id>`` must NOT route
    to ``cmd_chat`` via implicit-chat dispatch. The dispatch reads
    subcommand names from ``sub.choices`` so adding ``trajectory`` to
    the parser automatically excludes it from the chat fallback."""
    seen: dict = {}

    def fake_cmd_chat(args):
        seen["chat_message"] = args.message
        return 0

    def fake_cmd_trajectory_show(args):
        seen["trajectory_id"] = args.trajectory_id
        return 0

    monkeypatch.setattr(cli, "cmd_chat", fake_cmd_chat)
    monkeypatch.setattr(cli, "cmd_trajectory_show", fake_cmd_trajectory_show)
    rc = cli.main(["trajectories", "show", _VALID_TRAJ_ID])
    assert rc == 0
    assert seen["trajectory_id"] == _VALID_TRAJ_ID
    assert "chat_message" not in seen


def test_pepper_with_unknown_flag_goes_to_top_level_argparse(capsys) -> None:
    """Unknown top-level flags (anything starting with ``-`` that
    isn't ``--help`` / ``--version``) must hit top-level argparse so
    the error message is "unrecognized arguments: --foo" — not a
    confusing chat-side error from sending ``--foo`` as a message."""
    with pytest.raises(SystemExit) as ei:
        cli.main(["--foo"])
    # argparse exits 2 on unrecognized args.
    assert ei.value.code == 2


@pytest.mark.parametrize(
    ("argv", "hint"),
    [
        (["notse"], "Did you mean 'notes'?"),
        (["todos add"], "Did you mean 'todos'?"),
        (["notse add"], "Did you mean 'notes'?"),
    ],
)
def test_close_match_or_quoted_subcommand_errors_instead_of_chat(
    monkeypatch, capsys, argv, hint
) -> None:
    monkeypatch.setattr(cli, "cmd_chat", lambda args: pytest.fail("chatted"))
    assert cli.main(argv) == 2
    err = capsys.readouterr().err
    assert hint in err and "pepper chat" in err


def test_no_implicit_chat_env_errors_but_explicit_chat_works(monkeypatch, capsys) -> None:
    monkeypatch.setenv("PEPPER_NO_IMPLICIT_CHAT", "1")
    seen: list = []
    monkeypatch.setattr(cli, "cmd_chat", lambda args: seen.append(args.message) or 0)
    assert cli.main(["what's", "on", "today"]) == 2
    assert "unknown command" in capsys.readouterr().err
    assert cli.main(["chat", "hi"]) == 0
    assert cli.main([]) == 0
    assert seen == [["hi"], []]


def test_ordinary_chat_unaffected_by_typo_guard(monkeypatch) -> None:
    monkeypatch.delenv("PEPPER_NO_IMPLICIT_CHAT", raising=False)
    seen: list = []
    monkeypatch.setattr(cli, "cmd_chat", lambda args: seen.append(args.message) or 0)
    assert cli.main(["what's", "on", "today"]) == 0
    assert seen == [["what's", "on", "today"]]
