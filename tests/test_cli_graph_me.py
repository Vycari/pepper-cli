"""``pepper graph me``: list, mark and unmark the addresses that are the owner's own."""

from __future__ import annotations

import json

import httpx
import pytest

from pepper_cli import api_client, cli, credentials
from pepper_cli.secret import SecretStr

_A = "0b0a0000-0000-4000-8000-00000000000a"
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


_ME = f"{_BASE}/me"


def test_me_lists_marks(server, capsys) -> None:
    marks = [
        {"id": "m1", "address": "old@work.example", "created_at": "2026-11-15T10:00:00Z"},
        {"id": "m2", "address": "me@home.example", "created_at": "2026-11-16T10:00:00Z"},
    ]
    server.reply("GET", _ME, {"marks": marks})
    assert cli.main(["graph", "me"]) == 0
    out = capsys.readouterr().out
    assert "old@work.example" in out and "me@home.example" in out


def test_me_empty_and_json_envelope(server, capsys) -> None:
    server.reply("GET", _ME, {"marks": []})
    assert cli.main(["graph", "me"]) == 0
    assert "no addresses marked" in capsys.readouterr().out
    assert cli.main(["graph", "me", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"items": [], "next_before": None}


def test_mark_sends_the_named_addresses(server, capsys) -> None:
    server.reply("POST", _ME, {"marked": 2, "deleted": 1, "stripped": 1})
    argv = ["graph", "me", "mark", _A, "--address", "a@x.io", "--address", " b@x.io "]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "Marked 2 address(es)" in out and "1 entity(ies) left the graph" in out
    assert "1 address(es) taken off" in out
    assert json.loads(server.seen[0].content) == {
        "entity_id": _A,
        "addresses": ["a@x.io", "b@x.io"],
    }


def test_mark_all_sends_all_and_no_addresses(server, capsys) -> None:
    server.reply("POST", _ME, {"marked": 3, "deleted": 0, "stripped": 0})
    assert cli.main(["graph", "me", "mark", _A, "--all", "--json"]) == 0
    assert json.loads(server.seen[0].content) == {"entity_id": _A, "all": True}
    assert json.loads(capsys.readouterr().out) == {"marked": 3, "deleted": 0, "stripped": 0}


def test_mark_already_marked_says_nothing_changed(server, capsys) -> None:
    server.reply("POST", _ME, {"marked": 0, "deleted": 0, "stripped": 0})
    assert cli.main(["graph", "me", "mark", _A, "--all"]) == 0
    assert "already yours" in capsys.readouterr().out


def test_mark_needs_exactly_one_of_address_and_all(server, capsys) -> None:
    for extra in ([], ["--all", "--address", "a@x.io"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(["graph", "me", "mark", _A, *extra])
        assert exc.value.code == 2
    assert server.seen == []
    capsys.readouterr()


def test_a_blank_address_is_a_usage_error(server, capsys) -> None:
    assert cli.main(["graph", "me", "mark", _A, "--address", " "]) == 2
    assert cli.main(["graph", "me", "unmark", " "]) == 2
    assert server.seen == []
    capsys.readouterr()


@pytest.mark.parametrize(
    "reason,text",
    [
        ("not_a_person", "not a person"),
        ("already_merged", "already merged"),
        ("groups_linked", "linked to a person elsewhere"),
        ("household_address", "household"),
        ("address_not_on_entity", "not on that entity"),
        ("invalid_address", "not a usable email address"),
        ("no_addresses", "no email address"),
        ("too_many_addresses", "up to 50"),
    ],
)
def test_each_refusal_reason_has_a_one_line_message(server, capsys, reason, text) -> None:
    server.reply("POST", _ME, {"detail": reason}, status=409)
    assert cli.main(["graph", "me", "mark", _A, "--all"]) == 1
    captured = capsys.readouterr()
    assert f"Refused ({reason})" in captured.err and text in captured.err
    assert len(captured.err.strip().splitlines()) == 1 and captured.out == ""


def test_a_refusal_in_json_mode_is_a_conflict_error_line(server, capsys) -> None:
    server.reply("POST", _ME, {"detail": "household_address"}, status=409)
    assert cli.main(["graph", "me", "mark", _A, "--all", "--json"]) == 1
    err = json.loads(capsys.readouterr().err)["error"]
    assert (
        err["code"] == "conflict" and err["status"] == 409 and "household_address" in err["message"]
    )


def test_groups_unavailable_says_to_retry(server, capsys) -> None:
    server.reply("POST", _ME, {"detail": "groups_unavailable"}, status=503)
    assert cli.main(["graph", "me", "mark", _A, "--all"]) == 1
    err = capsys.readouterr().err
    assert "groups_unavailable" in err and "Try again" in err


def test_an_unknown_reason_is_shown_as_sent(server, capsys) -> None:
    server.reply("POST", _ME, {"detail": "brand_new_reason"}, status=409)
    assert cli.main(["graph", "me", "mark", _A, "--all"]) == 1
    assert "Refused: brand_new_reason" in capsys.readouterr().err


def test_a_404_says_the_graph_is_not_available(server, capsys) -> None:
    server.reply("GET", _ME, {"detail": "Not Found"}, status=404)
    assert cli.main(["graph", "me"]) == 1
    assert "not available for your account" in capsys.readouterr().err


def test_unmark_deletes_with_the_address_in_the_body(server, capsys) -> None:
    server.reply("DELETE", _ME, {"unmarked": 1})
    assert cli.main(["graph", "me", "unmark", " old@work.example "]) == 0
    assert "Unmarked" in capsys.readouterr().out
    assert server.seen[0].method == "DELETE"
    assert json.loads(server.seen[0].content) == {"address": "old@work.example"}


def test_unmark_of_an_unmarked_address_says_so(server, capsys) -> None:
    server.reply("DELETE", _ME, {"unmarked": 0})
    assert cli.main(["graph", "me", "unmark", "x@y.io"]) == 0
    assert "was not marked" in capsys.readouterr().out
    assert cli.main(["graph", "me", "--json", "unmark", "x@y.io"]) == 0
    assert json.loads(capsys.readouterr().out) == {"unmarked": 0}
