"""``pepper hubs``: the client methods and the commands, with the HTTP layer mocked."""

from __future__ import annotations

import json

import httpx
import pytest

from pepper_cli import api_client, cli, credentials
from pepper_cli.secret import SecretStr

HUB = "00000000-0000-0000-0000-0000000000b7"
OTHER = "00000000-0000-0000-0000-0000000000b8"
OFFER = "00000000-0000-0000-0000-0000000000b9"
ROOM = "00000000-0000-0000-0000-0000000000a1"
NOTE = "00000000-0000-0000-0000-0000000000e1"

_LIST = {
    "hubs": [{"id": HUB, "name": "Trip", "pinned": True, "shared_level": "edit"}],
    "shared": [{"id": OTHER, "name": "House", "owner_name": "Chris"}],
    "offers": [{"id": OFFER, "name": "Garden", "owner_name": "Sam"}],
    "rooms_complete": True,
    "future_field": "ignored",
}


def _detail(**kw: object) -> dict[str, object]:
    body: dict[str, object] = {
        "id": HUB,
        "name": "Trip",
        "role": "owner",
        "owner_name": None,
        "pinned": False,
        "shared_level": None,
        "can_share": True,
        "rooms": [{"id": ROOM, "name": "Trip planning", "notification_level": "mentions"}],
        "items": [{"id": NOTE, "name": "Plan", "kind": "note"}],
        "rooms_complete": True,
        "future_field": "ignored",
    }
    body.update(kw)
    return body


class _Server:
    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self.replies: dict[tuple[str, str], httpx.Response] = {}

    def reply(self, method: str, path: str, body: object = None, status: int = 200) -> None:
        self.replies[(method, path)] = httpx.Response(status, json=body)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return self.replies[(request.method, request.url.path)]

    def calls(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.path) for r in self.seen]


@pytest.fixture
def server(tmp_path, monkeypatch) -> _Server:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("PEPPER_JSON", raising=False)
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    srv = _Server()
    real = api_client.ApiClient

    def make(**kw):
        return real(**kw, client=httpx.Client(transport=httpx.MockTransport(srv.handle)))

    monkeypatch.setattr(api_client, "ApiClient", make)
    return srv


def test_list_shows_own_taken_and_offered_hubs(server, capsys) -> None:
    server.reply("GET", "/api/v1/hubs", _LIST)
    assert cli.main(["hubs", "list"]) == 0
    out = capsys.readouterr().out
    assert "Trip" in out and "pinned" in out and "shared: people can edit" in out
    assert "Shared with you" in out and "House" in out and "from Chris" in out
    assert "Offered to you" in out and "Garden" in out and "pepper hubs take HUB" in out
    assert "couldn't check" not in out


def test_list_json_is_the_envelope_with_a_standing_on_each_row(server, capsys) -> None:
    server.reply("GET", "/api/v1/hubs", {**_LIST, "rooms_complete": False})
    assert cli.main(["hubs", "list", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert [(r["name"], r["standing"]) for r in body["items"]] == [
        ("Trip", "owner"),
        ("House", "taken"),
        ("Garden", "offered"),
    ]
    assert body["items"][1]["owner_name"] == "Chris"
    assert (body["next_before"], body["rooms_complete"]) == (None, False)


def test_an_empty_list_says_how_to_make_one_and_flags_a_short_answer(server, capsys) -> None:
    server.reply("GET", "/api/v1/hubs", {"hubs": [], "rooms_complete": False})
    assert cli.main(["hubs", "list"]) == 0
    out = capsys.readouterr().out
    assert "pepper hubs create NAME" in out and "couldn't check all of your rooms" in out


def test_show_finds_a_hub_by_name_in_any_case(server, capsys) -> None:
    server.reply("GET", "/api/v1/hubs", _LIST)
    server.reply("GET", f"/api/v1/hubs/{HUB}", _detail(pinned=True, shared_level="view"))
    assert cli.main(["hubs", "show", "trip"]) == 0
    out = capsys.readouterr().out
    assert "Yours. Shared with the people in its rooms: they can view." in out
    assert "Pinned" in out and "Trip planning" in out and "@mentions only" in out
    assert "Plan" in out and NOTE in out


def test_show_of_someone_elses_hub_says_whose_and_what_you_may_do(server, capsys) -> None:
    server.reply("GET", f"/api/v1/hubs/{OTHER}", _detail(id=OTHER, role="edit", owner_name="Chris"))
    assert cli.main(["hubs", "show", OTHER.upper()]) == 0
    assert "Chris's. You can edit." in capsys.readouterr().out
    assert server.calls() == [("GET", f"/api/v1/hubs/{OTHER}")]


def test_an_unknown_or_repeated_name_is_refused_before_any_change(server, capsys) -> None:
    twice = {**_LIST, "shared": [{"id": OTHER, "name": "TRIP", "owner_name": "Chris"}]}
    server.reply("GET", "/api/v1/hubs", twice)
    assert cli.main(["hubs", "pin", "nope"]) == 2
    assert "no hub named 'nope'" in capsys.readouterr().err
    assert cli.main(["hubs", "pin", "Trip", "--json"]) == 2
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "ambiguous" and HUB in error["message"] and OTHER in error["message"]
    assert {method for method, _ in server.calls()} == {"GET"}


def test_create_prints_the_id_and_rename_sends_the_name(server, capsys) -> None:
    server.reply("POST", "/api/v1/hubs", _detail(), status=201)
    assert cli.main(["hubs", "create", " Trip "]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == HUB and "Made the hub 'Trip'." in captured.err
    assert json.loads(server.seen[-1].content) == {"name": "Trip"}

    server.reply("PATCH", f"/api/v1/hubs/{HUB}", _detail(name="Japan"))
    assert cli.main(["hubs", "edit", HUB, "--name", "Japan"]) == 0
    assert "Renamed to 'Japan'." in capsys.readouterr().err
    assert json.loads(server.seen[-1].content) == {"name": "Japan"}

    assert cli.main(["hubs", "create", "  "]) == 2
    assert cli.main(["hubs", "edit", HUB, "--name", " "]) == 2
    assert len(server.seen) == 2


def test_add_and_remove_send_the_room_or_the_item(server, capsys) -> None:
    server.reply("PUT", f"/api/v1/hubs/{HUB}/rooms/{ROOM}", _detail())
    server.reply("PUT", f"/api/v1/hubs/{HUB}/items/{NOTE}", _detail())
    server.reply("DELETE", f"/api/v1/hubs/{HUB}/rooms/{ROOM}", _detail(rooms=[]))
    server.reply("DELETE", f"/api/v1/hubs/{HUB}/items/{NOTE}", _detail(items=[]))
    assert cli.main(["hubs", "add", HUB, "--room", ROOM]) == 0
    assert "Added the room to 'Trip'." in capsys.readouterr().err
    assert cli.main(["hubs", "add", HUB, "--item", NOTE]) == 0
    assert "Added the item to 'Trip'." in capsys.readouterr().err
    assert cli.main(["hubs", "remove", HUB, "--room", ROOM, "--yes"]) == 0
    assert "Took the room out of 'Trip'." in capsys.readouterr().err
    assert cli.main(["hubs", "remove", HUB, "--item", NOTE, "--yes", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["items"] == []
    assert server.calls() == [
        ("PUT", f"/api/v1/hubs/{HUB}/rooms/{ROOM}"),
        ("PUT", f"/api/v1/hubs/{HUB}/items/{NOTE}"),
        ("DELETE", f"/api/v1/hubs/{HUB}/rooms/{ROOM}"),
        ("DELETE", f"/api/v1/hubs/{HUB}/items/{NOTE}"),
    ]


def test_add_needs_exactly_one_of_room_and_item(server, capsys) -> None:
    with pytest.raises(SystemExit):
        cli.main(["hubs", "add", HUB])
    with pytest.raises(SystemExit):
        cli.main(["hubs", "add", HUB, "--room", ROOM, "--item", NOTE])
    capsys.readouterr()
    assert cli.main(["hubs", "add", HUB, "--item", "not-an-id"]) == 2
    assert server.seen == []


@pytest.mark.parametrize(
    "argv",
    [
        ["hubs", "delete", HUB],
        ["hubs", "remove", HUB, "--room", ROOM],
        ["hubs", "revoke", HUB],
        ["hubs", "leave", HUB],
    ],
)
def test_the_verbs_that_take_something_away_ask_first(server, capsys, argv) -> None:
    assert cli.main(argv) == 2
    assert "--yes" in capsys.readouterr().err
    assert server.seen == []


def test_delete_revoke_and_leave_answer_ok_in_json(server, capsys) -> None:
    server.reply("DELETE", f"/api/v1/hubs/{HUB}", None, status=204)
    server.reply("DELETE", f"/api/v1/hubs/{HUB}/sharing", None, status=204)
    server.reply("DELETE", f"/api/v1/hubs/{HUB}/membership", None, status=204)
    assert cli.main(["hubs", "delete", HUB, "--yes"]) == 0
    assert "Deleted the hub." in capsys.readouterr().err
    assert cli.main(["hubs", "revoke", HUB, "--yes"]) == 0
    assert "Stopped sharing the hub." in capsys.readouterr().err
    assert cli.main(["hubs", "leave", HUB, "--yes", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "id": HUB}


def test_pin_unpin_and_take(server, capsys) -> None:
    server.reply("PUT", f"/api/v1/hubs/{HUB}/pin", _detail(pinned=True))
    server.reply("DELETE", f"/api/v1/hubs/{HUB}/pin", _detail())
    server.reply("PUT", f"/api/v1/hubs/{HUB}/membership", _detail(role="view", owner_name="Sam"))
    assert cli.main(["hubs", "pin", HUB]) == 0
    assert "focused on 'Trip'" in capsys.readouterr().err
    assert cli.main(["hubs", "unpin", HUB]) == 0
    assert "Unpinned 'Trip'." in capsys.readouterr().err
    assert cli.main(["hubs", "take", HUB]) == 0
    assert "Took 'Trip'." in capsys.readouterr().err


def test_share_sends_the_level_asked_for(server, capsys) -> None:
    server.reply("PUT", f"/api/v1/hubs/{HUB}/sharing", _detail(shared_level="edit"))
    assert cli.main(["hubs", "share", HUB, "--level", "edit"]) == 0
    assert "at least two of its rooms can edit." in capsys.readouterr().err
    assert json.loads(server.seen[-1].content) == {"level": "edit"}
    assert server.calls() == [("PUT", f"/api/v1/hubs/{HUB}/sharing")]


def test_share_with_no_level_writes_nothing_to_a_hub_already_shared(server, capsys) -> None:
    server.reply("GET", f"/api/v1/hubs/{HUB}", _detail(shared_level="edit"))
    assert cli.main(["hubs", "share", HUB]) == 0
    assert "can edit." in capsys.readouterr().err
    assert server.calls() == [("GET", f"/api/v1/hubs/{HUB}")]


def test_share_with_no_level_shares_an_unshared_hub_to_view(server) -> None:
    server.reply("GET", f"/api/v1/hubs/{HUB}", _detail())
    server.reply("PUT", f"/api/v1/hubs/{HUB}/sharing", _detail(shared_level="view"))
    assert cli.main(["hubs", "share", HUB]) == 0
    assert json.loads(server.seen[-1].content) == {"level": "view"}


def test_a_name_is_not_trusted_when_the_list_may_be_short(server, capsys) -> None:
    server.reply("GET", "/api/v1/hubs", {**_LIST, "rooms_complete": False})
    assert cli.main(["hubs", "take", "Garden", "--json"]) == 2
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "ambiguous" and "use its id" in error["message"]
    assert server.calls() == [("GET", "/api/v1/hubs")]


def test_notify_sets_every_room_or_one(server, capsys) -> None:
    server.reply("PUT", f"/api/v1/hubs/{HUB}/notifications", _detail())
    assert cli.main(["hubs", "notify", HUB, "--level", "muted"]) == 0
    assert "Set your rooms in 'Trip' to muted." in capsys.readouterr().err
    assert json.loads(server.seen[-1].content) == {"level": "muted"}
    assert cli.main(["hubs", "notify", HUB, "--level", "all", "--room", ROOM]) == 0
    assert "Set that room to all messages." in capsys.readouterr().err
    assert json.loads(server.seen[-1].content) == {"level": "all", "room_id": ROOM}


@pytest.mark.parametrize(
    ("status", "detail", "code", "exit_code", "said"),
    [
        (404, "Not found.", "not_found", 2, "hubs are not turned on"),
        (403, "That isn't a room you are in.", "forbidden", 1, "isn't a room you are in"),
        (409, "You already have a hub with that name.", "conflict", 1, "already have a hub"),
        (503, "Pepper couldn't check your rooms just now.", "server", 1, "couldn't check"),
    ],
)
def test_refusals_are_said_plainly(server, capsys, status, detail, code, exit_code, said) -> None:
    server.reply("PUT", f"/api/v1/hubs/{HUB}/rooms/{ROOM}", {"detail": detail}, status=status)
    assert cli.main(["hubs", "add", HUB, "--room", ROOM, "--json"]) == exit_code
    captured = capsys.readouterr()
    error = json.loads(captured.err)["error"]
    assert captured.out == "" and error["code"] == code and said in error["message"]


def test_a_malformed_hub_is_an_error_not_a_crash(server, capsys) -> None:
    server.reply("GET", f"/api/v1/hubs/{HUB}", {"id": HUB, "name": "Trip"})
    assert cli.main(["hubs", "show", HUB]) == 1
    assert "role" in capsys.readouterr().err


def test_server_text_cannot_write_to_the_terminal(server, capsys) -> None:
    nasty = "Trip\x1b[2J[bold]x[/bold]"
    server.reply("GET", f"/api/v1/hubs/{HUB}", _detail(name=nasty))
    assert cli.main(["hubs", "show", HUB]) == 0
    out = capsys.readouterr().out
    assert "\x1b[2J" not in out and "[bold]x[/bold]" in out


def test_a_404_on_an_item_names_the_item_too(server, capsys) -> None:
    server.reply("PUT", f"/api/v1/hubs/{HUB}/items/{NOTE}", {"detail": "Not found."}, status=404)
    assert cli.main(["hubs", "add", HUB, "--item", NOTE]) == 2
    assert "no such item of your own" in capsys.readouterr().err


def test_a_role_this_client_does_not_know_reads_as_view(server, capsys) -> None:
    server.reply("GET", f"/api/v1/hubs/{HUB}", _detail(role="admin", owner_name="Chris"))
    assert cli.main(["hubs", "show", HUB]) == 0
    assert "Chris's. You can view." in capsys.readouterr().out
