"""``pepper graph`` identity commands: merge, undo, duplicates, not-same and households."""

from __future__ import annotations

import json

import httpx
import pytest

from pepper_cli import api_client, cli, credentials
from pepper_cli.secret import SecretStr

_A = "0b0a0000-0000-4000-8000-00000000000a"
_B = "0b0a0000-0000-4000-8000-00000000000b"
_LOG = "0b0a0000-0000-4000-8000-0000000000f1"
_HH = "0b0a0000-0000-4000-8000-0000000000c1"
_BASE = "/api/v1/graph"


@pytest.fixture
def server(monkeypatch, tmp_path):
    """Route every ApiClient through a MockTransport; ``server.reply`` queues a response per
    method and path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("PEPPER_JSON", raising=False)
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    seen: list[httpx.Request] = []
    replies: dict[tuple[str, str], httpx.Response] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return replies[(request.method, request.url.path)]

    real = api_client.ApiClient

    def make(**kw):
        return real(**kw, client=httpx.Client(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(api_client, "ApiClient", make)

    class S:
        pass

    s = S()
    s.seen = seen  # type: ignore[attr-defined]
    s.reply = lambda method, path, body, status=200: replies.__setitem__(  # type: ignore[attr-defined]
        (method, path), httpx.Response(status, json=body)
    )
    return s


def test_merge_posts_both_ids_and_names_the_survivor(server, capsys) -> None:
    body = {"log_id": _LOG, "survivor_id": _B, "merged_id": _A, "aliases": 2}
    server.reply("POST", f"{_BASE}/merge", body)
    assert cli.main(["graph", "merge", _A, _B]) == 0
    out = capsys.readouterr().out
    assert f"Merged {_A} into {_B}" in out and f"pepper graph undo {_LOG}" in out
    assert json.loads(server.seen[0].content) == {"from_id": _A, "into_id": _B}


def test_merge_json_is_the_servers_body(server, capsys) -> None:
    body = {"log_id": _LOG, "survivor_id": _B, "merged_id": _A, "aliases": 2}
    server.reply("POST", f"{_BASE}/merge", body)
    assert cli.main(["graph", "merge", _A, _B, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == body


@pytest.mark.parametrize(
    "reason,text",
    [
        ("different_kind", "different kinds"),
        ("conflicting_person", "linked to a person"),
        ("already_merged", "already merged"),
    ],
)
def test_a_refused_merge_shows_the_reason_and_exits_1(server, capsys, reason, text) -> None:
    server.reply("POST", f"{_BASE}/merge", {"detail": reason}, status=409)
    assert cli.main(["graph", "merge", _A, _B]) == 1
    captured = capsys.readouterr()
    assert f"Refused ({reason})" in captured.err and text in captured.err
    assert captured.out == ""


def test_a_refusal_in_json_mode_is_a_conflict_error_line(server, capsys) -> None:
    server.reply("POST", f"{_BASE}/merge", {"detail": "different_kind"}, status=409)
    assert cli.main(["graph", "merge", _A, _B, "--json"]) == 1
    err = json.loads(capsys.readouterr().err)["error"]
    assert err["code"] == "conflict" and err["status"] == 409 and "different_kind" in err["message"]


def test_an_unknown_reason_code_is_shown_as_sent(server, capsys) -> None:
    server.reply("POST", f"{_BASE}/not-same", {"detail": "brand_new_reason"}, status=409)
    assert cli.main(["graph", "not-same", _A, _B]) == 1
    assert "Refused: brand_new_reason" in capsys.readouterr().err


def test_a_404_says_the_graph_is_not_available(server, capsys) -> None:
    server.reply("POST", f"{_BASE}/merge", {"detail": "Not Found"}, status=404)
    assert cli.main(["graph", "merge", _A, _B]) == 1
    assert "not available for your account" in capsys.readouterr().err


def test_merges_lists_history_with_undo_state(server, capsys) -> None:
    rows = [
        {
            "log_id": _LOG,
            "from_id": _A,
            "from_name": "Casey C",
            "into_id": _B,
            "into_name": None,
            "created_at": "2026-11-15T10:00:00Z",
            "undone_at": None,
            "undoable": True,
        },
        {
            "log_id": "l2",
            "from_id": "x",
            "from_name": "Old",
            "into_id": "y",
            "into_name": "New",
            "created_at": "2026-11-10T10:00:00Z",
            "undone_at": "2026-11-11T10:00:00Z",
            "undoable": False,
        },
    ]
    server.reply("GET", f"{_BASE}/merges", {"merges": rows})
    assert cli.main(["graph", "merges", _B]) == 0
    out = capsys.readouterr().out
    assert "Casey C" in out and "(unnamed)" in out and "undoable" in out
    assert "undone" in out and f"log {_LOG}" in out
    assert server.seen[0].url.params["entity_id"] == _B


def test_merges_json_uses_the_list_envelope_and_empty_says_so(server, capsys) -> None:
    server.reply("GET", f"{_BASE}/merges", {"merges": []})
    assert cli.main(["graph", "merges", _B]) == 0
    assert "no merges" in capsys.readouterr().out
    assert cli.main(["graph", "merges", _B, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"items": [], "next_before": None}


def test_undo_posts_to_the_log_and_reports_the_restored_entity(server, capsys) -> None:
    body = {"log_id": _LOG, "survivor_id": _B, "restored_id": _A, "aliases": 2}
    server.reply("POST", f"{_BASE}/merges/{_LOG}/undo", body)
    assert cli.main(["graph", "undo", _LOG]) == 0
    assert f"{_A} is its own entity again" in capsys.readouterr().out


def test_a_refused_undo_shows_the_reason(server, capsys) -> None:
    server.reply("POST", f"{_BASE}/merges/{_LOG}/undo", {"detail": "later_merge"}, status=409)
    assert cli.main(["graph", "undo", _LOG]) == 1
    err = capsys.readouterr().err
    assert "Refused (later_merge)" in err and "undo that one first" in err


def test_duplicates_lists_pairs_with_next_commands(server, capsys) -> None:
    pair = {"a": {"entity_id": _A, "name": "Casey"}, "b": {"entity_id": _B, "name": "C. Close"}}
    server.reply("GET", f"{_BASE}/duplicates", {"duplicates": [pair]})
    assert cli.main(["graph", "duplicates"]) == 0
    out = capsys.readouterr().out
    assert "Casey" in out and "C. Close" in out
    lines = out.splitlines()
    # Each suggestion is its own line, so copying one never runs the other.
    assert any(line.strip() == f"pepper graph merge {_A} {_B}" for line in lines)
    assert any(line.strip() == f"pepper graph not-same {_A} {_B}" for line in lines)
    assert "|" not in out


def test_no_duplicates_says_so(server, capsys) -> None:
    server.reply("GET", f"{_BASE}/duplicates", {"duplicates": []})
    assert cli.main(["graph", "duplicates"]) == 0
    assert "no likely duplicates" in capsys.readouterr().out


@pytest.mark.parametrize("marked,text", [(True, "Marked as different"), (False, "Already marked")])
def test_not_same_reports_whether_it_marked(server, capsys, marked, text) -> None:
    server.reply("POST", f"{_BASE}/not-same", {"marked": marked})
    assert cli.main(["graph", "not-same", _A, _B]) == 0
    assert text in capsys.readouterr().out
    assert json.loads(server.seen[0].content) == {"a_id": _A, "b_id": _B}


def test_households_lists_addresses_and_members(server, capsys) -> None:
    body = {
        "households": [
            {
                "household_id": _HH,
                "name": "The Closes",
                "addresses": ["home@close.example"],
                "members": [{"entity_id": None, "name": None}, {"entity_id": _A, "name": "Casey"}],
            }
        ]
    }
    server.reply("GET", f"{_BASE}/households", body)
    assert cli.main(["graph", "households"]) == 0
    out = capsys.readouterr().out
    assert "The Closes" in out and "home@close.example" in out
    assert "member: you" in out and "Casey" in out


def test_household_create_sends_members_and_notes_rekinding(server, capsys) -> None:
    body = {"household_id": _HH, "rekinded": True, "members": 2}
    server.reply("POST", f"{_BASE}/households", body)
    argv = ["graph", "household-create", "The Closes", "home@close.example", "--member", _A]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert f"Created household {_HH}" in out and "became the household" in out
    assert json.loads(server.seen[0].content) == {
        "name": "The Closes",
        "address": "home@close.example",
        "member_ids": [_A],
    }


def test_a_refused_household_shows_the_reason(server, capsys) -> None:
    server.reply("POST", f"{_BASE}/households", {"detail": "groups_linked"}, status=409)
    assert cli.main(["graph", "household-create", "n", "a@b.example"]) == 1
    assert "Refused (groups_linked)" in capsys.readouterr().err


@pytest.mark.parametrize("changed,text", [(True, "Added to"), (False, "Already a member")])
def test_household_add(server, capsys, changed, text) -> None:
    server.reply("POST", f"{_BASE}/households/{_HH}/members", {"changed": changed})
    assert cli.main(["graph", "household-add", _HH, _A]) == 0
    assert text in capsys.readouterr().out
    assert json.loads(server.seen[0].content) == {"entity_id": _A}


def test_household_remove_defaults_to_you_and_takes_a_member(server, capsys) -> None:
    server.reply("POST", f"{_BASE}/households/{_HH}/members/remove", {"changed": True})
    assert cli.main(["graph", "household-remove", _HH]) == 0
    assert "Removed from" in capsys.readouterr().out
    assert json.loads(server.seen[0].content) == {"entity_id": None}
    assert cli.main(["graph", "household-remove", _HH, "--member", _A]) == 0
    assert json.loads(server.seen[1].content) == {"entity_id": _A}


def test_household_dissolve_with_and_without_into(server, capsys) -> None:
    body = {"household_id": _HH, "person_id": _A, "aliases": 1}
    server.reply("POST", f"{_BASE}/households/{_HH}/dissolve", body)
    assert cli.main(["graph", "household-dissolve", _HH, "--into", _A]) == 0
    assert f"went back to {_A}" in capsys.readouterr().out
    assert json.loads(server.seen[0].content) == {"into_id": _A}
    assert cli.main(["graph", "household-dissolve", _HH, "--json"]) == 0
    assert json.loads(server.seen[1].content) == {"into_id": None}
    assert json.loads(capsys.readouterr().out) == body


def test_a_busy_server_is_reported_as_a_server_error(server, capsys) -> None:
    server.reply("POST", f"{_BASE}/households/{_HH}/dissolve", {"detail": "busy"}, status=503)
    assert cli.main(["graph", "household-dissolve", _HH]) == 1
    assert "503" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["graph", "merge", " ", _B],
        ["graph", "merges", ""],
        ["graph", "undo", " "],
        ["graph", "not-same", _A, ""],
        ["graph", "household-add", _HH, " "],
        ["graph", "household-create", "n", "a@b.example", "--member", " "],
        ["graph", "household-remove", _HH, "--member", " "],
        ["graph", "household-dissolve", _HH, "--into", " "],
    ],
)
def test_blank_ids_are_refused_before_any_request(server, capsys, argv) -> None:
    assert cli.main(argv) == 2
    assert "must not be blank" in capsys.readouterr().err
    assert server.seen == []
