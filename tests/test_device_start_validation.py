"""``device_start`` treats the ``/auth/device/start`` body as untrusted input (#299).

A missing, mistyped or out-of-bounds field fails before the CLI prints, opens or polls with
anything, and the error names the field but never the device code.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from pepper_cli import api_client

_GOOD: dict[str, Any] = {
    "device_code": "pep_dev_supersecret",
    "user_code": "ABCD-EFGH",
    "verification_url": "https://pepper.example/auth/activate?user_code=ABCD-EFGH",
    "expires_in": 600,
    "interval": 5,
}


def _start(body: object, *, server_url: str = "https://pepper.example") -> api_client.DeviceStart:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=body)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return api_client.device_start(server_url, "alice@laptop", client=client)


def _with(**changes: object) -> dict[str, Any]:
    body = dict(_GOOD)
    for key, value in changes.items():
        if value is _MISSING:
            body.pop(key)
        else:
            body[key] = value
    return body


_MISSING = object()


def test_a_well_formed_response_with_unknown_fields_is_accepted() -> None:
    start = _start(_with(new_server_field={"anything": True}))
    assert start.user_code == "ABCD-EFGH"
    assert start.expires_in == 600
    assert start.interval == 5


def test_a_local_dev_server_may_send_a_plain_http_verification_url() -> None:
    url = "http://localhost:8000/auth/activate?user_code=ABCD-EFGH"
    start = _start(_with(verification_url=url), server_url="http://localhost:8000")
    assert start.verification_url == url


@pytest.mark.parametrize(
    ("body", "why"),
    [
        (["not", "an", "object"], "not a JSON object"),
        (_with(device_code=_MISSING), "device_code"),
        (_with(device_code=123), "device_code"),
        (_with(user_code=""), "user_code"),
        (_with(verification_url=None), "verification_url"),
        (_with(expires_in=_MISSING), "expires_in"),
        (_with(expires_in="600"), "expires_in"),
        (_with(expires_in=True), "expires_in"),
        (_with(expires_in=0), "expires_in"),
        (_with(expires_in=-5), "expires_in"),
        (_with(expires_in=10**9), "expires_in"),
        (_with(interval=5.5), "interval"),
        (_with(interval=0), "interval"),
        (_with(interval=-1), "interval"),
        (_with(interval=86400), "interval"),
        (_with(expires_in=10, interval=30), "interval is longer than expires_in"),
        (_with(verification_url="javascript:alert(1)"), "http"),
        (_with(verification_url="file:///etc/passwd"), "http"),
        (_with(verification_url="https:///no-host"), "http"),
        (_with(verification_url="https://evil@pepper.example/a"), "user name"),
        (_with(verification_url="https://pepper.example/\x1b]0;x\x07"), "control"),
        (_with(verification_url="http://pepper.example/a"), "plain http"),
    ],
)
def test_a_malformed_response_fails_before_polling(body: object, why: str) -> None:
    with pytest.raises(api_client.ApiError, match="malformed device-login response") as excinfo:
        _start(body)
    assert why in str(excinfo.value)
    assert "pep_dev_supersecret" not in str(excinfo.value)
    assert "pep_dev_supersecret" not in excinfo.value.body


def test_a_non_json_body_is_malformed() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(201, content=b"<html>proxy page</html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(api_client.ApiError, match="not a JSON object"):
        api_client.device_start("https://pepper.example", "alice@laptop", client=client)
