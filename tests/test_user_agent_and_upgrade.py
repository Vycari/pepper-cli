"""``User-Agent`` on every request, and the 426 Upgrade Required exit path."""

from __future__ import annotations

import importlib.metadata
import re
from pathlib import Path

import httpx
import pytest

from pepper_cli import _version, api_client, cli
from pepper_cli.secret import SecretStr

_WHOAMI = {"user_id": "u1", "email": "alice@example.com", "display_name": "Alice"}
_DEVICE_START = {
    "device_code": "pep_dev_abc",
    "user_code": "ABCD-EFGH",
    "verification_url": "https://example.com/activate",
    "expires_in": 600,
    "interval": 5,
}


def _mock(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _api(handler) -> api_client.ApiClient:
    return api_client.ApiClient(
        server_url="http://example.com", token=SecretStr("pep_pat_abc"), client=_mock(handler)
    )


def _recorder(response: httpx.Response, seen: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response

    return handler


# ---------------------------------------------------------------------------
# version / User-Agent


def test_user_agent_is_pepper_cli_slash_installed_version() -> None:
    installed = importlib.metadata.version("vycari-pepper")
    assert f"pepper-cli/{installed}" == api_client.USER_AGENT
    assert _version.get_version() == installed


def test_version_falls_back_when_metadata_is_missing(monkeypatch) -> None:
    def missing(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(_version, "version", missing)
    assert _version.get_version() == "0+unknown"


@pytest.mark.parametrize(
    "bad",
    ["1.0\r\nX-Injected: yes", "1.0 beta", "1.0\x1b[2J", "", "1.0\u00e9", "1" * 65, "1.0\n"],
)
def test_version_that_is_not_header_safe_falls_back(monkeypatch, bad: str) -> None:
    monkeypatch.setattr(_version, "version", lambda _name: bad)
    assert _version.get_version() == "0+unknown"


@pytest.mark.parametrize("good", ["0.1.0", "1.2.3rc1", "1!2.0.post1.dev3", "0.1.0+local.build-7"])
def test_ordinary_versions_pass_through(monkeypatch, good: str) -> None:
    monkeypatch.setattr(_version, "version", lambda _name: good)
    assert _version.get_version() == good


def test_normal_request_sends_user_agent() -> None:
    seen: list[httpx.Request] = []
    with _api(_recorder(httpx.Response(200, json=_WHOAMI), seen)) as api:
        api.whoami()
    assert seen[0].headers["user-agent"] == api_client.USER_AGENT
    assert re.fullmatch(r"pepper-cli/\S+", seen[0].headers["user-agent"])


def test_streaming_request_sends_user_agent() -> None:
    seen: list[httpx.Request] = []
    body = 'event: reply\ndata: {"reply": "hi"}\n\n'
    resp = httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
    with _api(_recorder(resp, seen)) as api:
        list(api.chat_send_stream("hi"))
    assert seen[0].headers["user-agent"] == api_client.USER_AGENT


def test_feed_stream_and_takeout_send_user_agent(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    feed = httpx.Response(200, content=": ping\n\n", headers={"content-type": "text/event-stream"})
    with _api(_recorder(feed, seen)) as api:
        list(api.watch_agent_events())
    zipped = httpx.Response(200, content=b"zip")
    with _api(_recorder(zipped, seen)) as api:
        api.download_takeout(out_path=str(tmp_path / "t.zip"))
    assert [r.headers["user-agent"] for r in seen] == [api_client.USER_AGENT] * 2


def test_login_flow_sends_user_agent() -> None:
    seen: list[httpx.Request] = []
    api_client.device_start(
        "http://example.com",
        "alice@laptop",
        client=_mock(_recorder(httpx.Response(201, json=_DEVICE_START), seen)),
    )
    api_client.device_poll(
        "http://example.com",
        SecretStr("pep_dev_abc"),
        client=_mock(_recorder(httpx.Response(200, json={"status": "pending"}), seen)),
    )
    assert len(seen) == 2
    assert [r.headers["user-agent"] for r in seen] == [api_client.USER_AGENT] * 2


def test_owned_device_client_sends_user_agent(httpx_mock) -> None:
    httpx_mock.add_response(json=_DEVICE_START, status_code=201)
    api_client.device_start("http://example.com", "alice@laptop")
    assert httpx_mock.get_requests()[0].headers["user-agent"] == api_client.USER_AGENT


# ---------------------------------------------------------------------------
# 426 Upgrade Required


def test_426_json_detail_is_the_message() -> None:
    resp = httpx.Response(426, json={"detail": "Upgrade with: uv tool upgrade vycari-pepper"})
    with _api(lambda _: resp) as api, pytest.raises(api_client.UpgradeRequiredError) as ei:
        api.whoami()
    assert ei.value.message == "Upgrade with: uv tool upgrade vycari-pepper"


def test_426_plain_text_body_is_the_message() -> None:
    resp = httpx.Response(426, text="Please upgrade to 0.2.\nMore detail.\n")
    with _api(lambda _: resp) as api, pytest.raises(api_client.UpgradeRequiredError) as ei:
        api.whoami()
    assert ei.value.message == "Please upgrade to 0.2."


def _upgrade_message_for(resp: httpx.Response) -> str:
    with _api(lambda _: resp) as api, pytest.raises(api_client.UpgradeRequiredError) as ei:
        api.whoami()
    return ei.value.message


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # CSI sequences go whole: no ``[2J`` left behind.
        ("Upgrade\x1b[2J\x1b[Hnow", "Upgradenow"),
        ("\x1b[31mUpgrade\x1b[0m now", "Upgrade now"),
        ("Upgrade\x1b[?25l now", "Upgrade now"),
        # OSC (window title), ended by BEL or by ST.
        ("\x1b]0;owned\x07Upgrade now", "Upgrade now"),
        ("\x1b]0;owned\x1b\\Upgrade now", "Upgrade now"),
        # An OSC hyperlink keeps its visible text only.
        ("\x1b]8;;http://example.com\x07Upgrade\x1b]8;;\x07 now", "Upgrade now"),
        # Other two-byte escapes, and a lone trailing ESC.
        ("Upgrade\x1bc now", "Upgrade now"),
        ("Upgrade now\x1b", "Upgrade now"),
        ("Upgrade now\x1b[", "Upgrade now"),
        # C1 forms of CSI and OSC, and other C1 controls.
        ("Upgrade\x9b2J now", "Upgrade now"),
        ("\x9d0;owned\x9cUpgrade now", "Upgrade now"),
        ("Upgrade\x85\x90now", "Upgrade now"),
        # Bare C0 controls and DEL become spaces; runs collapse.
        ("Upgrade\x07\x08\x00\t\x7f  now", "Upgrade now"),
        # A carriage return cannot be used to overwrite the start of the line.
        ("Upgrade now\rAll good", "Upgrade now"),
        ("\x1b[2J\n\x07\nUpgrade now\nsecond line", "Upgrade now"),
        # Bidirectional overrides and zero-width characters cannot reorder or
        # hide the text.
        ("Upgrade \u202enow\u202c\u200b\u2066\u2069\ufeff", "Upgrade now"),
    ],
)
def test_426_message_has_terminal_controls_removed(body: str, expected: str) -> None:
    plain = _upgrade_message_for(httpx.Response(426, content=body.encode()))
    detail = _upgrade_message_for(httpx.Response(426, json={"detail": body}))
    assert plain == detail == expected


@pytest.mark.parametrize(
    "body",
    ["\x1b[2J\x1b[H", "\x07\x7f\x9b2J", "\x1b]0;owned\x07", "\r\n\t", "\u202e\u200b"],
)
def test_426_body_of_only_controls_uses_the_default_message(body: str) -> None:
    assert (
        _upgrade_message_for(httpx.Response(426, content=body.encode()))
        == api_client.DEFAULT_UPGRADE_MESSAGE
    )
    assert (
        _upgrade_message_for(httpx.Response(426, json={"detail": body}))
        == api_client.DEFAULT_UPGRADE_MESSAGE
    )


def test_426_message_is_capped_after_cleaning() -> None:
    # The escape sequences sit inside the first 300 raw characters; the cap
    # applies to what is left, so the cut never lands inside a sequence.
    body = "\x1b[31m" * 50 + "x" * 400
    message = _upgrade_message_for(httpx.Response(426, json={"detail": body}))
    assert message == "x" * 300


def test_cli_prints_no_terminal_controls_from_a_426(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    real_client = httpx.Client
    resp = httpx.Response(426, content=b"Upgrade\x1b[2J\x1b[H now\x07\r\nmore")

    def make_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(lambda _: resp)
        return real_client(**kwargs)

    monkeypatch.setattr(api_client.httpx, "Client", make_client)
    rc = cli.main(["login", "--server", "http://example.com", "--token", "pep_pat_abc"])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err == "Upgrade now\n"
    assert captured.out == ""


@pytest.mark.parametrize(
    "resp",
    [
        httpx.Response(426),
        httpx.Response(426, text="  \n"),
        httpx.Response(426, json={"detail": ["not", "a string"]}),
        httpx.Response(426, json={}),
    ],
)
def test_426_without_usable_body_uses_the_default_message(resp: httpx.Response) -> None:
    with _api(lambda _: resp) as api, pytest.raises(api_client.UpgradeRequiredError) as ei:
        api.whoami()
    assert ei.value.message == api_client.DEFAULT_UPGRADE_MESSAGE
    assert "uv tool upgrade vycari-pepper" in ei.value.message


def test_426_on_streaming_paths(tmp_path: Path) -> None:
    resp = httpx.Response(426, json={"detail": "Upgrade now"})
    with _api(lambda _: resp) as api:
        with pytest.raises(api_client.UpgradeRequiredError, match="Upgrade now"):
            list(api.chat_send_stream("hi"))
        with pytest.raises(api_client.UpgradeRequiredError, match="Upgrade now"):
            list(api.watch_agent_events())
        with pytest.raises(api_client.UpgradeRequiredError, match="Upgrade now"):
            api.download_takeout(out_path=str(tmp_path / "t.zip"))
    assert not (tmp_path / "t.zip").exists()


def test_426_on_device_login_flow() -> None:
    resp = httpx.Response(426, json={"detail": "Upgrade now"})
    with pytest.raises(api_client.UpgradeRequiredError):
        api_client.device_start("http://example.com", "dev", client=_mock(lambda _: resp))
    with pytest.raises(api_client.UpgradeRequiredError):
        api_client.device_poll("http://example.com", SecretStr("x"), client=_mock(lambda _: resp))


def test_other_errors_are_still_api_errors() -> None:
    resp = httpx.Response(500, json={"detail": "boom"})
    with _api(lambda _: resp) as api, pytest.raises(api_client.ApiError) as ei:
        api.whoami()
    assert (ei.value.status, ei.value.detail) == (500, "boom")


def test_cli_prints_upgrade_message_to_stderr_and_exits_nonzero_without_retry(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    seen: list[httpx.Request] = []
    real_client = httpx.Client
    resp = httpx.Response(426, json={"detail": "Upgrade with: uv tool upgrade vycari-pepper"})

    def make_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(_recorder(resp, seen))
        return real_client(**kwargs)

    monkeypatch.setattr(api_client.httpx, "Client", make_client)
    rc = cli.main(["login", "--server", "http://example.com", "--token", "pep_pat_abc"])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err.strip() == "Upgrade with: uv tool upgrade vycari-pepper"
    assert "Traceback" not in captured.err
    assert "pep_pat_abc" not in captured.out + captured.err
    assert len(seen) == 1


# ---------------------------------------------------------------------------
# check_latest_version / parse_version


def test_parse_version() -> None:
    assert _version.parse_version("0.3.0") == (0, 3, 0)
    assert _version.parse_version("0.4.0") == (0, 4, 0)
    assert _version.parse_version("0.4.0") > _version.parse_version("0.3.0")
    assert _version.parse_version("0.3.1") > _version.parse_version("0.3.0")
    assert _version.parse_version("0.3.0") == _version.parse_version("0.3.0")
    assert _version.parse_version("0.2.0") < _version.parse_version("0.3.0")
    assert _version.parse_version("invalid") == (0,)


def test_check_latest_version_pypi_newer(monkeypatch, httpx_mock) -> None:
    monkeypatch.setattr(_version, "get_version", lambda: "0.3.0")
    monkeypatch.setenv("PEPPER_NO_UPDATE_CHECK", "")
    httpx_mock.add_response(
        url="https://pypi.org/pypi/vycari-pepper/json",
        json={"info": {"version": "0.4.0"}},
    )
    assert _version.check_latest_version() == "0.4.0"


def test_check_latest_version_pypi_same_or_older(monkeypatch, httpx_mock) -> None:
    monkeypatch.setattr(_version, "get_version", lambda: "0.3.0")
    monkeypatch.setenv("PEPPER_NO_UPDATE_CHECK", "")
    httpx_mock.add_response(
        url="https://pypi.org/pypi/vycari-pepper/json",
        json={"info": {"version": "0.3.0"}},
    )
    assert _version.check_latest_version() is None


def test_check_latest_version_pypi_error(monkeypatch, httpx_mock) -> None:
    monkeypatch.setattr(_version, "get_version", lambda: "0.3.0")
    monkeypatch.setenv("PEPPER_NO_UPDATE_CHECK", "")
    httpx_mock.add_response(
        url="https://pypi.org/pypi/vycari-pepper/json",
        status_code=500,
    )
    assert _version.check_latest_version() is None


def test_check_latest_version_disabled(monkeypatch) -> None:
    monkeypatch.setenv("PEPPER_NO_UPDATE_CHECK", "1")
    assert _version.check_latest_version() is None


def test_check_latest_version_unknown_version(monkeypatch) -> None:
    monkeypatch.setattr(_version, "get_version", lambda: _version.UNKNOWN_VERSION)
    monkeypatch.setenv("PEPPER_NO_UPDATE_CHECK", "")
    assert _version.check_latest_version() is None
