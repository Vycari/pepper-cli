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
_BOB = "00000000-0000-0000-0000-0000000000b2"
_TID = "00000000-0000-0000-0000-000000000010"


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
        self.scope: list[tuple[bool, bool]] = []
        self.created_with: list[dict] = []
        self.edits: list[tuple[str, dict, str | None]] = []
        self.dismiss_reasons: list[str | None] = []
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

    def list_todos(self, *, status="open", group_id=None, mine=False, asked=False):
        self.calls.append(("list", status, group_id))
        self.scope.append((mine, asked))
        return self._result("todo_list")

    def create_todo(
        self,
        *,
        title,
        details=None,
        priority=None,
        due_date=None,
        due=None,
        group_id=None,
        assignee_person_id=None,
        next_action=None,
    ):
        self.created_with.append(
            {"assignee_person_id": assignee_person_id, "next_action": next_action}
        )
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

    def dismiss_todo(self, todo_id, *, group_id=None, reason=None):
        self.calls.append(("dismiss", todo_id, group_id))
        self.dismiss_reasons.append(reason)
        return self._result("todo_dismissed")

    def edit_todo(self, todo_id, fields, *, group_id=None):
        self.edits.append((todo_id, fields, group_id))
        return self._result("todo_edited")

    def group_roster(self, group_id):
        return [api_client.GroupRosterMember(person_id=_BOB, display_name="Bob Stone")]


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


# ---------------------------------------------------------------------------
# assignees: list --mine/--asked, create --assignee, edit, dismiss --reason


def test_list_mine_and_asked_pass_the_scope(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(todo_list=[])
    assert cli.main(["todos", "list", "--mine"]) == 0
    assert cli.main(["todos", "list", "--asked", "--room", _ROOM]) == 0
    assert [i.scope for i in _FakeTodoApi.instances] == [[(True, False)], [(False, True)]]


def test_list_asked_needs_a_room_and_mine_excludes_asked(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(todo_list=[])
    assert cli.main(["todos", "list", "--asked"]) == 2
    assert "--asked needs --room" in capsys.readouterr().err
    assert _FakeTodoApi.instances == []
    with pytest.raises(SystemExit):
        cli.main(["todos", "list", "--mine", "--asked", "--room", _ROOM])


def test_list_shows_who_asked_whom_and_the_state(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    row = _todo(
        title="Book the venue",
        requested_by=api_client.TodoPerson(user_id="u1", label="Ann"),
        assignee=api_client.TodoPerson(user_id="u2", label="Bob"),
        source_ref={"state": "declined", "reason": "out of town"},
    )
    patch_api(todo_list=[row])
    assert cli.main(["todos", "list", "--room", _ROOM]) == 0
    out = capsys.readouterr().out
    assert "from Ann" in out
    assert "Bob" in out
    assert "declined: out of town" in out


def test_list_shows_a_person_with_no_label_as_unnamed(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    row = _todo(
        requested_by=api_client.TodoPerson(user_id=None, label=None),
        assignee=api_client.TodoPerson(user_id="u2", label=None),
    )
    patch_api(todo_list=[row])
    assert cli.main(["todos", "list", "--room", _ROOM]) == 0
    out = capsys.readouterr().out
    assert "from (unnamed)" in out
    assert "→ (unnamed)" in out
    assert "None" not in out


def test_list_shows_the_next_action_and_pepper_suggestion(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    mine = api_client.TodoNextAction(
        text="Email the vet", by="user", updated_at="2026-10-20T00:00Z"
    )
    pepper = api_client.TodoNextAction(text="Call Bob", by="model", updated_at="2026-10-20T00:00Z")
    suggestion = api_client.TodoNextActionSuggestion(text="Book a slot", updated_at="2026-10-21Z")
    patch_api(
        todo_list=[
            _todo(title="Vet", next_action=mine),
            _todo(title="Trip", next_action=pepper, next_action_suggestion=suggestion),
            _todo(title="Plain"),
        ]
    )
    assert cli.main(["todos", "list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1] == "      Next: Email the vet"
    assert lines[3] == "      Next: Call Bob"
    assert lines[4] == "        Or: Book a slot"
    assert len(lines) == 6  # "Plain" has no extra lines


def test_list_keeps_titles_and_next_actions_in_one_column_past_row_99(
    tmp_home, patch_api, capsys
) -> None:
    _login(tmp_home)
    action = api_client.TodoNextAction(text="Go", by="user", updated_at="2026-10-20T00:00Z")
    patch_api(todo_list=[_todo(title=f"T{i}", next_action=action) for i in range(1, 101)])
    assert cli.main(["todos", "list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("  1. ") and lines[198].startswith("100. ")
    title_col = lines[198].index("T100")
    assert lines[0].index("T1") == title_col
    assert lines[199] == " " * title_col + "Next: Go"
    assert lines[1] == lines[199]


def test_list_json_passes_the_next_action_fields_through(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    action = api_client.TodoNextAction(text="Call Bob", by="model", updated_at="2026-10-20T00:00Z")
    patch_api(todo_list=[_todo(next_action=action)])
    assert cli.main(["todos", "list", "--json"]) == 0
    item = json.loads(capsys.readouterr().out)["items"][0]
    assert item["next_action"] == {
        "text": "Call Bob",
        "by": "model",
        "updated_at": "2026-10-20T00:00Z",
    }
    assert item["next_action_suggestion"] is None


def test_create_sends_the_next_action(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(todo_created=_todo())
    assert cli.main(["todos", "create", "Vet", "--next-action", "Email the vet"]) == 0
    assert _FakeTodoApi.instances[-1].created_with[0]["next_action"] == "Email the vet"


@pytest.mark.parametrize("text", ["", "   "])
def test_create_refuses_a_blank_next_action(tmp_home, patch_api, text) -> None:
    _login(tmp_home)
    patch_api(todo_created=_todo())
    assert cli.main(["todos", "create", "Vet", "--next-action", text]) == 2
    assert not _FakeTodoApi.instances or not _FakeTodoApi.instances[-1].created_with


def test_create_assignee_resolves_the_name_through_the_roster(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(todo_created=_todo(assignee=api_client.TodoPerson(user_id="u2", label="Bob Stone")))
    rc = cli.main(["todos", "create", "Book it", "--room", _ROOM, "--assignee", "bob"])
    assert rc == 0
    assert _FakeTodoApi.instances[-1].created_with == [
        {"assignee_person_id": _BOB, "next_action": None}
    ]
    assert "Asked Bob Stone" in capsys.readouterr().err


def test_create_assignee_without_a_room_or_unknown_person_is_a_usage_error(
    tmp_home, patch_api, capsys
) -> None:
    _login(tmp_home)
    patch_api(todo_created=_todo())
    assert cli.main(["todos", "create", "x", "--assignee", "bob"]) == 2
    assert "--assignee needs --room" in capsys.readouterr().err
    assert cli.main(["todos", "create", "x", "--room", _ROOM, "--assignee", "zed"]) == 2
    assert _FakeTodoApi.instances[-1].calls == []


def test_dismiss_sends_the_reason_only_when_given(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(todo_dismissed=_todo(status="dismissed"))
    assert cli.main(["todos", "dismiss", _TID]) == 0
    assert cli.main(["todos", "dismiss", _TID, "--reason", "no time"]) == 0
    assert [i.dismiss_reasons for i in _FakeTodoApi.instances] == [[None], ["no time"]]


def test_dismiss_surfaces_a_server_refusal_as_it_came(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(
        todo_dismissed=api_client.ApiError(status=422, body="", detail="this ask is already done")
    )
    assert cli.main(["todos", "dismiss", _TID, "--room", _ROOM]) == 1
    assert "this ask is already done" in capsys.readouterr().err


def test_edit_sends_only_what_was_given(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(todo_edited=_todo(title="New"))
    rc = cli.main(
        ["todos", "edit", _TID, "--title", " New ", "--clear", "details", "--clear", "priority"]
    )
    assert rc == 0
    assert _FakeTodoApi.instances[-1].edits == [
        (_TID, {"title": "New", "details": None, "priority": None}, None)
    ]
    assert "Updated todo" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("flag", "sent"),
    [
        (["--next-action", " Call Bob "], {"next_action": "Call Bob"}),
        (["--clear-next-action"], {"next_action": None}),
        (["--accept-suggestion"], {"next_action_suggestion": "accept"}),
        (["--dismiss-suggestion"], {"next_action_suggestion": "dismiss"}),
    ],
)
def test_edit_next_action_flags_send_the_contract_body(tmp_home, patch_api, flag, sent) -> None:
    _login(tmp_home)
    patch_api(todo_edited=_todo())
    assert cli.main(["todos", "edit", _TID, *flag]) == 0
    assert _FakeTodoApi.instances[-1].edits == [(_TID, sent, None)]


def test_edit_room_due_and_assignee(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(todo_edited=_todo())
    rc = cli.main(
        ["todos", "edit", _TID, "--room", _ROOM, "--due", "2026-11-02", "--assignee", "Bob"]
    )
    assert rc == 0
    assert _FakeTodoApi.instances[-1].edits == [
        (_TID, {"due": "2026-11-02", "assignee_person_id": _BOB}, _ROOM)
    ]


@pytest.mark.parametrize(
    "argv",
    [
        ["edit", _TID],
        ["edit", "nope", "--title", "x"],
        ["edit", _TID, "--title", "  "],
        ["edit", _TID, "--details", "x", "--clear", "details"],
        ["edit", _TID, "--assignee", "bob"],
        ["edit", _TID, "--next-action", "  "],
        ["edit", _TID, "--room", _ROOM, "--due", "tomorrow"],
    ],
)
def test_edit_bad_input_never_reaches_the_api(tmp_home, patch_api, argv) -> None:
    _login(tmp_home)
    patch_api(todo_edited=_todo())
    assert cli.main(["todos", *argv]) == 2
    assert _FakeTodoApi.instances == []


def test_edit_conflict_prints_the_server_text_and_a_next_step(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(todo_edited=api_client.ApiError(status=409, body="", detail="the ask has changed"))
    assert cli.main(["todos", "edit", _TID, "--title", "x"]) == 1
    err = capsys.readouterr().err
    assert "Server returned 409: the ask has changed" in err
    assert "pepper todos list" in err


@pytest.mark.parametrize(
    "argv",
    [
        ["edit", _TID, "--title", "x", "--json"],
        ["dismiss", _TID, "--json"],
    ],
)
def test_conflict_in_json_mode_is_one_error_document(tmp_home, patch_api, capsys, argv) -> None:
    _login(tmp_home)
    conflict = api_client.ApiError(status=409, body="", detail="the ask has changed")
    patch_api(todo_edited=conflict, todo_dismissed=conflict)
    assert cli.main(["todos", *argv]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    doc = json.loads(captured.err)
    assert doc["error"]["status"] == 409
    assert doc["error"]["message"] == "Server returned 409: the ask has changed"


# ---------------------------------------------------------------------------
# numbered refs

_T1 = "11111111-aaaa-0000-0000-000000000001"
_T2 = "22222222-bbbb-0000-0000-000000000002"
_T3 = "33333333-cccc-0000-0000-000000000003"


def _listed(patch_api, *, room: str | None = None, **canned) -> None:
    """Sign in, install the fake, and list three todos (yours, or ``room``'s)."""
    patch_api(
        todo_list=[
            _todo(id=_T1, title="First"),
            _todo(id=_T2, title="Second", next_action=None),
            _todo(id=_T3, title="Third"),
        ],
        todo_done=_todo(id=_T2, title="Second", status="done"),
        todo_dismissed=_todo(id=_T2, title="Second", status="dismissed"),
        todo_edited=_todo(id=_T2, title="Renamed"),
        **canned,
    )
    argv = ["todos", "list", *(["--room", room] if room else [])]
    assert cli.main(argv) == 0


def _calls() -> list[tuple]:
    return [c for i in _FakeTodoApi.instances for c in i.calls if c[0] != "list"]


def test_list_numbers_rows_in_server_order_and_continuation_lines_align(
    tmp_home, patch_api, capsys
) -> None:
    _login(tmp_home)
    patch_api(
        todo_list=[
            _todo(id=_T1, title="First"),
            _todo(
                id=_T2,
                title="Second",
                next_action=api_client.TodoNextAction(
                    text="Call them", by="user", updated_at="2026-10-20T00:00Z"
                ),
            ),
        ]
    )
    assert cli.main(["todos", "list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith(" 1. ") and "First" in lines[0] and _T1 in lines[0]
    assert lines[1].startswith(" 2. ") and "Second" in lines[1]
    assert lines[2] == "      Next: Call them"


def test_list_json_is_not_numbered_but_still_saves_the_listing(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(todo_list=[_todo(id=_T1, title="First")], todo_done=_todo(id=_T1))
    assert cli.main(["todos", "list", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert "n" not in out["items"][0] and "note" not in out
    assert cli.main(["todos", "done", "1"]) == 0
    assert _calls() == [("done", _T1, None)]


@pytest.mark.parametrize(
    ("verb", "expect"),
    [("done", "done"), ("reopen", "reopen"), ("dismiss", "dismiss")],
)
def test_verbs_take_a_number_from_the_last_listing(tmp_home, patch_api, verb, expect) -> None:
    _login(tmp_home)
    _listed(patch_api)
    assert cli.main(["todos", verb, "2"]) == 0
    assert _calls() == [(expect, _T2, None)]


def test_edit_takes_a_number(tmp_home, patch_api) -> None:
    _login(tmp_home)
    _listed(patch_api)
    assert cli.main(["todos", "edit", "3", "--title", "Renamed"]) == 0
    assert _FakeTodoApi.instances[-1].edits == [(_T3, {"title": "Renamed"}, None)]


def test_a_full_uuid_still_works_with_no_listing_and_with_one(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(todo_done=_todo(id=_T1))
    assert cli.main(["todos", "done", _T1]) == 0
    _listed(patch_api)
    assert cli.main(["todos", "done", "00000000-0000-0000-0000-000000000099"]) == 0
    assert _calls() == [
        ("done", _T1, None),
        ("done", "00000000-0000-0000-0000-000000000099", None),
    ]


def test_a_unique_prefix_of_a_listed_id_resolves_and_an_ambiguous_one_does_not(
    tmp_home, patch_api, capsys
) -> None:
    _login(tmp_home)
    _listed(patch_api)
    assert cli.main(["todos", "done", "22222222-b"]) == 0
    assert _calls() == [("done", _T2, None)]
    capsys.readouterr()
    assert cli.main(["todos", "done", "9999999", "--json"]) == 2  # held by no id: not a ref
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "usage"


def test_a_prefix_two_listed_ids_share_is_ambiguous(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(
        todo_list=[
            _todo(id="abcdef01-0000-0000-0000-000000000001"),
            _todo(id="abcdef02-0000-0000-0000-000000000002"),
        ]
    )
    assert cli.main(["todos", "list"]) == 0
    capsys.readouterr()
    assert cli.main(["todos", "done", "abcdef", "--json"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "ambiguous"


def test_a_number_with_no_saved_listing_names_todos_list(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(todo_done=_todo())
    assert cli.main(["todos", "done", "3"]) == 2
    assert "pepper todos list" in capsys.readouterr().err
    assert _FakeTodoApi.instances == []


def test_a_number_out_of_range_names_todos_list(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    _listed(patch_api)
    capsys.readouterr()
    for ref in ("0", "4"):
        assert cli.main(["todos", "done", ref]) == 2
    err = capsys.readouterr().err
    assert "pepper todos list" in err and "listed 3" in err
    assert _calls() == []


@pytest.mark.parametrize(
    "argv",
    [["done"], ["reopen"], ["dismiss"], ["edit", "--title", "X"]],
)
def test_a_non_ascii_digit_is_a_usage_error_not_a_crash(tmp_home, patch_api, capsys, argv) -> None:
    _login(tmp_home)
    _listed(patch_api)
    capsys.readouterr()
    cmd, *rest = argv
    assert cli.main(["todos", cmd, "\u00b2", *rest, "--json"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "usage"
    assert _calls() == []


def test_a_number_from_your_list_refuses_with_room(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    _listed(patch_api)
    capsys.readouterr()
    assert cli.main(["todos", "done", "1", "--room", _ROOM]) == 2
    err = capsys.readouterr().err
    assert "your own todos" in err and "drop --room" in err
    assert _calls() == []


def test_a_number_from_a_rooms_list_refuses_without_room(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    _listed(patch_api, room="trip")
    capsys.readouterr()
    for verb in ("done", "dismiss", "edit"):
        extra = ["--title", "x"] if verb == "edit" else []
        assert cli.main(["todos", verb, "1", *extra]) == 2
    err = capsys.readouterr().err
    assert err.count("a room's todos, not yours") == 3
    assert _calls() == []
    assert _FakeTodoApi.instances[-1].edits == []


def test_a_number_from_a_rooms_list_works_with_that_room(tmp_home, patch_api) -> None:
    _login(tmp_home)
    _listed(patch_api, room="trip")
    assert cli.main(["todos", "done", "1", "--room", "trip"]) == 0
    assert cli.main(["todos", "done", "3", "--room", _ROOM.upper()]) == 0
    assert _calls() == [("done", _T1, _ROOM), ("done", _T3, _ROOM.upper())]


def test_a_uuid_is_never_room_checked(tmp_home, patch_api) -> None:
    _login(tmp_home)
    _listed(patch_api)
    assert cli.main(["todos", "done", _T1, "--room", _ROOM]) == 0
    assert _calls() == [("done", _T1, _ROOM)]


def test_mine_filtered_list_numbers_resolve_in_the_same_room(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(todo_list=[_todo(id=_T1)], todo_done=_todo(id=_T1))
    assert cli.main(["todos", "list", "--room", "trip", "--mine"]) == 0
    assert cli.main(["todos", "done", "1", "--room", "trip"]) == 0
    assert _calls() == [("done", _T1, _ROOM)]


def test_an_unsaved_listing_is_reported_and_a_stale_one_is_not_shown(
    tmp_home, patch_api, monkeypatch, capsys
) -> None:
    from pepper_cli import numbered_view, todos_view

    _login(tmp_home)
    patch_api(todo_list=[_todo(id=_T1)])

    def refuse(*_a, **_k):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(numbered_view.tempfile, "mkstemp", refuse)
    assert cli.main(["todos", "list"]) == 0
    assert "numbers from this list won't work" in capsys.readouterr().err
    todos_view.VIEW.state_path().write_text("{}")
    monkeypatch.setattr(
        numbered_view.Path, "unlink", lambda *_a, **_k: (_ for _ in ()).throw(OSError(1, "nope"))
    )
    assert cli.main(["todos", "list"]) == 1
    out = capsys.readouterr()
    assert "not shown" in out.err and "First" not in out.out


def test_logout_forgets_the_todos_listing(tmp_home, patch_api) -> None:
    from pepper_cli import todos_view

    _login(tmp_home)
    _listed(patch_api)
    assert todos_view.VIEW.state_path().exists()
    assert cli.main(["logout"]) == 0
    assert not todos_view.VIEW.state_path().exists()
