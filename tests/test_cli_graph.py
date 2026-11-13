"""``pepper graph``: the client methods and each command, with the HTTP layer mocked."""

from __future__ import annotations

import json

import httpx
import pytest

from pepper_cli import api_client, cli, credentials
from pepper_cli.secret import SecretStr

_CARD = {
    "entity_id": "0b0a0000-0000-4000-8000-000000000001",
    "name": "Casey Close",
    "aliases": [{"kind": "email", "value": "casey@x.io"}],
    "last_contact": "2026-11-11T17:00:00Z",
    "tie": {"rank": 2, "total": 40, "band": 5, "percentile": 0.97, "strength": 3.2},
    "meetings": [{"event_id": "past1", "occurred_at": "2026-11-09T17:00:00Z"}],
    "is_new": False,
}
_NEW = {
    "entity_id": "0b0a0000-0000-4000-8000-000000000002",
    "name": "new",
    "aliases": [{"kind": "email", "value": "new@y.io"}],
    "last_contact": None,
    "tie": None,
    "meetings": [],
    "is_new": True,
}
_PEOPLE = "/api/v1/graph/people"
_BRIEF = "/api/v1/graph/meetings/brief"
_UPCOMING = "/api/v1/graph/meetings/upcoming"


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


def test_list_shows_the_top_ties_lists_top_ties(server, capsys) -> None:
    server.reply(_PEOPLE, {"status": "ok", "people": [_CARD]})
    assert cli.main(["graph", "list", "--limit", "3"]) == 0
    out = capsys.readouterr().out
    assert "Casey Close" in out and "casey@x.io" in out
    assert "#2 of 40 (top 5)" in out and "past1" not in out
    request = server.seen[0]
    assert request.url.params["limit"] == "3" and "q" not in request.url.params


def test_search_sends_it_and_says_when_nobody_matches(server, capsys) -> None:
    server.reply(_PEOPLE, {"status": "ok", "people": []})
    assert cli.main(["graph", "search", "a b&c"]) == 0
    assert "nobody matches" in capsys.readouterr().out
    assert server.seen[0].url.params["q"] == "a b&c"


def test_brief_shows_known_and_new_attendees(server, capsys) -> None:
    server.reply(
        _BRIEF, {"status": "ok", "found": True, "event_id": "e1", "attendees": [_CARD, _NEW]}
    )
    assert cli.main(["graph", "show", "e1"]) == 0
    out = capsys.readouterr().out
    assert "Casey Close" in out and "(new to you)" in out and "no tie yet" in out
    assert server.seen[0].url.params["event"] == "e1"


def test_brief_for_an_event_the_graph_lacks(server, capsys) -> None:
    server.reply(_BRIEF, {"status": "ok", "found": False, "event_id": "zz", "attendees": []})
    assert cli.main(["graph", "show", "zz"]) == 0
    assert "holds no attendees for event zz" in capsys.readouterr().out


def test_upcoming_lists_meetings_with_cards(server, capsys) -> None:
    body = {
        "status": "ok",
        "meetings": [
            {"event_id": "e1", "starts_at": "2026-11-13T18:00:00Z", "attendees": [_CARD]},
        ],
    }
    server.reply(_UPCOMING, body)
    assert cli.main(["graph", "upcoming"]) == 0
    out = capsys.readouterr().out
    assert "event e1" in out and "Casey Close" in out
    assert server.seen[0].url.params["limit"] == "5"


def test_a_timeout_status_is_called_out(server, capsys) -> None:
    server.reply(_UPCOMING, {"status": "timeout", "meetings": []})
    assert cli.main(["graph", "upcoming"]) == 0
    assert "timed out" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [["graph", "list"], ["graph", "show", "e1"], ["graph", "upcoming"]],
)
def test_json_is_the_servers_body_and_people_use_the_list_envelope(server, capsys, argv) -> None:
    body = {"status": "ok", "people": [_CARD], "meetings": [], "found": False, "attendees": []}
    for path in (_PEOPLE, _BRIEF, _UPCOMING):
        server.reply(path, body)
    assert cli.main([*argv, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    if argv[1] == "list":
        assert out == {"items": [_CARD], "next_before": None, "status": "ok"}
    else:
        assert out == body


@pytest.mark.parametrize(
    "argv",
    [["graph", "list"], ["graph", "show", "e1"], ["graph", "upcoming"]],
)
def test_a_404_says_the_graph_is_not_available(server, capsys, argv) -> None:
    for path in (_PEOPLE, _BRIEF, _UPCOMING):
        server.reply(path, {"detail": "Not Found"}, status=404)
    assert cli.main(argv) == 1
    assert "not available for your account" in capsys.readouterr().err


@pytest.mark.parametrize("limit", ["0", "51"])
def test_limit_is_checked_before_any_request(server, capsys, limit) -> None:
    assert cli.main(["graph", "list", "--limit", limit]) == 2
    assert cli.main(["graph", "upcoming", "--limit", limit]) == 2
    assert server.seen == []


@pytest.mark.parametrize("argv", [["graph", "search", "   "], ["graph", "show", ""]])
def test_a_blank_query_or_event_is_refused_before_any_request(server, capsys, argv) -> None:
    assert cli.main(argv) == 2
    assert "must not be blank" in capsys.readouterr().err
    assert server.seen == []
