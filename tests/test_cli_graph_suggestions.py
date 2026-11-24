"""``pepper graph suggestions`` and ``graph decide``, and the entity page's open questions."""

from __future__ import annotations

import json

import httpx
import pytest

from pepper_cli import api_client, cli, credentials
from pepper_cli.secret import SecretStr

_A = "0b0a0000-0000-4000-8000-00000000000a"
_B = "0b0a0000-0000-4000-8000-00000000000b"
_S = "0b0a0000-0000-4000-8000-0000000000d1"
_LOG = "0b0a0000-0000-4000-8000-0000000000f1"
_BASE = "/api/v1/graph"
_QUESTION = {
    "suggestion_id": _S,
    "kind": "project",
    "a": {"entity_id": _A, "name": "Kitchen redo", "kind": "project"},
    "b": {"entity_id": _B, "name": "Kitchen remodel", "kind": "project"},
    "confidence": 0.72,
    "created_at": "2026-11-24T10:00:00Z",
}


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


def test_suggestions_lists_each_question_with_its_answer_command(server, capsys) -> None:
    server.reply("GET", f"{_BASE}/suggestions", {"suggestions": [_QUESTION]})
    assert cli.main(["graph", "suggestions"]) == 0
    out = capsys.readouterr().out
    assert "Kitchen redo (project)" in out and "Kitchen remodel (project)" in out
    assert "72% sure" in out
    assert f"pepper graph decide {_S} same|different|not-sure" in out
    assert "entity_id" not in server.seen[0].url.params


def test_suggestions_narrows_to_one_entity(server, capsys) -> None:
    server.reply("GET", f"{_BASE}/suggestions", {"suggestions": []})
    assert cli.main(["graph", "suggestions", "--entity", _A]) == 0
    assert "(no open questions)" in capsys.readouterr().out
    assert server.seen[0].url.params["entity_id"] == _A


def test_suggestions_json_is_the_list_envelope(server, capsys) -> None:
    server.reply("GET", f"{_BASE}/suggestions", {"suggestions": [_QUESTION]})
    assert cli.main(["graph", "suggestions", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["items"][0]["suggestion_id"] == _S


def test_a_blank_entity_is_a_usage_error(server, capsys) -> None:
    assert cli.main(["graph", "suggestions", "--entity", " "]) == 2
    assert server.seen == []
    assert "--entity must not be blank" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("typed", "wire", "status", "text"),
    [
        ("different", "different", "rejected", "Kept apart"),
        ("not-sure", "not_sure", "snoozed", "ask again in 30 days"),
    ],
)
def test_decide_sends_the_wire_answer(server, capsys, typed, wire, status, text) -> None:
    server.reply(
        "POST", f"{_BASE}/suggestions/{_S}/decide", {"suggestion_id": _S, "status": status}
    )
    assert cli.main(["graph", "decide", _S, typed]) == 0
    out = capsys.readouterr().out
    assert text in out and "undo" not in out
    assert json.loads(server.seen[0].content) == {"answer": wire}


def test_a_same_answer_names_the_merge_and_its_undo(server, capsys) -> None:
    body = {"suggestion_id": _S, "status": "accepted", "merge_log_id": _LOG, "survivor_id": _B}
    server.reply("POST", f"{_BASE}/suggestions/{_S}/decide", body)
    assert cli.main(["graph", "decide", _S, "same"]) == 0
    out = capsys.readouterr().out
    assert "Joined" in out and f"survivor {_B}" in out
    assert f"pepper graph undo {_LOG}" in out


def test_an_answered_question_is_refused_with_its_meaning(server, capsys) -> None:
    server.reply("POST", f"{_BASE}/suggestions/{_S}/decide", {"detail": "decided"}, status=409)
    assert cli.main(["graph", "decide", _S, "same"]) == 1
    assert "Refused (decided): that question is already answered." in capsys.readouterr().err


def test_an_unknown_answer_is_rejected_before_any_request(server, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["graph", "decide", _S, "maybe"])
    assert exc.value.code == 2
    assert server.seen == []


def test_the_entity_page_lists_its_open_questions(server, capsys) -> None:
    page = {
        "status": "ok",
        "entity": {
            "entity": {"id": _A, "kind": "project", "subtype": None, "name": "Kitchen redo"},
            "sensitive": False,
            "requested_id": _A,
            "catalog": None,
            "edges": [],
            "suggestions": [_QUESTION],
        },
    }
    server.reply("GET", f"{_BASE}/entities/{_A}", page)
    assert cli.main(["graph", "entity", _A]) == 0
    out = capsys.readouterr().out
    assert "(no edges)" in out and "Open questions" in out
    assert f"pepper graph decide {_S}" in out


def test_an_entity_page_without_questions_prints_no_heading(server, capsys) -> None:
    page = {
        "status": "ok",
        "entity": {
            "entity": {"id": _A, "kind": "project", "subtype": None, "name": "Kitchen redo"},
            "sensitive": False,
            "requested_id": _A,
            "catalog": None,
            "edges": [],
        },
    }
    server.reply("GET", f"{_BASE}/entities/{_A}", page)
    assert cli.main(["graph", "entity", _A]) == 0
    assert "Open questions" not in capsys.readouterr().out
