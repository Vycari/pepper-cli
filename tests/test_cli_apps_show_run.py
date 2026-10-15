"""``pepper apps show`` / ``apps run`` (#18 part 2) against the real client and a mocked server."""

from __future__ import annotations

import json

import httpx
import pytest

from pepper_cli import api_client, cli, credentials
from pepper_cli.secret import SecretStr

ROOM = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def server(monkeypatch, tmp_path):
    """Real ``ApiClient`` over a ``MockTransport``; ``server.requests`` records each call."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("PEPPER_JSON", raising=False)
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    requests: list[httpx.Request] = []
    routes: dict[tuple[str, str], httpx.Response] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return routes.get((request.method, request.url.path), httpx.Response(404, json={}))

    real = api_client.ApiClient

    def factory(**kwargs):
        return real(client=httpx.Client(transport=httpx.MockTransport(handler)), **kwargs)

    monkeypatch.setattr(api_client, "ApiClient", factory)
    routes[("GET", "/api/v1/groups")] = httpx.Response(
        200,
        json={
            "enabled": True,
            "degraded": False,
            "total": 1,
            "archived_total": 0,
            "groups": [
                {
                    "group_id": ROOM,
                    "display_name": "Family",
                    "color": "#fff",
                    "sub": "",
                    "member_count": 2,
                    "archived": False,
                }
            ],
        },
    )
    return type("Server", (), {"requests": requests, "routes": routes})


def test_show_reads_own_data(server, capsys) -> None:
    server.routes[("GET", "/api/v1/apps/notes/data")] = httpx.Response(
        200, json={"rev": 3, "data": {"k": "v"}}
    )
    assert cli.main(["apps", "show", "notes", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"rev": 3, "data": {"k": "v"}}
    assert "principal" not in server.requests[-1].url.params


def test_show_room_sends_principal_and_human_view_is_indented(server, capsys) -> None:
    server.routes[("GET", "/api/v1/apps/notes/data")] = httpx.Response(
        200, json={"rev": 1, "data": {"k": "v"}}
    )
    assert cli.main(["apps", "show", "notes", "--room", "Family"]) == 0
    assert server.requests[-1].url.params["principal"] == ROOM
    assert '"k": "v"' in capsys.readouterr().out


def test_show_unknown_app_is_a_json_not_found(server, capsys) -> None:
    assert cli.main(["apps", "show", "nope", "--json"]) == 1
    err = json.loads(capsys.readouterr().err)["error"]
    assert (err["code"], err["status"]) == ("not_found", 404)


def test_run_posts_params_as_strings_to_the_room(server, capsys) -> None:
    server.routes[("POST", "/api/v1/apps/notes/actions/archive")] = httpx.Response(
        200, json={"result": {"ok": True}}
    )
    argv = [
        "apps",
        "run",
        "notes",
        "archive",
        "--room",
        ROOM,
        "--param",
        "id=007",
        "--param",
        "x=a=b",
    ]
    assert cli.main([*argv, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"result": {"ok": True}}
    sent = server.requests[-1]
    assert json.loads(sent.content) == {"params": {"id": "007", "x": "a=b"}}
    assert sent.url.params["principal"] == ROOM


def test_run_in_a_room_says_it_writes_before_sending(server, capsys) -> None:
    server.routes[("POST", "/api/v1/apps/notes/actions/archive")] = httpx.Response(
        200, json={"result": {}}
    )
    assert cli.main(["apps", "run", "notes", "archive", "--room", "Family"]) == 0
    assert "writes to the room's 'notes' data" in capsys.readouterr().err


@pytest.mark.parametrize("param", ["noequals", "=v"])
def test_run_rejects_a_malformed_param(server, capsys, param) -> None:
    assert cli.main(["apps", "run", "notes", "a", "--param", param, "--json"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "usage"
    assert server.requests == []


def test_run_rejects_a_repeated_param(server, capsys) -> None:
    argv = ["apps", "run", "notes", "a", "--param", "k=1", "--param", "k=2", "--json"]
    assert cli.main(argv) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "usage"


def test_run_without_an_action_lists_the_owner_scoped_ones(server, capsys) -> None:
    server.routes[("GET", "/api/v1/apps")] = httpx.Response(
        200,
        json=[
            {"slug": "other", "title": "O", "description": "", "owner_scoped_actions": ["x"]},
            {"slug": "notes", "title": "N", "description": "", "owner_scoped_actions": ["delete"]},
        ],
    )
    assert cli.main(["apps", "run", "notes", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "slug": "notes",
        "owner_scoped_actions": ["delete"],
    }
    assert cli.main(["apps", "run", "notes"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "delete"
    assert "owner-scoped actions only" in captured.err


def test_run_without_an_action_on_an_unknown_app(server, capsys) -> None:
    server.routes[("GET", "/api/v1/apps")] = httpx.Response(200, json=[])
    assert cli.main(["apps", "run", "ghost", "--json"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "not_found"


def test_run_forbidden_is_a_json_error(server, capsys) -> None:
    server.routes[("POST", "/api/v1/apps/notes/actions/delete")] = httpx.Response(
        403, json={"detail": "nope"}
    )
    assert cli.main(["apps", "run", "notes", "delete", "--json"]) == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "forbidden"
