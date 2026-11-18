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


_ENTITY_PATH = "/api/v1/graph/entities/e-1"
_CATALOG_PATH = "/api/v1/graph/catalog/c-1"
_CATALOG_LOOKUP = "/api/v1/graph/catalog"
_TEAM = {"id": "e-1", "kind": "topic", "subtype": "team", "name": "Seattle Kraken"}
_CARD_C = {
    "id": "c-1",
    "kind": "topic",
    "subtype": "team",
    "name": "Seattle Kraken",
    "curated": True,
    "sensitive": False,
    "attrs": {},
    "external_ids": [{"scheme": "espn", "value": "nhl:26"}],
    "providers": [{"provider": "espn", "ref": "nhl/26"}],
    "neighbours": [
        {
            "direction": "out",
            "type": "member_of",
            "entry": {"id": "c-9", "kind": "topic", "subtype": "league", "name": "NHL"},
        }
    ],
}
_FOLLOW = {
    "id": "x1",
    "type": "follows",
    "origin": "follow",
    "direction": "in",
    "other": None,
    "follow_ref": "espn:nhl:26",
    "source": "follow_sources",
    "valid_from": "2026-11-01T00:00:00Z",
    "valid_to": None,
    "confidence": 1.0,
}
_CLOSED = {
    **_FOLLOW,
    "id": "x2",
    "type": "mentions",
    "direction": "out",
    "other": {"id": "o1", "kind": "person", "subtype": None, "name": "Casey Close"},
    "follow_ref": None,
    "valid_to": "2026-11-05T00:00:00Z",
}
_ENTITY_BODY = {
    "status": "ok",
    "entity": {
        "entity": _TEAM,
        "sensitive": False,
        "requested_id": "old-id",
        "catalog": _CARD_C,
        "edges": [_FOLLOW, _CLOSED],
    },
}


def test_entity_page_groups_edges_and_shows_the_merge(server, capsys) -> None:
    server.reply(_ENTITY_PATH, _ENTITY_BODY)
    assert cli.main(["graph", "entity", "e-1"]) == 0
    out = capsys.readouterr().out
    assert "Seattle Kraken (topic/team)" in out and "catalog: Seattle Kraken" in out
    assert "merged: old-id is now this entity" in out
    assert "follows" in out and "follow espn:nhl:26" in out and "source follow_sources" in out
    assert "mentions" in out and "-> Casey Close (person)" in out
    assert "closed" not in out and "open" not in out
    assert server.seen[0].url.params["history"] == "false"


def test_entity_page_with_history_marks_closed_edges(server, capsys) -> None:
    server.reply(_ENTITY_PATH, _ENTITY_BODY)
    assert cli.main(["graph", "entity", "e-1", "--history"]) == 0
    out = capsys.readouterr().out
    assert "closed" in out and "open" in out
    assert server.seen[0].url.params["history"] == "true"


def test_catalog_page_by_id_shows_the_card_and_your_entities(server, capsys) -> None:
    body = {
        "status": "ok",
        "page": {"catalog": _CARD_C, "entities": [{"entity": _TEAM, "edges": [_FOLLOW]}]},
    }
    server.reply(_CATALOG_PATH, body)
    assert cli.main(["graph", "catalog", "c-1"]) == 0
    out = capsys.readouterr().out
    assert "external ids: espn:nhl:26" in out and "providers: espn:nhl/26" in out
    assert "member_of -> NHL (topic/league)" in out
    assert "Your entities" in out and "follow espn:nhl:26" in out


def test_catalog_lookup_by_external_id(server, capsys) -> None:
    body = {"status": "ok", "page": {"catalog": _CARD_C, "entities": []}}
    server.reply(_CATALOG_LOOKUP, body)
    assert cli.main(["graph", "catalog", "--scheme", "espn", "--value", "nhl:26"]) == 0
    assert "none of your entities link here" in capsys.readouterr().out
    params = server.seen[0].url.params
    assert params["scheme"] == "espn" and params["value"] == "nhl:26"


@pytest.mark.parametrize(
    "argv,path",
    [
        (["graph", "entity", "e-1"], _ENTITY_PATH),
        (["graph", "catalog", "c-1"], _CATALOG_PATH),
    ],
)
def test_new_pages_json_is_the_body(server, capsys, argv, path) -> None:
    body = {"status": "ok", "entity": {"entity": _TEAM, "edges": []}, "page": {"catalog": _CARD_C}}
    server.reply(path, body)
    assert cli.main([*argv, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == body


@pytest.mark.parametrize("status", ["timeout", "error"])
def test_a_fail_soft_page_exits_non_zero(server, capsys, status) -> None:
    server.reply(_ENTITY_PATH, {"status": status, "entity": None})
    assert cli.main(["graph", "entity", "e-1"]) == 1
    err = capsys.readouterr().err
    assert "nothing to show" in err and ("timed out" in err) == (status == "timeout")


@pytest.mark.parametrize(
    "argv,path",
    [
        (["graph", "entity", "e-1"], _ENTITY_PATH),
        (["graph", "catalog", "c-1"], _CATALOG_PATH),
        (["graph", "catalog", "--scheme", "espn", "--value", "v"], _CATALOG_LOOKUP),
    ],
)
def test_new_pages_404_uses_one_message(server, capsys, argv, path) -> None:
    server.reply(path, {"detail": "Not Found"}, status=404)
    assert cli.main(argv) == 1
    assert "unknown, is not in your graph" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["graph", "entity", " "],
        ["graph", "catalog"],
        ["graph", "catalog", "--scheme", "espn"],
        ["graph", "catalog", "c-1", "--scheme", "espn", "--value", "v"],
    ],
)
def test_bad_arguments_are_refused_before_any_request(server, capsys, argv) -> None:
    assert cli.main(argv) == 2
    assert server.seen == []


def test_an_unknown_scheme_is_refused_by_the_parser(server, capsys) -> None:
    with pytest.raises(SystemExit):
        cli.main(["graph", "catalog", "--scheme", "nope", "--value", "v"])
    assert server.seen == []


_ROOM = "5c2f6a50-0000-4000-8000-0000000000aa"
_ROOM_ENTITY_PATH = f"/api/v1/graph/rooms/{_ROOM}/entities/e-1"


def test_room_entity_page_reads_the_room_route_and_prints_the_same_page(server, capsys) -> None:
    server.reply(_ROOM_ENTITY_PATH, _ENTITY_BODY)
    assert cli.main(["graph", "entity", "--room", _ROOM, "e-1"]) == 0
    out = capsys.readouterr().out
    assert "Seattle Kraken (topic/team)" in out and "follow espn:nhl:26" in out
    assert [r.url.path for r in server.seen] == [_ROOM_ENTITY_PATH]
    assert server.seen[0].url.params["history"] == "false"


def test_room_entity_page_history_and_json(server, capsys) -> None:
    server.reply(_ROOM_ENTITY_PATH, _ENTITY_BODY)
    assert cli.main(["graph", "entity", "--room", _ROOM, "e-1", "--history", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == _ENTITY_BODY
    assert server.seen[0].url.params["history"] == "true"


def test_room_entity_page_resolves_a_room_name(server, capsys) -> None:
    server.reply(
        "/api/v1/groups",
        {
            "enabled": True,
            "degraded": False,
            "total": 1,
            "archived_total": 0,
            "groups": [
                {
                    "group_id": _ROOM,
                    "display_name": "Chef Steve",
                    "color": "#fff",
                    "sub": "",
                    "member_count": 2,
                    "archived": False,
                }
            ],
        },
    )
    server.reply(_ROOM_ENTITY_PATH, _ENTITY_BODY)
    assert cli.main(["graph", "entity", "--room", "chef", "e-1"]) == 0
    assert server.seen[-1].url.path == _ROOM_ENTITY_PATH


def test_room_entity_page_404_names_membership_and_rollout(server, capsys) -> None:
    server.reply(_ROOM_ENTITY_PATH, {"detail": "Not Found"}, status=404)
    assert cli.main(["graph", "entity", "--room", _ROOM, "e-1"]) == 1
    err = capsys.readouterr().err
    assert "not a member" in err and "rollout" in err


def test_room_entity_page_fail_soft_status_exits_non_zero(server, capsys) -> None:
    server.reply(_ROOM_ENTITY_PATH, {"status": "timeout", "entity": None})
    assert cli.main(["graph", "entity", "--room", _ROOM, "e-1"]) == 1


def test_a_blank_room_is_refused_before_any_request(server, capsys) -> None:
    assert cli.main(["graph", "entity", "--room", " ", "e-1"]) == 2
    assert server.seen == []
