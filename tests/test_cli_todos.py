"""End-to-end tests for the ``pepper todos`` subcommands.

Same approach as ``test_cli.py``: invoke ``cli.main([...])`` with a
monkeypatched ``api_client.ApiClient`` so commands run without the
network, and exercise credentials through the real (tmp-home) module.
A local fake is used rather than extending ``test_cli.py``'s shared one
so the todo surface stays self-contained.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest

from pepper_cli import api_client, cli, credentials
from pepper_cli.render import _format_due
from pepper_cli.secret import SecretStr

_ROOM = "00000000-0000-0000-0000-0000000000a1"


def _todo(**over) -> api_client.TodoEntry:
    base = {
        "id": "00000000-0000-0000-0000-000000000010",
        "title": "Buy milk",
        "details": None,
        "status": "open",
        "source": "user",
        "priority": None,
        "due_date": None,
        "due_is_date_only": None,
        "created_at": "2026-06-05T00:00:00Z",
        "done_at": None,
        "source_ref": None,
    }
    base.update(over)
    return api_client.TodoEntry(**base)


class _FakeTodoApi:
    """Records calls and returns canned todo responses (or raises)."""

    instances: list[_FakeTodoApi] = []

    def __init__(self, *, server_url, token, **canned) -> None:
        self.server_url = server_url
        self.token = token
        self._canned = canned
        self.calls: list[tuple] = []
        _FakeTodoApi.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def _result(self, key):
        value = self._canned.get(key)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise AssertionError(f"test forgot to set {key}")
        return value

    def list_todos(self, *, status="open", group_id=None):
        self.calls.append(("list", status, group_id))
        return self._result("todo_list")

    def create_todo(
        self, *, title, details=None, priority=None, due_date=None, due=None, group_id=None
    ):
        self.calls.append(("create", title, details, priority, due_date, due, group_id))
        return self._result("todo_created")

    def complete_todo(self, todo_id, *, group_id=None):
        self.calls.append(("done", todo_id, group_id))
        return self._result("todo_done")

    def reopen_todo(self, todo_id, *, group_id=None):
        self.calls.append(("reopen", todo_id, group_id))
        return self._result("todo_done")

    def list_groups(self, *, archived=False):
        return api_client.GroupList(
            enabled=True,
            degraded=False,
            total=1,
            archived_total=0,
            groups=[
                api_client.GroupSummary(
                    group_id=_ROOM,
                    display_name="Trip",
                    color="#fff",
                    sub="",
                    member_count=2,
                    archived=False,
                )
            ],
        )

    def dismiss_todo(self, todo_id, *, group_id=None):
        self.calls.append(("dismiss", todo_id, group_id))
        return self._result("todo_dismissed")


@pytest.fixture
def patch_api(monkeypatch):
    _FakeTodoApi.instances.clear()

    def install(**canned):
        def factory(**kwargs):
            return _FakeTodoApi(**canned, **kwargs)

        monkeypatch.setattr(api_client, "ApiClient", factory)

    return install


def _login(tmp_home: Path) -> None:
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))


# ---------------------------------------------------------------------------
# list


def test_todos_list_renders_titles_and_ids(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(todo_list=[_todo(title="Buy milk"), _todo(id="abc", title="Call dentist")])
    rc = cli.main(["todos", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Buy milk" in out
    assert "Call dentist" in out
    # default status passed through
    assert _FakeTodoApi.instances[-1].calls == [("list", "open", None)]


def test_todos_list_json_dumps_rows(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(todo_list=[_todo(title="Buy milk")])
    assert cli.main(["todos", "list", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["items"][0]["title"] == "Buy milk"
    assert out["next_before"] is None


def test_todos_list_passes_status_flag(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(todo_list=[])
    rc = cli.main(["todos", "list", "--status", "all"])
    assert rc == 0
    assert _FakeTodoApi.instances[-1].calls == [("list", "all", None)]


def test_todos_list_empty(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(todo_list=[])
    rc = cli.main(["todos", "list"])
    assert rc == 0
    assert "(no todos)" in capsys.readouterr().out


def test_todos_list_without_credentials_errors(tmp_home, patch_api) -> None:
    patch_api(todo_list=[])
    rc = cli.main(["todos", "list"])
    assert rc == 1


# ---------------------------------------------------------------------------
# render: due_is_date_only


def test_format_due_date_only_shows_the_bare_utc_day_with_no_clock(machine_zone) -> None:
    # America/Los_Angeles is UTC-7 in July: a machine there would render
    # midnight UTC as the *previous* evening if `.astimezone()` ran, so this
    # zone is the one that actually catches a regression — a UTC test
    # machine can't tell "no conversion" from "conversion that happens to
    # land on the same day".
    machine_zone("America/Los_Angeles")
    assert _format_due("2026-07-31T00:00:00Z", True) == "2026-07-31"


def test_format_due_date_only_never_converts_to_the_local_zone(machine_zone) -> None:
    # Same instant, explicit offset spelling, same zone — still just the UTC
    # day. (`.astimezone()` is what would otherwise shift this to the wrong
    # calendar day for a reader west of UTC.)
    machine_zone("America/Los_Angeles")
    assert _format_due("2026-07-31T00:00:00+00:00", True) == "2026-07-31"


def test_todos_list_renders_date_only_due_with_no_clock(
    tmp_home, patch_api, capsys, machine_zone
) -> None:
    machine_zone("America/Los_Angeles")
    _login(tmp_home)
    patch_api(
        todo_list=[
            _todo(title="Renew passport", due_date="2026-07-31T00:00:00Z", due_is_date_only=True)
        ]
    )
    rc = cli.main(["todos", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    # Reintroducing `.astimezone()` here would show "2026-07-30 17:00" —
    # the wrong day *and* a clock — so this pins both failure modes at once.
    assert "due 2026-07-31" in out
    assert "2026-07-30" not in out
    assert not re.search(r"\d{2}:\d{2}", out)


# ---------------------------------------------------------------------------
# add


def test_todos_add_joins_title_and_parses_due(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(todo_created=_todo(title="buy milk"))
    rc = cli.main(["todos", "create", "buy", "milk", "--due", "2026-06-10", "--priority", "high"])
    assert rc == 0
    create = _FakeTodoApi.instances[-1].calls[-1]
    assert create[0] == "create"
    assert create[1] == "buy milk"  # joined
    assert create[3] == "high"  # priority
    # due parsed to an aware ISO string for that local date
    assert create[4] is not None
    assert create[4].startswith("2026-06-10")
    assert create[5:] == (None, None)  # not the date-only form, not a room
    assert "Added todo" in capsys.readouterr().err


def test_todos_add_rejects_bad_due_without_calling_api(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(todo_created=_todo())
    rc = cli.main(["todos", "create", "x", "--due", "not-a-date"])
    assert rc == 2
    # never constructed a client / made a call
    assert _FakeTodoApi.instances == []


# ---------------------------------------------------------------------------
# done / dismiss


def test_todos_done_calls_complete(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    tid = "00000000-0000-0000-0000-000000000010"
    patch_api(todo_done=_todo(id=tid, status="done"))
    rc = cli.main(["todos", "done", tid])
    assert rc == 0
    assert _FakeTodoApi.instances[-1].calls == [("done", tid, None)]
    assert "Marked done" in capsys.readouterr().err


def test_todos_done_and_reopen_in_a_room_tick_through_the_room(tmp_home, patch_api) -> None:
    _login(tmp_home)
    tid = "00000000-0000-0000-0000-000000000010"
    patch_api(todo_done=_todo(id=tid))
    assert cli.main(["todos", "done", tid, "--room", "trip"]) == 0
    assert cli.main(["todos", "reopen", tid, "--room", _ROOM]) == 0
    assert [c for i in _FakeTodoApi.instances for c in i.calls] == [
        ("done", tid, _ROOM),
        ("reopen", tid, _ROOM),
    ]


def test_room_todos_list_add_dismiss_use_the_room_routes(tmp_home, patch_api) -> None:
    _login(tmp_home)
    tid = "00000000-0000-0000-0000-000000000010"
    patch_api(
        todo_list=[_todo()],
        todo_created=_todo(),
        todo_dismissed=_todo(id=tid, status="dismissed", title="Book"),
    )
    assert cli.main(["todos", "list", "--room", "Trip", "--status", "all"]) == 0
    assert cli.main(["todos", "create", "Book", "--due", "2026-10-09", "--room", "Trip"]) == 0
    assert cli.main(["todos", "dismiss", tid, "--room", "Trip"]) == 0
    calls = [c for i in _FakeTodoApi.instances for c in i.calls]
    assert calls == [
        ("list", "all", _ROOM),
        ("create", "Book", None, None, None, "2026-10-09", _ROOM),
        ("dismiss", tid, _ROOM),
    ]


def test_room_todo_add_sends_details_and_priority(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(todo_created=_todo())
    argv = ["todos", "create", "x", "--details", "d", "--priority", "high", "--room", "Trip"]
    assert cli.main(argv) == 0
    assert _FakeTodoApi.instances[-1].calls[-1] == ("create", "x", "d", "high", None, None, _ROOM)


def test_todos_dismiss_calls_dismiss(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    tid = "00000000-0000-0000-0000-000000000010"
    patch_api(todo_dismissed=_todo(id=tid, status="dismissed", title="drop me"))
    rc = cli.main(["todos", "dismiss", tid])
    assert rc == 0
    assert _FakeTodoApi.instances[-1].calls == [("dismiss", tid, None)]
    assert "Dismissed" in capsys.readouterr().err


def test_todos_done_rejects_non_uuid(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(todo_done=_todo())
    rc = cli.main(["todos", "done", "not-a-uuid"])
    assert rc == 2
    assert _FakeTodoApi.instances == []  # validated before any call


def test_todos_done_404_exits_one(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    tid = "00000000-0000-0000-0000-000000000099"
    patch_api(todo_done=api_client.ApiError(status=404, body="", detail="todo not found"))
    rc = cli.main(["todos", "done", tid])
    assert rc == 1
    assert "404" in capsys.readouterr().err


def test_todos_done_handles_unreachable_server(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    tid = "00000000-0000-0000-0000-000000000010"
    patch_api(todo_done=httpx.ConnectError("boom"))
    rc = cli.main(["todos", "done", tid])
    assert rc == 1
    assert "Couldn't reach" in capsys.readouterr().err
