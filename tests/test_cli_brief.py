"""``pepper brief`` against a mocked server (synthetic data; ``--now`` is a mock, never prod)."""

from __future__ import annotations

import json

import httpx
import pytest

from pepper_cli import api_client, brief_view, cli, credentials
from pepper_cli.secret import SecretStr

NEWEST = {
    "id": "r2",
    "date": "2026-10-17",
    "dateline": "Saturday, Oct 17",
    "delivered_at": "7:00 AM",
    "text": "plain",
    "sections": {
        "structured": True,
        "lede": "Sunny \x1b[2Jday",
        "events": [{"time": "9:00", "title": "Standup"}],
        "lead_ins": [{"name": "Weather", "value": "72"}],
        "todos": [{"title": "Call Dana"}],
        "stories": [{"headline": "Big news", "context": "Why", "url": "https://example.com/n"}],
        "rest": ["Have a good one."],
    },
}
OLDER = {
    "id": "r1",
    "date": "2026-10-16",
    "dateline": "Friday, Oct 16",
    "text": "Older &amp; wiser",
}


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("PEPPER_JSON", raising=False)
    credentials.save(server_url="https://pepper.example.com", token=SecretStr("pep_pat_abc"))
    state = {"doc": {"rev": 1, "data": {"configured": True, "briefs": [NEWEST, OLDER]}}}
    calls: list[tuple[str, str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.content))
        if request.url.path == "/api/v1/apps/brief/data":
            return httpx.Response(200, json=state["doc"])
        if request.url.path == "/api/v1/apps/brief/actions/run_now":
            return httpx.Response(200, json={"result": {"started": True, "message": "Queued."}})
        return httpx.Response(404, json={})

    real = api_client.ApiClient
    monkeypatch.setattr(
        api_client,
        "ApiClient",
        lambda **kw: real(client=httpx.Client(transport=httpx.MockTransport(handler)), **kw),
    )
    return type("Server", (), {"state": state, "calls": calls})


def _run(capsys, *argv: str) -> tuple[int, str, str]:
    rc = cli.main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def test_latest_is_the_first_edition_as_given(server, capsys) -> None:
    rc, out, _ = _run(capsys, "brief")
    assert rc == 0
    assert "Saturday, Oct 17" in out and "Standup" in out and "Weather" in out
    assert "Call Dana" in out and "Big news" in out and "https://example.com/n" in out
    assert "\x1b" not in out


def test_the_cli_does_not_sort_editions(server, capsys) -> None:
    """Whatever order the server sends is the order used: the first edition is "latest"."""
    server.state["doc"]["data"]["briefs"] = [OLDER, NEWEST]
    rc, out, _ = _run(capsys, "brief", "--json")
    assert rc == 0 and json.loads(out)["date"] == "2026-10-16"


def test_date_is_a_plain_lookup_by_the_date_field(server, capsys) -> None:
    rc, out, _ = _run(capsys, "brief", "--date", "2026-10-16")
    assert rc == 0 and "Friday, Oct 16" in out and "Older & wiser" in out
    rc, _, err = _run(capsys, "brief", "--date", "2026-01-01", "--json")
    assert rc == 1 and json.loads(err)["error"]["code"] == "not_found"


def test_json_is_the_edition(server, capsys) -> None:
    rc, out, _ = _run(capsys, "brief", "--json")
    assert rc == 0 and json.loads(out)["id"] == "r2"


@pytest.mark.parametrize(
    ("configured", "words"),
    [(True, "No brief has been delivered"), (False, "No brief is set up")],
)
def test_no_editions(server, capsys, configured, words) -> None:
    server.state["doc"]["data"] = {"configured": configured, "briefs": []}
    rc, _, err = _run(capsys, "brief")
    assert rc == 1 and words in err


def test_now_runs_the_action_and_prints_the_answer(server, capsys) -> None:
    rc, _, err = _run(capsys, "brief", "--now")
    assert rc == 0 and "Queued." in err
    assert [c[1] for c in server.calls] == ["/api/v1/apps/brief/actions/run_now"]
    assert json.loads(server.calls[0][2]) == {"params": {}}


def test_now_with_a_date_is_a_usage_error(server, capsys, monkeypatch) -> None:
    rc, _, err = _run(capsys, "brief", "--now", "--date", "2026-10-16", "--json")
    assert rc == 2 and json.loads(err)["error"]["code"] == "usage" and not server.calls


def test_editions_of_ignores_non_objects() -> None:
    doc = {"data": {"briefs": [OLDER, "junk", None]}}
    assert brief_view.editions_of(doc) == [OLDER]
    assert brief_view.editions_of({}) == []


def test_plain_text_edition_and_empty_text(server, capsys) -> None:
    server.state["doc"]["data"]["briefs"] = [{"date": "2026-10-17", "text": ""}]
    rc, out, _ = _run(capsys, "brief")
    assert rc == 0 and "(empty)" in out
