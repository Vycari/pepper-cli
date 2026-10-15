"""Phase 2d: J8 error lines under ``--json`` and the runs/thread keyset cursor (§5.3)."""

from __future__ import annotations

import json

import httpx
import pytest

from pepper_cli import api_client, cli, credentials
from pepper_cli.secret import SecretStr

TASK = "00000000-0000-0000-0000-0000000000a8"
RUNS = f"/api/v1/scheduled-tasks/{TASK}/runs"


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("PEPPER_JSON", raising=False)
    monkeypatch.chdir(tmp_path)
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
    return type("Server", (), {"requests": requests, "routes": routes})


def _error(capsys) -> dict:
    captured = capsys.readouterr()
    assert captured.out == ""
    lines = [ln for ln in captured.err.splitlines() if ln.strip()]
    assert len(lines) == 1
    return json.loads(lines[0])["error"]


@pytest.mark.parametrize(
    "argv",
    [
        ["todos", "list"],
        ["scheduled-tasks", "list"],
        ["skills", "list"],
        ["notes", "list"],
        ["whoami"],
    ],
)
def test_expired_token_is_an_auth_error_line(server, capsys, argv) -> None:
    for path in (
        "/api/v1/todos",
        "/api/v1/scheduled-tasks",
        "/api/v1/skills",
        "/api/v1/whoami",
        "/api/v1/artifacts",
    ):
        server.routes[("GET", path)] = httpx.Response(401, json={"detail": "bad token"})
    rc = cli.main([*argv, "--json"])
    assert rc != 0
    err = _error(capsys)
    assert err["code"] == "auth"
    assert "pep_pat_abc" not in json.dumps(err)


def test_unknown_skill_is_not_found(server, capsys) -> None:
    server.routes[("GET", "/api/v1/skills/nope")] = httpx.Response(404, json={"detail": "no"})
    assert cli.main(["skills", "show", "nope", "--json"]) != 0
    assert _error(capsys)["code"] == "not_found"


def test_json_mode_never_opens_an_editor(server, capsys) -> None:
    assert cli.main(["notes", "create", "Runbook", "--json"]) == 2
    assert _error(capsys)["code"] == "usage"
    assert cli.main(["skills", "create", "triage", "--json"]) == 2
    assert _error(capsys)["code"] == "usage"
    assert server.requests == []


def test_json_delete_without_yes_is_usage(server, capsys) -> None:
    assert cli.main(["skills", "delete", "triage", "--json"]) == 2
    assert _error(capsys)["code"] == "usage"


def test_bad_page_limit_is_usage(server, capsys) -> None:
    assert cli.main(["scheduled-tasks", "runs", TASK, "--limit", "500", "--json"]) == 2
    assert _error(capsys)["code"] == "usage"


def _run(i: int) -> dict:
    return {"id": f"r{i}", "fired_at": f"2026-10-01T08:00:0{i}+00:00", "status": "ok"}


def test_runs_cursor_round_trips(server, capsys) -> None:
    server.routes[("GET", f"/api/v1/scheduled-tasks/{TASK}")] = httpx.Response(
        200,
        json={
            "id": TASK,
            "name": "Brief",
            "prompt": "p",
            "schedule": "0 8 * * *",
            "active": True,
        },
    )
    server.routes[("GET", RUNS)] = httpx.Response(200, json=[_run(2), _run(1)])
    assert cli.main(["scheduled-tasks", "runs", TASK, "--limit", "2", "--json"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in first["items"]] == ["r2", "r1"]
    cursor = first["next_before"]
    assert cursor == "2026-10-01T08:00:01+00:00|r1"

    server.routes[("GET", RUNS)] = httpx.Response(200, json=[_run(0)])
    assert cli.main(["scheduled-tasks", "runs", TASK, "--before", cursor, "--json"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["next_before"] is None
    params = server.requests[-1].url.params
    assert params["before"] == "2026-10-01T08:00:01+00:00"
    assert params["before_id"] == "r1"


def test_bare_timestamp_cursor_is_accepted(server, capsys) -> None:
    server.routes[("GET", RUNS)] = httpx.Response(200, json=[])
    assert cli.main(["scheduled-tasks", "runs", TASK, "--before", "2026-10-01", "--json"]) == 0
    capsys.readouterr()
    params = server.requests[-1].url.params
    assert params["before"] == "2026-10-01"
    assert "before_id" not in params


def test_saved_flags_tolerates_a_field_without_a_flag_name() -> None:
    # A future edit field must not raise inside the partial-save error path (#26).
    assert cli._saved_flags({"future_field": 1}) == "future_field"
    assert cli._saved_flags({"description": None}) == "--clear-description"


def test_repl_startup_errors_stay_text_under_pepper_json(server, capsys, monkeypatch) -> None:
    """J1: the REPL never emits JSON, even when ``PEPPER_JSON=1`` is exported."""
    monkeypatch.setenv("PEPPER_JSON", "1")
    credentials.delete()
    assert cli.main(["chat"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip()
    assert not captured.err.lstrip().startswith("{")


def test_repl_bad_root_stays_text_under_pepper_json(server, capsys, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PEPPER_JSON", "1")
    assert cli.main(["chat", "--root", str(tmp_path / "missing")]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--root" in captured.err
    assert not captured.err.lstrip().startswith("{")


def test_json_chat_reports_a_failed_tool_result_post_as_a_j8_line(
    server, capsys, monkeypatch
) -> None:
    """A 404 on the tool-result post is a J8 line on stderr, never a text note,
    and the turn still completes (exit 0) with every stdout line JSON."""

    def frame(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    server.routes[("POST", "/api/v1/chat/send")] = httpx.Response(
        200,
        text=frame(
            "client_tool_call",
            {"call_id": "abc", "name": "fs_read", "arguments": {}, "deadline_ms": 20000},
        )
        + frame("reply", {"reply": "done", "steps": []}),
        headers={"content-type": "text/event-stream"},
    )
    server.routes[("POST", "/api/v1/chat/tool-result")] = httpx.Response(
        404, json={"detail": "unknown call_id"}
    )
    assert cli.main(["chat", "hello", "--json"]) == 0
    captured = capsys.readouterr()
    kinds = [json.loads(line)["kind"] for line in captured.out.splitlines()]
    assert kinds == ["client_tool_call", "reply"]
    lines = [ln for ln in captured.err.splitlines() if ln.strip()]
    assert len(lines) == 1
    error = json.loads(lines[0])["error"]
    assert error["code"] == "not_found"
    assert error["status"] == 404
    assert "fs_read" in error["message"]
