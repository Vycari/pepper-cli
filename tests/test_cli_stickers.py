"""``pepper stickers``: the client method and the command, with the HTTP layer mocked."""

from __future__ import annotations

import json

import httpx
import pytest

from pepper_cli import api_client, cli, credentials
from pepper_cli.secret import SecretStr

_PATH = "/api/v1/achievements"


def _sticker(key: str, title: str, *, earned: bool, **kw: object) -> dict[str, object]:
    row: dict[str, object] = {
        "key": key,
        "title": title,
        "how": f"How to earn {title}.",
        "kind": "weekly",
        "earned": earned,
        "tier": 0,
        "tier_name": None,
        "max_tier": 0,
        "times": 0,
        "first_week": None,
        "last_week": None,
        "citation": None,
        "secret": False,
        "sensitive": False,
        "image_path": None,
        "tiers": [],
    }
    row.update(kw)
    return row


_BOOK = {
    "earned": 2,
    "total": 3,
    "future_field": "ignored",
    "stickers": [
        _sticker(
            "daily-dynamo",
            "Daily Dynamo",
            earned=True,
            times=3,
            last_week="2026-11-15",
            citation="Chatted 7 days in a row.",
        ),
        _sticker(
            "closer",
            "Closer",
            earned=True,
            kind="lifetime",
            tier=1,
            tier_name="Bronze",
            max_tier=3,
            times=1,
            tiers=[
                {"tier": 1, "name": "Bronze", "threshold": 10, "earned_week": "2026-11-08"},
                {"tier": 2, "name": "Silver", "threshold": 100, "earned_week": None},
            ],
        ),
        _sticker("scribe", "Scribe", earned=False),
    ],
}


@pytest.fixture
def server(monkeypatch, tmp_path):
    """Route every ApiClient through a MockTransport; ``server.reply`` queues a response."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("PEPPER_JSON", raising=False)
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    seen: list[httpx.Request] = []
    replies: dict[str, httpx.Response] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return replies[request.url.path]

    real = api_client.ApiClient

    def make(**kw):
        return real(**kw, client=httpx.Client(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(api_client, "ApiClient", make)

    class S:
        pass

    s = S()
    s.seen = seen  # type: ignore[attr-defined]
    s.reply = lambda path, body, status=200: replies.__setitem__(  # type: ignore[attr-defined]
        path, httpx.Response(status, json=body)
    )
    return s


def test_the_book_lists_what_was_earned(server, capsys) -> None:
    server.reply(_PATH, _BOOK)
    assert cli.main(["stickers"]) == 0
    out = capsys.readouterr().out
    assert "2 of 3 stickers earned" in out
    assert "Daily Dynamo" in out and "3 weeks" in out and "Chatted 7 days in a row." in out
    assert "Closer (Bronze)" in out and "next: Silver at 100" in out
    assert "Scribe" not in out


def test_all_adds_the_unearned_ones_with_how(server, capsys) -> None:
    server.reply(_PATH, _BOOK)
    assert cli.main(["stickers", "--all"]) == 0
    out = capsys.readouterr().out
    assert "Not earned yet" in out and "Scribe" in out and "How to earn Scribe." in out


def test_json_is_the_list_envelope(server, capsys) -> None:
    server.reply(_PATH, _BOOK)
    assert cli.main(["stickers", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert [r["key"] for r in body["items"]] == ["daily-dynamo", "closer"]
    assert (body["earned"], body["total"], body["next_before"]) == (2, 3, None)


def test_an_empty_book_says_how_stickers_come(server, capsys) -> None:
    server.reply(_PATH, {"earned": 0, "total": 1, "stickers": [_sticker("a", "A", earned=False)]})
    assert cli.main(["stickers"]) == 0
    assert "none yet" in capsys.readouterr().out


def test_a_404_says_stickers_are_not_available(server, capsys) -> None:
    server.reply(_PATH, {"detail": "Not found."}, status=404)
    assert cli.main(["stickers"]) == 1
    assert "not available for your account" in capsys.readouterr().err
