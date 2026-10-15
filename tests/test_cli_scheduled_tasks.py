"""End-to-end tests for the ``pepper scheduled-tasks`` subcommands.

Same approach as ``test_cli_todos.py``: invoke ``cli.main([...])`` with a
monkeypatched ``api_client.ApiClient`` so commands run without the
network, and exercise credentials through the real (tmp-home) module.

What's worth testing here is mostly *what reaches the wire*: the three
destination states are three different payloads and collapsing any two of
them changes what the user gets, and a PATCH must carry only the fields
the user actually named or the server can't tell "leave it" from "set it".
"""

from __future__ import annotations

import dataclasses
import json
import re
import time
from pathlib import Path

import httpx
import pytest

from pepper_cli import api_client, cli, credentials
from pepper_cli.secret import SecretStr

_TASK_ID = "00000000-0000-0000-0000-0000000000a1"


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return tmp_path


def _task(**over) -> api_client.ScheduledTaskSummary:
    base = {
        "id": _TASK_ID,
        "name": "morning-weather",
        "description": None,
        "cron": "0 8 * * 1-5",
        "timezone": None,
        "destinations": None,
        "active": True,
        "one_shot": False,
        "last_run_at": None,
        "next_run_at": "2026-06-05T15:00:00+00:00",
    }
    base.update(over)
    return api_client.ScheduledTaskSummary(**base)


def _run(**over) -> api_client.ScheduledTaskRunEntry:
    base = {
        "id": "00000000-0000-0000-0000-0000000000b1",
        "fired_at": "2026-06-04T15:00:00+00:00",
        "finished_at": "2026-06-04T15:00:09+00:00",
        "status": "succeeded",
        "error": None,
        "delivered_channels": ["main_chat"],
        "trajectory_id": None,
        "reply_preview": "Sunny, 21C.",
    }
    base.update(over)
    return api_client.ScheduledTaskRunEntry(**base)


class _FakeTaskApi:
    """Records calls and returns canned scheduled-task responses (or raises)."""

    instances: list[_FakeTaskApi] = []

    def __init__(self, *, server_url, token, **canned) -> None:
        self.server_url = server_url
        self.token = token
        self._canned = canned
        self.calls: list[tuple] = []
        _FakeTaskApi.instances.append(self)

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

    def list_scheduled_tasks(self, *, include_inactive=False):
        self.calls.append(("list", include_inactive))
        return self._result("task_list")

    def get_scheduled_task(self, task_id, *, group_id=None):
        self.calls.append(("show", task_id))
        return self._result("task_detail")

    def list_scheduled_task_runs(
        self, task_id, *, limit=20, before=None, before_id=None, group_id=None
    ):
        self.calls.append(("runs", task_id, limit))
        return self._result("task_runs")

    def get_scheduled_task_thread(
        self, task_id, *, limit=20, before=None, before_id=None, group_id=None
    ):
        self.calls.append(("thread", task_id, limit))
        return self._result("task_thread")

    def create_scheduled_task(self, **kwargs):
        self.calls.append(("create", kwargs))
        return self._result("task_created")

    def update_scheduled_task(self, task_id, *, edits):
        self.calls.append(("update", task_id, edits))
        return self._result("task_updated")

    def cancel_scheduled_task(self, task_id):
        self.calls.append(("cancel", task_id))
        return self._result("task_cancelled")

    def resume_scheduled_task(self, task_id):
        self.calls.append(("resume", task_id))
        return self._result("task_resumed")

    def set_scheduled_task_destinations(self, task_id, *, destinations, group_id=None, topic=None):
        self.calls.append(("destinations", task_id, destinations))
        return self._result("task_destinations")

    def renew_scheduled_task(self, task_id, *, group_id=None):
        self.calls.append(("renew", task_id))
        return self._result("task_renewed")

    def delete_scheduled_task(self, task_id):
        self.calls.append(("delete", task_id))
        self._result("task_deleted")


@pytest.fixture
def patch_api(monkeypatch):
    _FakeTaskApi.instances.clear()

    def install(**canned):
        def factory(**kwargs):
            return _FakeTaskApi(**canned, **kwargs)

        monkeypatch.setattr(api_client, "ApiClient", factory)

    return install


def _login(tmp_home: Path) -> None:
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))


def _last_call(kind: str) -> tuple:
    calls = [c for c in _FakeTaskApi.instances[-1].calls if c[0] == kind]
    assert calls, f"no {kind!r} call was made"
    return calls[-1]


# ---------------------------------------------------------------------------
# list / show / runs / thread


def test_list_renders_names_and_destinations(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(
        task_list=[
            _task(name="morning-weather", destinations=["resend_email"]),
            _task(id="b", name="quiet-task", destinations=[]),
        ]
    )
    rc = cli.main(["scheduled-tasks", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "morning-weather" in out
    assert "email" in out
    # `[]` (silent) and `None` (inherit) must not render the same — they
    # are opposite answers about whether the user hears from the task.
    assert "nobody (silent)" in out
    assert _last_call("list") == ("list", False)


def test_list_all_includes_inactive(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(task_list=[])
    rc = cli.main(["scheduled-tasks", "list", "--all"])
    assert rc == 0
    assert _last_call("list") == ("list", True)


def test_list_empty(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(task_list=[])
    assert cli.main(["scheduled-tasks", "list"]) == 0
    assert "(no scheduled tasks)" in capsys.readouterr().out


def test_list_without_credentials_errors(tmp_home, patch_api) -> None:
    patch_api(task_list=[])
    assert cli.main(["scheduled-tasks", "list"]) == 1


def _detail(*, latest_run=None, **over) -> api_client.ScheduledTaskDetail:
    return api_client.ScheduledTaskDetail(
        **dataclasses.asdict(_task(**over)), latest_run=latest_run
    )


def test_show_renders_the_latest_run(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(task_detail=_detail(description="Weekday weather", latest_run=_run()))
    rc = cli.main(["scheduled-tasks", "show", _TASK_ID])
    assert rc == 0
    out = capsys.readouterr().out
    assert "morning-weather" in out
    assert "Weekday weather" in out
    assert "succeeded" in out


@pytest.fixture
def machine_zone(monkeypatch):
    """Pin the process's local zone, and put it back afterwards.

    ``monkeypatch.setenv`` restores the variable but not libc's cached
    zone, so the teardown ``tzset`` is what keeps this from leaking into
    every test that renders a timestamp after it.
    """

    def install(name: str) -> None:
        monkeypatch.setenv("TZ", name)
        time.tzset()

    yield install
    monkeypatch.undo()
    time.tzset()


def test_show_labels_the_zone_its_stamps_are_in(tmp_home, patch_api, capsys, machine_zone) -> None:
    """A stamp is rendered in the machine's zone and says so.

    ``show`` prints a cron task's pinned timezone beside the schedule, and
    the machine's zone is usually a different one — the label is what stops
    the next-fire line from being read as a time in the pin.
    """
    machine_zone("America/New_York")
    _login(tmp_home)
    patch_api(
        task_detail=_detail(
            timezone="Europe/London",
            next_run_at="2026-06-05T15:00:00+00:00",
            latest_run=_run(),
        )
    )
    assert cli.main(["scheduled-tasks", "show", _TASK_ID]) == 0
    out = capsys.readouterr().out
    # The pin is printed with the schedule; the stamp is 15:00Z rendered in
    # the machine's zone (EDT), and carries that name so the two can't be
    # confused for each other.
    assert "Europe/London" in out
    assert "2026-06-05 11:00 EDT" in out


def test_runs_passes_the_limit(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(task_runs=[_run(status="error", error="boom", reply_preview=None)])
    rc = cli.main(["scheduled-tasks", "runs", _TASK_ID, "--limit", "5"])
    assert rc == 0
    assert _last_call("runs") == ("runs", _TASK_ID, 5)
    assert "boom" in capsys.readouterr().out


def test_thread_renders_messages(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(
        task_thread=[
            api_client.ScheduledTaskMessage(
                id="m1",
                role="assistant",
                text="Sunny, 21C.",
                channel="client_api",
                trajectory_id=None,
                created_at="2026-06-04T15:00:09+00:00",
            )
        ]
    )
    rc = cli.main(["scheduled-tasks", "thread", _TASK_ID])
    assert rc == 0
    assert "Sunny, 21C." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# name resolution


def test_a_name_is_resolved_to_an_id(tmp_home, patch_api) -> None:
    """The API is id-keyed but a name is what the user calls a task, so
    the CLI looks it up rather than making them paste a UUID."""
    _login(tmp_home)
    patch_api(task_list=[_task(name="morning-weather")], task_cancelled=_task(active=False))
    rc = cli.main(["scheduled-tasks", "pause", "morning-weather"])
    assert rc == 0
    # Paused tasks are in scope for the lookup: `resume` targets only those.
    assert _last_call("list") == ("list", True)
    assert _last_call("cancel") == ("cancel", _TASK_ID)


def test_an_unknown_name_exits_two_without_writing(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(task_list=[_task(name="morning-weather")])
    rc = cli.main(["scheduled-tasks", "pause", "no-such-task"])
    assert rc == 2
    assert "no scheduled task named" in capsys.readouterr().err
    assert [c[0] for c in _FakeTaskApi.instances[-1].calls] == ["list"]


def test_a_uuid_skips_the_lookup(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(task_cancelled=_task(active=False))
    rc = cli.main(["scheduled-tasks", "pause", _TASK_ID])
    assert rc == 0
    assert [c[0] for c in _FakeTaskApi.instances[-1].calls] == ["cancel"]


# ---------------------------------------------------------------------------
# add


def test_create_sends_cron_and_destinations(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(task_created=_task(destinations=["main_chat"]))
    rc = cli.main(
        [
            "scheduled-tasks",
            "create",
            "morning-weather",
            "--prompt",
            "Give me the forecast.",
            "--cron",
            "0 8 * * 1-5",
            "--to",
            "main_chat",
        ]
    )
    assert rc == 0
    kwargs = _last_call("create")[1]
    assert kwargs["name"] == "morning-weather"
    assert kwargs["cron"] == "0 8 * * 1-5"
    assert kwargs["run_at"] is None
    assert kwargs["destinations"] == ["main_chat"]
    assert "Scheduled" in capsys.readouterr().err


def test_create_parses_at_into_an_aware_timestamp(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(task_created=_task(one_shot=True, cron=None))
    rc = cli.main(
        [
            "scheduled-tasks",
            "create",
            "call-mom",
            "--prompt",
            "Ring her.",
            "--at",
            "2099-01-02 15:30",
        ]
    )
    assert rc == 0
    run_at = _last_call("create")[1]["run_at"]
    assert run_at.startswith("2099-01-02T15:30")
    # An offset, not a bare wall-clock time — the server can't guess the
    # client's zone and refuses a naive value.
    assert run_at[-6] in "+-"


def test_create_omits_destinations_to_mean_inherit(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(task_created=_task())
    rc = cli.main(["scheduled-tasks", "create", "x", "--prompt", "p", "--cron", "0 8 * * *"])
    assert rc == 0
    assert _last_call("create")[1]["destinations"] is None


def test_create_silent_sends_an_empty_list(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(task_created=_task(destinations=[]))
    rc = cli.main(
        ["scheduled-tasks", "create", "x", "--prompt", "p", "--cron", "0 8 * * *", "--silent"]
    )
    assert rc == 0
    assert _last_call("create")[1]["destinations"] == []


def test_create_requires_exactly_one_of_cron_or_at(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(task_created=_task())
    assert cli.main(["scheduled-tasks", "create", "x", "--prompt", "p"]) == 2
    assert (
        cli.main(
            [
                "scheduled-tasks",
                "create",
                "x",
                "--prompt",
                "p",
                "--cron",
                "0 8 * * *",
                "--at",
                "2099-01-01",
            ]
        )
        == 2
    )
    assert "exactly one" in capsys.readouterr().err
    assert _FakeTaskApi.instances == []  # nothing reached the network


def test_create_rejects_silent_alongside_to(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(task_created=_task())
    rc = cli.main(
        [
            "scheduled-tasks",
            "create",
            "x",
            "--prompt",
            "p",
            "--cron",
            "0 8 * * *",
            "--silent",
            "--to",
            "main_chat",
        ]
    )
    assert rc == 2
    assert _FakeTaskApi.instances == []


def test_create_rejects_an_unparseable_at(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(task_created=_task())
    rc = cli.main(["scheduled-tasks", "create", "x", "--prompt", "p", "--at", "next tuesday"])
    assert rc == 2
    assert "--at" in capsys.readouterr().err
    assert _FakeTaskApi.instances == []


def test_create_surfaces_a_name_conflict(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(
        task_created=api_client.ApiError(
            status=409, body="", detail="a scheduled task named 'x' already exists"
        )
    )
    rc = cli.main(["scheduled-tasks", "create", "x", "--prompt", "p", "--cron", "0 8 * * *"])
    assert rc == 1
    assert "already exists" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# edit


def test_edit_sends_only_the_fields_named(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(task_updated=_task())
    rc = cli.main(["scheduled-tasks", "edit", _TASK_ID, "--prompt", "new prompt"])
    assert rc == 0
    # Only ``prompt`` — anything else in the dict would tell the server to
    # write a field the user never mentioned.
    assert _last_call("update")[2] == {"prompt": "new prompt"}


def test_edit_clear_description_sends_an_explicit_null(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(task_updated=_task())
    rc = cli.main(["scheduled-tasks", "edit", _TASK_ID, "--clear-description"])
    assert rc == 0
    assert _last_call("update")[2] == {"description": None}


def test_edit_rejects_both_description_flags(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(task_updated=_task())
    rc = cli.main(
        ["scheduled-tasks", "edit", _TASK_ID, "--description", "d", "--clear-description"]
    )
    assert rc == 2
    assert _FakeTaskApi.instances == []


def test_edit_with_nothing_to_change_exits_two(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(task_updated=_task())
    rc = cli.main(["scheduled-tasks", "edit", _TASK_ID])
    assert rc == 2
    assert "nothing to change" in capsys.readouterr().err
    assert _FakeTaskApi.instances == []


# ---------------------------------------------------------------------------
# pause / resume


def test_pause_and_resume(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(task_cancelled=_task(active=False), task_resumed=_task(active=True))
    assert cli.main(["scheduled-tasks", "pause", _TASK_ID]) == 0
    assert "Paused" in capsys.readouterr().err
    assert cli.main(["scheduled-tasks", "resume", _TASK_ID]) == 0
    assert "Resumed" in capsys.readouterr().err


def test_resume_of_a_spent_one_shot_prints_the_servers_reason(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(
        task_resumed=api_client.ApiError(
            status=422,
            body="",
            detail="That one-off task's time has already passed.",
        )
    )
    rc = cli.main(["scheduled-tasks", "resume", _TASK_ID])
    assert rc == 1
    assert "already passed" in capsys.readouterr().err


def test_pause_handles_an_unreachable_server(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(task_cancelled=httpx.ConnectError("boom"))
    rc = cli.main(["scheduled-tasks", "pause", _TASK_ID])
    assert rc == 1
    assert "Couldn't reach" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# destinations


def test_edit_delivery_to_sends_the_list(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(task_destinations=_task(destinations=["main_chat", "resend_email"]))
    rc = cli.main(
        ["scheduled-tasks", "edit", _TASK_ID, "--to", "main_chat", "--to", "resend_email"]
    )
    assert rc == 0
    assert _last_call("destinations")[2] == ["main_chat", "resend_email"]


def test_edit_delivery_default_sends_null(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(task_destinations=_task(destinations=None))
    rc = cli.main(["scheduled-tasks", "edit", _TASK_ID, "--default"])
    assert rc == 0
    assert _last_call("destinations")[2] is None


def test_edit_delivery_silent_sends_an_empty_list(tmp_home, patch_api) -> None:
    """The difference from ``--default`` is the point: a later change to
    the user's notification preference must not un-silence the task."""
    _login(tmp_home)
    patch_api(task_destinations=_task(destinations=[]))
    rc = cli.main(["scheduled-tasks", "edit", _TASK_ID, "--silent"])
    assert rc == 0
    assert _last_call("destinations")[2] == []


def test_edit_delivery_requires_exactly_one_choice(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(task_destinations=_task())
    assert cli.main(["scheduled-tasks", "edit", _TASK_ID]) == 2
    assert "nothing to change" in capsys.readouterr().err
    assert cli.main(["scheduled-tasks", "edit", _TASK_ID, "--default", "--silent"]) == 2
    assert "pass only one of" in capsys.readouterr().err
    assert _FakeTaskApi.instances == []


def test_edit_delivery_rejects_an_unknown_name_locally(tmp_home, patch_api) -> None:
    """A typo becomes an argparse usage error (``choices``) rather than a
    round-trip 422."""
    _login(tmp_home)
    patch_api(task_destinations=_task())
    with pytest.raises(SystemExit):
        cli.main(["scheduled-tasks", "edit", _TASK_ID, "--to", "carrier-pigeon"])
    assert _FakeTaskApi.instances == []


# ---------------------------------------------------------------------------
# dispatch


def test_scheduled_tasks_is_not_swallowed_by_implicit_chat(tmp_home, patch_api) -> None:
    """``pepper <words>`` is shorthand for chat, so a new subcommand name
    has to be registered or it silently becomes a chat message."""
    _login(tmp_home)
    patch_api(task_list=[])
    assert cli.main(["scheduled-tasks", "list"]) == 0
    assert _FakeTaskApi.instances[-1].calls == [("list", False)]


# ---------------------------------------------------------------------------
# --room: a room's scheduled tasks
#
# Same commands, the room's routes. What matters on the wire is the same as
# above — which path, which three-state delivery payload — plus the two
# things only a room has: the owner gate's one sentence, and a dialect of
# destinations that must not be mixed with a person's.

_ROOM = "00000000-0000-0000-0000-0000000000c1"


def _room_task(**over) -> api_client.RoomScheduledTaskSummary:
    extra = {
        "delivery": over.pop("delivery", "The room's topic (the room's default)"),
        "last_run_status": over.pop("last_run_status", None),
    }
    return api_client.RoomScheduledTaskSummary(**dataclasses.asdict(_task(**over)), **extra)


class _FakeRoomTaskApi(_FakeTaskApi):
    """The room-aware half of the client: every call records the room it hit,
    so a test can tell a room write from a write on the caller's own task."""

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

    def list_scheduled_tasks(self, *, include_inactive=False, group_id=None):
        self.calls.append(("list", include_inactive, group_id))
        return self._result("task_list")

    def cancel_scheduled_task(self, task_id, *, group_id=None):
        self.calls.append(("cancel", task_id, group_id))
        return self._result("task_cancelled")

    def resume_scheduled_task(self, task_id, *, group_id=None):
        self.calls.append(("resume", task_id, group_id))
        return self._result("task_resumed")

    def set_scheduled_task_destinations(self, task_id, *, destinations, group_id=None, topic=None):
        self.calls.append(("destinations", task_id, destinations, group_id, topic))
        return self._result("task_destinations")

    def update_room_scheduled_task(
        self, group_id, task_id, *, edits, destinations=None, topic=None
    ):
        self.calls.append(("room_update", group_id, task_id, edits, destinations, topic))
        return self._result("task_room_updated")

    def get_scheduled_task(self, task_id, *, group_id=None):
        self.calls.append(("show", task_id, group_id))
        return self._result("task_detail")

    def list_scheduled_task_runs(
        self, task_id, *, limit=20, before=None, before_id=None, group_id=None
    ):
        self.calls.append(("runs", task_id, limit, before, before_id, group_id))
        return self._result("task_runs")

    def get_scheduled_task_thread(
        self, task_id, *, limit=20, before=None, before_id=None, group_id=None
    ):
        self.calls.append(("thread", task_id, limit, before, before_id, group_id))
        return self._result("task_thread")

    def renew_scheduled_task(self, task_id, *, group_id=None):
        self.calls.append(("renew", task_id, group_id))
        return self._result("task_renewed")


@pytest.fixture
def patch_room_api(monkeypatch):
    _FakeTaskApi.instances.clear()

    def install(**canned):
        def factory(**kwargs):
            return _FakeRoomTaskApi(**canned, **kwargs)

        monkeypatch.setattr(api_client, "ApiClient", factory)

    return install


def _calls() -> list[tuple]:
    return [c for i in _FakeTaskApi.instances for c in i.calls]


def _forbidden() -> api_client.ApiError:
    detail = "Only an owner of this room can change its scheduled tasks."
    return api_client.ApiError(status=403, body="", detail=detail)


def test_room_list_reads_the_room_and_prints_the_servers_delivery(
    tmp_home, patch_room_api, capsys
) -> None:
    """A room row's ``destinations`` of ``None`` is the *room's* default, so
    the personal renderer's "your default" would be wrong — the server's own
    sentence is what gets printed."""
    _login(tmp_home)
    patch_room_api(task_list=[_room_task(name="weekly-menu", last_run_status="succeeded")])
    assert cli.main(["scheduled-tasks", "list", "--room", "trip"]) == 0
    # Rich wraps at the captured terminal's width; compare unwrapped.
    out = " ".join(capsys.readouterr().out.split())
    assert "weekly-menu" in out
    assert "The room's topic (the room's default)" in out
    assert "your default" not in out
    assert "last run succeeded" in out
    assert _calls() == [("list", False, _ROOM)]


def test_room_list_all_includes_paused(tmp_home, patch_room_api) -> None:
    _login(tmp_home)
    patch_room_api(task_list=[])
    assert cli.main(["scheduled-tasks", "list", "--all", "--room", _ROOM]) == 0
    assert _calls() == [("list", True, _ROOM)]


def test_room_create_sends_the_room_form(tmp_home, patch_room_api, capsys) -> None:
    _login(tmp_home)
    patch_room_api(task_created=_room_task(name="weekly-menu"))
    rc = cli.main(
        [
            "scheduled-tasks",
            "create",
            "weekly-menu",
            "--prompt",
            "Plan the week's dinners.",
            "--cron",
            "0 9 * * 0",
            "--timezone",
            "America/Los_Angeles",
            "--room",
            "Trip",
        ]
    )
    assert rc == 0
    assert _last_call("create")[1] == {
        "name": "weekly-menu",
        "prompt": "Plan the week's dinners.",
        "cron": "0 9 * * 0",
        "timezone": "America/Los_Angeles",
        "skill": None,
        "run_at": None,
        # Omitted --to pins nothing: the server picks the room's delivery.
        "destinations": None,
        "group_id": _ROOM,
        "topic": None,
    }
    assert "on the room" in capsys.readouterr().err


def test_room_create_delivery_states(tmp_home, patch_room_api) -> None:
    """Silent, a pinned set and a named topic are three different requests."""
    _login(tmp_home)
    patch_room_api(task_created=_room_task())
    base = [
        "scheduled-tasks",
        "create",
        "t",
        "--prompt",
        "p",
        "--cron",
        "0 9 * * 0",
        "--room",
        "Trip",
    ]
    assert cli.main([*base, "--silent"]) == 0
    assert _last_call("create")[1]["destinations"] == []
    assert cli.main([*base, "--to", "resend_email"]) == 0
    assert _last_call("create")[1]["destinations"] == ["resend_email"]
    assert cli.main([*base, "--to", "groups_service", "--deliver-topic", "Dinner"]) == 0
    last = _last_call("create")[1]
    assert (last["destinations"], last["topic"]) == (["groups_service"], "Dinner")


@pytest.mark.parametrize(
    ("extra", "said"),
    [
        (["--cron", "0 9 * * 0", "--to", "twilio_sms"], "isn't available with --room"),
        (["--cron", "0 9 * * 0", "--to", "main_chat"], "isn't available with --room"),
        (["--cron", "0 9 * * 0", "--deliver-topic", "Dinner"], "--to room"),
        (
            ["--cron", "0 9 * * 0", "--to", "resend_email", "--deliver-topic", "Dinner"],
            "--to room",
        ),
        (["--cron", "0 9 * * 0", "--description", "d"], "--description isn't available"),
        (["--at", "2030-01-01 09:00", "--cron", "0 9 * * 0"], "exactly one of --cron"),
        (["--at", "not-a-time"], "--at"),
        ([], "exactly one of --cron"),
        (["--cron", "0 9 * * 0", "--to", "resend_email", "--silent"], "--silent"),
    ],
)
def test_room_create_refuses_what_the_room_form_lacks(
    tmp_home, patch_room_api, capsys, extra, said
) -> None:
    """Each is a usage error before any request: nothing reaches the server."""
    _login(tmp_home)
    patch_room_api(task_created=_room_task())
    base = ["scheduled-tasks", "create", "t", "--prompt", "p", "--room", "Trip"]
    assert cli.main([*base, *extra]) == 2
    assert said in capsys.readouterr().err
    assert _calls() == []


def test_room_only_flags_are_refused_on_a_personal_task(tmp_home, patch_api, capsys) -> None:
    """The other direction of the same mix-up: the room's dialect on a task
    of your own is a usage error, not a 422 from the personal route."""
    _login(tmp_home)
    patch_api(task_created=_task(), task_destinations=_task())
    add = ["scheduled-tasks", "create", "t", "--prompt", "p", "--cron", "0 9 * * 0"]
    assert cli.main([*add, "--to", "groups_service"]) == 2
    assert "only available with --room" in capsys.readouterr().err
    assert cli.main([*add, "--deliver-topic", "Dinner"]) == 2
    assert "only available with --room" in capsys.readouterr().err
    dest = ["scheduled-tasks", "edit", _TASK_ID]
    assert cli.main([*dest, "--to", "groups_service"]) == 2
    assert cli.main([*dest, "--default", "--deliver-topic", "Dinner"]) == 2
    assert _FakeTaskApi.instances == []


def test_room_pause_and_resume_by_name_look_in_the_room(tmp_home, patch_room_api, capsys) -> None:
    """The name is resolved among the room's tasks (paused included), and the
    flip goes to the room — never to a same-named task of the caller's."""
    _login(tmp_home)
    row = _room_task(name="weekly-menu")
    patch_room_api(task_list=[row], task_cancelled=row, task_resumed=row)
    assert cli.main(["scheduled-tasks", "pause", "weekly-menu", "--room", "Trip"]) == 0
    assert cli.main(["scheduled-tasks", "resume", "weekly-menu", "--room", "Trip"]) == 0
    assert _calls() == [
        ("list", True, _ROOM),
        ("cancel", _TASK_ID, _ROOM),
        ("list", True, _ROOM),
        ("resume", _TASK_ID, _ROOM),
    ]
    err = capsys.readouterr().err
    assert "Paused: weekly-menu" in err
    assert "Resumed: weekly-menu" in err


def test_room_pause_by_id_skips_the_lookup(tmp_home, patch_room_api) -> None:
    _login(tmp_home)
    patch_room_api(task_cancelled=_room_task(active=False))
    assert cli.main(["scheduled-tasks", "pause", _TASK_ID, "--room", _ROOM]) == 0
    assert _calls() == [("cancel", _TASK_ID, _ROOM)]


def test_room_unknown_task_name_exits_two_without_writing(tmp_home, patch_room_api, capsys) -> None:
    _login(tmp_home)
    patch_room_api(task_list=[_room_task(name="weekly-menu")], task_cancelled=_room_task())
    assert cli.main(["scheduled-tasks", "pause", "nope", "--room", "Trip"]) == 2
    assert "in that room" in capsys.readouterr().err
    assert _calls() == [("list", True, _ROOM)]


def test_room_unknown_room_exits_two_without_a_task_call(tmp_home, patch_room_api) -> None:
    _login(tmp_home)
    patch_room_api(task_list=[])
    assert cli.main(["scheduled-tasks", "list", "--room", "nowhere"]) == 2
    assert _calls() == []


@pytest.mark.parametrize(
    ("flags", "destinations", "topic"),
    [
        (["--to", "resend_email"], ["resend_email"], None),
        (["--to", "groups_service", "--deliver-topic", "Dinner"], ["groups_service"], "Dinner"),
        (["--silent"], [], None),
    ],
)
def test_room_edit_delivery_states(tmp_home, patch_room_api, flags, destinations, topic) -> None:
    _login(tmp_home)
    patch_room_api(task_room_updated=_room_task())
    rc = cli.main(["scheduled-tasks", "edit", _TASK_ID, *flags, "--room", "Trip"])
    assert rc == 0
    assert _calls() == [("room_update", _ROOM, _TASK_ID, {}, destinations, topic)]


def test_room_edit_delivery_has_no_default(tmp_home, patch_room_api, capsys) -> None:
    """A room has no notification preference to follow, and what the server
    stores for "default" depends on a deployment flag — so the room form makes
    the owner say where, rather than send a sentinel on their behalf."""
    _login(tmp_home)
    patch_room_api(task_destinations=_room_task())
    args = ["scheduled-tasks", "edit", _TASK_ID, "--default", "--room", "Trip"]
    assert cli.main(args) == 2
    assert "--default isn't available with --room" in capsys.readouterr().err
    assert _calls() == []


def test_room_edit_delivery_refuses_a_personal_destination(
    tmp_home, patch_room_api, capsys
) -> None:
    _login(tmp_home)
    patch_room_api(task_destinations=_room_task())
    args = ["scheduled-tasks", "edit", _TASK_ID, "--to", "whatsapp", "--room", "Trip"]
    assert cli.main(args) == 2
    assert "isn't available with --room" in capsys.readouterr().err
    assert _calls() == []


@pytest.mark.parametrize(
    "argv",
    [
        ["create", "t", "--prompt", "p", "--cron", "0 9 * * 0"],
        ["pause", _TASK_ID],
        ["resume", _TASK_ID],
        ["edit", _TASK_ID, "--silent"],
        ["edit", _TASK_ID, "--prompt", "New."],
        ["renew", _TASK_ID],
    ],
)
def test_a_non_owner_is_told_only_an_owner_can(tmp_home, patch_room_api, capsys, argv) -> None:
    """Every room write a non-owner tries ends in the same fixed sentence,
    whatever the server's own wording was."""
    _login(tmp_home)
    refused = _forbidden()
    patch_room_api(
        task_created=refused,
        task_cancelled=refused,
        task_resumed=refused,
        task_room_updated=refused,
        task_renewed=refused,
    )
    assert cli.main(["scheduled-tasks", *argv, "--room", "Trip"]) == 1
    assert "only a room owner can change its scheduled tasks." in capsys.readouterr().err


def test_room_errors_other_than_403_carry_the_servers_detail(
    tmp_home, patch_room_api, capsys
) -> None:
    """A full budget or a taken name is the server's sentence, verbatim —
    and it is not mistaken for the owner gate."""
    _login(tmp_home)
    full = api_client.ApiError(status=409, body="", detail="This room is at its limit of tasks.")
    patch_room_api(task_created=full)
    argv = [
        "scheduled-tasks",
        "create",
        "t",
        "--prompt",
        "p",
        "--cron",
        "0 9 * * 0",
        "--room",
        "Trip",
    ]
    assert cli.main(argv) == 1
    err = capsys.readouterr().err
    assert "This room is at its limit of tasks." in err
    assert "only a room owner" not in err


def test_personal_commands_never_touch_a_room(tmp_home, patch_room_api) -> None:
    """Without ``--room`` nothing is addressed to a room, even on a client
    that could: the flag is the only way in."""
    _login(tmp_home)
    row = _task()
    patch_room_api(task_list=[row], task_cancelled=row, task_destinations=row)
    assert cli.main(["scheduled-tasks", "list"]) == 0
    assert cli.main(["scheduled-tasks", "pause", _TASK_ID]) == 0
    assert cli.main(["scheduled-tasks", "edit", _TASK_ID, "--silent"]) == 0
    assert _calls() == [
        ("list", False, None),
        ("cancel", _TASK_ID, None),
        ("destinations", _TASK_ID, [], None, None),
    ]


# ---------------------------------------------------------------------------
# 0.2.0 shape: edit absorbs destinations, `--to room`, renew, old verbs gone


@pytest.mark.parametrize("old", ["add", "destinations", "new", "rm"])
def test_old_verbs_are_rejected(tmp_home, patch_api, old) -> None:
    _login(tmp_home)
    with pytest.raises(SystemExit) as ei:
        cli.main(["scheduled-tasks", old, _TASK_ID])
    assert ei.value.code == 2
    assert _FakeTaskApi.instances == []


def test_edit_sends_fields_first_then_delivery(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(task_updated=_task(), task_destinations=_task())
    rc = cli.main(["scheduled-tasks", "edit", _TASK_ID, "--prompt", "New.", "--silent"])
    assert rc == 0
    kinds = [c[0] for c in _FakeTaskApi.instances[-1].calls]
    assert kinds == ["update", "destinations"]
    assert _last_call("update")[2] == {"prompt": "New."}
    assert _last_call("destinations")[2] == []


def test_edit_whose_delivery_fails_says_the_fields_were_saved(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(
        task_updated=_task(),
        task_destinations=api_client.ApiError(status=503, body="", detail="try later"),
    )
    rc = cli.main(
        ["scheduled-tasks", "edit", _TASK_ID, "--prompt", "New.", "--clear-description", "--silent"]
    )
    assert rc == 1
    assert [c[0] for c in _FakeTaskApi.instances[-1].calls] == ["update", "destinations"]
    err = capsys.readouterr().err
    assert "saved --prompt, --clear-description on 'morning-weather'" in err
    assert "changing its delivery failed" in err and "503: try later" in err


def test_edit_delivery_only_failure_claims_nothing_saved(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(task_destinations=api_client.ApiError(status=503, body="", detail="try later"))
    assert cli.main(["scheduled-tasks", "edit", _TASK_ID, "--silent"]) == 1
    err = capsys.readouterr().err
    assert "saved" not in err and "503: try later" in err


def test_edit_with_only_fields_makes_no_delivery_call(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(task_updated=_task())
    assert cli.main(["scheduled-tasks", "edit", _TASK_ID, "--prompt", "New."]) == 0
    assert [c[0] for c in _FakeTaskApi.instances[-1].calls] == ["update"]


def test_edit_to_room_on_a_personal_task_is_refused(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(task_destinations=_task())
    assert cli.main(["scheduled-tasks", "edit", _TASK_ID, "--to", "room"]) == 2
    assert "only available with --room" in capsys.readouterr().err
    assert cli.main(["scheduled-tasks", "edit", _TASK_ID, "--deliver-topic", "Dinner"]) == 2
    assert "only available with --room" in capsys.readouterr().err
    assert _FakeTaskApi.instances == []


def test_room_create_to_room_goes_over_the_wire_as_groups_service(tmp_home, patch_room_api) -> None:
    _login(tmp_home)
    patch_room_api(task_created=_room_task())
    base = ["scheduled-tasks", "create", "t", "--prompt", "p", "--cron", "0 9 * * 0"]
    assert cli.main([*base, "--room", "Trip", "--to", "room", "--deliver-topic", "Dinner"]) == 0
    last = _last_call("create")[1]
    assert (last["destinations"], last["topic"]) == (["groups_service"], "Dinner")


def test_room_edit_to_room_goes_over_the_wire_as_groups_service(tmp_home, patch_room_api) -> None:
    _login(tmp_home)
    patch_room_api(task_room_updated=_room_task())
    argv = ["scheduled-tasks", "edit", _TASK_ID, "--room", "Trip", "--to", "room"]
    assert cli.main([*argv, "--deliver-topic", "Dinner"]) == 0
    assert _calls() == [("room_update", _ROOM, _TASK_ID, {}, ["groups_service"], "Dinner")]


def test_room_edit_sends_fields_and_delivery_as_one_request(tmp_home, patch_room_api) -> None:
    """Every flag lands in a single call (one atomic PATCH): there is no partial
    save to report."""
    _login(tmp_home)
    patch_room_api(task_room_updated=_room_task())
    argv = [
        "scheduled-tasks",
        "edit",
        _TASK_ID,
        "--room",
        "Trip",
        "--prompt",
        "New.",
        "--cron",
        "0 9 * * 1",
        "--timezone",
        "America/Los_Angeles",
        "--clear-description",
        "--silent",
    ]
    assert cli.main(argv) == 0
    edits = {
        "prompt": "New.",
        "cron": "0 9 * * 1",
        "timezone": "America/Los_Angeles",
        "description": None,
    }
    assert _calls() == [("room_update", _ROOM, _TASK_ID, edits, [], None)]


def test_room_edit_fields_alone_leave_the_delivery_out(tmp_home, patch_room_api) -> None:
    _login(tmp_home)
    patch_room_api(task_room_updated=_room_task())
    argv = ["scheduled-tasks", "edit", _TASK_ID, "--room", "Trip", "--description", "Menu"]
    assert cli.main(argv) == 0
    assert _calls() == [("room_update", _ROOM, _TASK_ID, {"description": "Menu"}, None, None)]


def test_room_edit_at_is_sent_as_an_offset_aware_run_at(tmp_home, patch_room_api) -> None:
    _login(tmp_home)
    patch_room_api(task_room_updated=_room_task())
    argv = ["scheduled-tasks", "edit", _TASK_ID, "--room", "Trip", "--at", "2030-01-01 09:00"]
    assert cli.main(argv) == 0
    run_at = _calls()[0][3]["run_at"]
    assert re.fullmatch(r"2030-01-01T\d\d:\d\d:\d\d([+-]\d\d:\d\d|Z)", run_at)


def test_room_edit_server_refusals_carry_the_servers_detail(
    tmp_home, patch_room_api, capsys
) -> None:
    """The cross-kind and cadence-floor refusals are the server's sentence."""
    _login(tmp_home)
    patch_room_api(
        task_room_updated=api_client.ApiError(status=409, body="", detail="Too frequent.")
    )
    argv = ["scheduled-tasks", "edit", _TASK_ID, "--room", "Trip", "--cron", "* * * * *"]
    assert cli.main(argv) == 1
    assert "Too frequent." in capsys.readouterr().err


@pytest.mark.parametrize(
    ("extra", "said"),
    [
        (["--default"], "--default isn't available with --room"),
        ([], "nothing to change"),
        (["--to", "room", "--silent"], "pass only one of"),
        (["--description", "d", "--clear-description"], "either --description"),
    ],
)
def test_room_edit_usage_errors(tmp_home, patch_room_api, capsys, extra, said) -> None:
    _login(tmp_home)
    patch_room_api(task_room_updated=_room_task())
    assert cli.main(["scheduled-tasks", "edit", _TASK_ID, "--room", "Trip", *extra]) == 2
    assert said in capsys.readouterr().err
    assert _calls() == []


def test_room_create_with_a_skill_and_no_prompt(tmp_home, patch_room_api) -> None:
    _login(tmp_home)
    patch_room_api(task_created=_room_task())
    argv = ["scheduled-tasks", "create", "t", "--skill", "menu", "--cron", "0 9 * * 0"]
    assert cli.main([*argv, "--room", "Trip"]) == 0
    sent = _last_call("create")[1]
    assert (sent["prompt"], sent["skill"], sent["cron"], sent["run_at"]) == (
        None,
        "menu",
        "0 9 * * 0",
        None,
    )


def test_room_create_at_sends_run_at_and_no_cron(tmp_home, patch_room_api) -> None:
    _login(tmp_home)
    patch_room_api(task_created=_room_task())
    argv = ["scheduled-tasks", "create", "t", "--prompt", "p", "--at", "2030-01-01 09:00"]
    assert cli.main([*argv, "--room", "Trip", "--json"]) == 0
    sent = _last_call("create")[1]
    assert sent["cron"] is None and sent["run_at"].startswith("2030-01-01T")


def test_room_create_needs_a_prompt_or_a_skill(tmp_home, patch_room_api, capsys) -> None:
    _login(tmp_home)
    patch_room_api(task_created=_room_task())
    argv = ["scheduled-tasks", "create", "t", "--cron", "0 9 * * 0", "--room", "Trip"]
    assert cli.main(argv) == 2
    assert "--prompt, --skill, or both" in capsys.readouterr().err
    assert _calls() == []


def test_personal_create_needs_a_prompt_and_refuses_a_skill(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(task_created=_task())
    assert cli.main(["scheduled-tasks", "create", "t", "--cron", "0 9 * * 0"]) == 2
    assert "--prompt is required" in capsys.readouterr().err
    argv = ["scheduled-tasks", "create", "t", "--prompt", "p", "--cron", "0 9 * * 0"]
    assert cli.main([*argv, "--skill", "menu"]) == 2
    assert "--skill is only available with --room" in capsys.readouterr().err
    assert _FakeTaskApi.instances == []


def test_room_show_by_name_reads_the_room_and_prints_the_rooms_delivery(
    tmp_home, patch_room_api, capsys
) -> None:
    _login(tmp_home)
    row = _room_task(name="weekly-menu")
    detail = api_client.ScheduledTaskDetail(
        **dataclasses.asdict(_task(name="weekly-menu")),
        latest_run=None,
        group_name="Trip",
        group_members=["Ada"],
        group_delivery="Posted to the room",
    )
    patch_room_api(task_list=[row], task_detail=detail)
    assert cli.main(["scheduled-tasks", "show", "weekly-menu", "--room", "Trip"]) == 0
    captured = capsys.readouterr()
    out = " ".join((captured.out + captured.err).split())
    assert _calls() == [("list", True, _ROOM), ("show", _TASK_ID, _ROOM)]
    assert "Posted to the room" in out
    assert "your default" not in out


def test_room_runs_and_thread_page_with_the_same_cursor_and_envelope(
    tmp_home, patch_room_api, capsys
) -> None:
    _login(tmp_home)
    patch_room_api(
        task_runs=[_run(id="r2"), _run(id="r1", fired_at="2026-06-03T15:00:00+00:00")],
        task_thread=[
            api_client.ScheduledTaskMessage(
                id=f"m{i}",
                role="assistant",
                text="hi",
                channel="main_chat",
                trajectory_id=None,
                created_at=f"2026-06-0{i}T10:00:00+00:00",
            )
            for i in (1, 2)
        ],
    )
    base = ["scheduled-tasks"]
    tail = [_TASK_ID, "--room", _ROOM, "--limit", "2", "--json", "--before", "2026-06-09|x9"]
    assert cli.main([*base, "runs", *tail]) == 0
    runs = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in runs["items"]] == ["r2", "r1"]
    assert runs["next_before"] == "2026-06-03T15:00:00+00:00|r1"
    assert cli.main([*base, "thread", *tail]) == 0
    thread = json.loads(capsys.readouterr().out)
    assert thread["next_before"] == "2026-06-01T10:00:00+00:00|m1"
    assert _calls() == [
        ("runs", _TASK_ID, 2, "2026-06-09", "x9", _ROOM),
        ("thread", _TASK_ID, 2, "2026-06-09", "x9", _ROOM),
    ]


def test_room_renew_goes_to_the_room(tmp_home, patch_room_api, capsys) -> None:
    _login(tmp_home)
    patch_room_api(task_renewed=_room_task(name="weekly-menu"))
    assert cli.main(["scheduled-tasks", "renew", _TASK_ID, "--room", "Trip"]) == 0
    assert _calls() == [("renew", _TASK_ID, _ROOM)]
    assert "Renewed 'weekly-menu'" in capsys.readouterr().err


def test_renew_by_id_prints_the_task(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(task_renewed=_task(name="morning-weather"))
    assert cli.main(["scheduled-tasks", "renew", _TASK_ID]) == 0
    assert _FakeTaskApi.instances[-1].calls == [("renew", _TASK_ID)]
    assert "Renewed 'morning-weather'" in capsys.readouterr().err


def test_renew_by_name_looks_the_task_up(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(task_list=[_task(name="morning-weather")], task_renewed=_task())
    assert cli.main(["scheduled-tasks", "renew", "morning-weather"]) == 0
    calls = _FakeTaskApi.instances[-1].calls
    assert [c[0] for c in calls] == ["list", "renew"]


def test_renew_refusal_prints_the_servers_reason(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(
        task_renewed=api_client.ApiError(
            status=422, body="", detail="This task isn't due for renewal."
        )
    )
    assert cli.main(["scheduled-tasks", "renew", _TASK_ID]) == 1
    assert "isn't due for renewal" in capsys.readouterr().err


def test_renew_json_emits_the_task_and_j8_on_error(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(task_renewed=_task(name="morning-weather"))
    assert cli.main(["scheduled-tasks", "renew", _TASK_ID, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["name"] == "morning-weather"
    patch_api(
        task_renewed=api_client.ApiError(status=422, body="", detail="Not due."),
    )
    assert cli.main(["scheduled-tasks", "renew", _TASK_ID, "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["status"] == 422


def test_renew_unknown_name_json_is_not_found(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(task_list=[], task_renewed=_task())
    assert cli.main(["scheduled-tasks", "renew", "ghost", "--json"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "not_found"


def test_delete_by_id_needs_yes_under_json_and_then_reports_ok(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api()
    assert cli.main(["scheduled-tasks", "delete", _TASK_ID, "--json"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "usage"
    assert _FakeTaskApi.instances == []


def test_delete_with_yes_resolves_the_name_and_emits_the_j5_result(
    tmp_home, patch_api, capsys
) -> None:
    _login(tmp_home)
    patch_api(task_list=[_task(name="morning-weather")], task_deleted=True)
    argv = ["scheduled-tasks", "delete", "morning-weather", "--yes", "--json"]
    assert cli.main(argv) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "id": _TASK_ID}
    assert [c[0] for c in _FakeTaskApi.instances[-1].calls] == ["list", "delete"]
    assert _last_call("delete") == ("delete", _TASK_ID)


def test_delete_human_mode_confirms_and_can_abort(tmp_home, patch_api, monkeypatch, capsys) -> None:
    _login(tmp_home)
    patch_api(task_deleted=True)
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_prompt", lambda _p: "n")
    assert cli.main(["scheduled-tasks", "delete", _TASK_ID]) == 0
    assert "Aborted." in capsys.readouterr().err
    assert _FakeTaskApi.instances == []
    monkeypatch.setattr(cli, "_prompt", lambda _p: "y")
    assert cli.main(["scheduled-tasks", "delete", _TASK_ID]) == 0
    assert "Deleted scheduled task" in capsys.readouterr().err


def test_delete_unknown_name_is_not_found_and_never_deletes(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(task_list=[], task_deleted=True)
    assert cli.main(["scheduled-tasks", "delete", "ghost", "--yes", "--json"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "not_found"
    assert [c[0] for c in _FakeTaskApi.instances[-1].calls] == ["list"]


def test_delete_has_no_room_flag_yet(tmp_home, patch_api) -> None:
    """The server has no room delete route; the flag is absent rather than a dead end."""
    _login(tmp_home)
    with pytest.raises(SystemExit) as ei:
        cli.main(["scheduled-tasks", "delete", _TASK_ID, "--room", "Trip", "--yes"])
    assert ei.value.code == 2
