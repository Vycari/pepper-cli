"""The room-acting commands of ``rooms``, ``topics``, ``messages`` and ``attachments``: each
reaches the right ``ApiClient`` method, and the destructive ones ask first."""

from __future__ import annotations

import json

import pytest

from pepper_cli import api_client, cli, credentials
from pepper_cli.secret import SecretStr
from tests.test_cli_groups import _message

GID = "00000000-0000-0000-0000-0000000000a1"
TID = "00000000-0000-0000-0000-0000000000b1"
MID = "00000000-0000-0000-0000-0000000000c1"
PID = "00000000-0000-0000-0000-0000000000d1"


class _Api:
    """Records every call; answers from ``canned`` by method name, else ``{}``."""

    calls: list[tuple] = []
    canned: dict = {}

    def __init__(self, **_: object) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def list_groups(self, *, archived=False):
        topic = api_client.GroupTopicSummary(TID, "General", "2026-10-02")
        room = api_client.GroupSummary(GID, "Trip", "#fff", "", 2, False, [topic])
        return api_client.GroupList(True, False, 1, 0, [] if archived else [room])

    def group_roster(self, group_id):
        return [api_client.GroupRosterMember(PID, "Dana")]

    def __getattr__(self, name):
        def call(*args, **kwargs):
            _Api.calls.append((name, *args, kwargs))
            result = _Api.canned.get(name, {})
            if isinstance(result, Exception):
                raise result
            return result

        return call


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    _Api.calls, _Api.canned = [], {}
    monkeypatch.setattr(api_client, "ApiClient", _Api)
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    return _Api


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (
            ["messages", "edit", "--room", "Trip", MID, "new", "text"],
            ("edit_group_message", GID, MID, {"body": "new text"}),
        ),
        (
            ["messages", "react", "--room", "Trip", MID, "🎉"],
            ("react_to_group_message", GID, MID, {"emoji": "🎉"}),
        ),
        (
            ["messages", "unreact", "--room", "Trip", MID, "🎉"],
            ("clear_group_reaction", GID, MID, {"emoji": "🎉"}),
        ),
        (
            ["topics", "create", "--room", "Trip", "Plans"],
            ("create_group_topic", GID, {"name": "Plans"}),
        ),
        (
            ["topics", "edit", "--room", "Trip", TID, "--name", "Docs"],
            ("rename_group_topic", GID, TID, {"name": "Docs"}),
        ),
        (
            ["topics", "archive", "--room", "Trip", TID],
            ("set_group_topic_archived", GID, TID, {"archived": True}),
        ),
        (["rooms", "edit", "Trip", "--name", "Trip 2"], ("rename_group", GID, {"name": "Trip 2"})),
        (["rooms", "archive", "Trip"], ("set_group_archived", GID, {"archived": True})),
        (["rooms", "unarchive", "Trip"], ("set_group_archived", GID, {"archived": False})),
        (["rooms", "mute", "Trip"], ("set_group_muted", GID, {"muted": True})),
        (["rooms", "unmute", "Trip"], ("set_group_muted", GID, {"muted": False})),
    ],
)
def test_command_reaches_its_api_method(api, argv, expected) -> None:
    assert cli.main(argv) == 0
    assert api.calls == [expected]


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (
            ["messages", "delete", "--room", "Trip", MID],
            ("delete_group_message", GID, MID, {}),
        ),
        (["rooms", "leave", "Trip"], ("leave_group", GID, {})),
    ],
)
def test_destructive_commands_need_yes_off_a_terminal(api, argv, expected) -> None:
    assert cli.main(argv) == 2
    assert api.calls == []
    assert cli.main([*argv, "--yes"]) == 0
    assert api.calls == [expected]


def test_destructive_command_asks_on_a_terminal(api, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_prompt", lambda _: "n")
    assert cli.main(["rooms", "leave", "Trip"]) == 0
    assert api.calls == []
    monkeypatch.setattr(cli, "_prompt", lambda _: "y")
    assert cli.main(["rooms", "leave", "Trip"]) == 0
    assert api.calls == [("leave_group", GID, {})]


def test_destructive_command_refuses_under_json_without_yes(api, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    assert cli.main(["rooms", "leave", "Trip", "--json"]) == 2
    assert api.calls == []
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "usage"


@pytest.mark.parametrize(
    ("status", "rc", "words"), [(404, 2, "not found"), (403, 1, "not allowed")]
)
def test_api_errors_print_one_line(api, capsys, status, rc, words) -> None:
    api.canned["rename_group"] = api_client.ApiError(status=status, body="", detail="nope")
    assert cli.main(["rooms", "edit", "Trip", "--name", "x"]) == rc
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1
    assert words in err[0]


@pytest.mark.parametrize(("status", "code"), [(404, "not_found"), (403, "forbidden")])
def test_api_errors_are_json_under_json(api, capsys, status, code) -> None:
    api.canned["rename_group"] = api_client.ApiError(status=status, body="", detail="nope")
    assert cli.main(["rooms", "edit", "Trip", "--name", "x", "--json"]) != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["code"] == code


def test_create_resolves_a_contact_name_to_its_handle(api, capsys) -> None:
    api.canned["search_contacts"] = [{"name": "Dana Lee", "handle": "dana@x.co"}]
    api.canned["create_group"] = {"group_id": GID, "created": True}
    assert cli.main(["rooms", "create", "--name", "Plans", "dana", "+15551234567"]) == 0
    assert api.calls[-1] == (
        "create_group",
        {"people": ["dana@x.co", "+15551234567"], "name": "Plans"},
    )
    assert capsys.readouterr().out.strip() == GID


def test_list_mark_marks_through_the_newest_message(api) -> None:
    api.canned["list_group_messages"] = api_client.GroupMessagePage(
        [
            _message(created_at="2026-10-01T10:00:00+00:00"),
            _message(created_at="2026-10-02T10:00:00+00:00"),
        ]
    )
    api.canned["list_group_topics"] = [api_client.GroupTopicSummary(TID, "General", "2026-10-02")]
    assert cli.main(["messages", "list", "--room", "Trip", "--mark"]) == 0
    assert api.calls[-1] == (
        "set_group_read_mark",
        GID,
        TID,
        {"read_through": "2026-10-02T10:00:00+00:00"},
    )


def test_list_json_mark_failure_prints_nothing_and_fails(api, capsys) -> None:
    api.canned["list_group_messages"] = api_client.GroupMessagePage(
        [_message(created_at="2026-10-02T10:00:00+00:00")]
    )
    api.canned["list_group_topics"] = [api_client.GroupTopicSummary(TID, "General", "2026-10-02")]
    api.canned["set_group_read_mark"] = api_client.ApiError(status=500, body="", detail="boom")
    assert cli.main(["messages", "list", "--room", "Trip", "--json", "--mark"]) == 1
    assert capsys.readouterr().out == ""


def test_activity_names_the_recent_topics(api) -> None:
    api.canned["group_activity"] = []
    api.canned["list_group_topics"] = [api_client.GroupTopicSummary(TID, "General", "2026-10-02")]
    assert cli.main(["rooms", "activity", "--since", "2026-10-01T00:00:00+00:00"]) == 0
    assert api.calls[1:] == [
        ("group_activity", {"since": "2026-10-01T00:00:00+00:00", "topic_ids": [TID]}),
    ]


def test_download_to_an_unwritable_path_is_one_line(api, tmp_path, capsys) -> None:
    api.canned["get_group_attachment"] = b"x"
    argv = ["attachments", "download", "--room", "Trip", MID, "--out", str(tmp_path / "no" / "f")]
    assert cli.main(argv) == 2
    assert "couldn't write" in capsys.readouterr().err


def test_download_json_reports_the_path_and_size(api, tmp_path, capsys) -> None:
    api.canned["get_group_attachment"] = b"xyz"
    out = tmp_path / "f.bin"
    assert (
        cli.main(["attachments", "download", "--room", "Trip", MID, "--out", str(out), "--json"])
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {"path": str(out), "bytes": 3}
    assert out.read_bytes() == b"xyz"


def test_mutations_print_the_server_object_or_ok_under_json(api, capsys) -> None:
    api.canned["rename_group"] = {"group_id": GID, "display_name": "Trip 2"}
    assert cli.main(["rooms", "edit", "Trip", "--name", "Trip 2", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"group_id": GID, "display_name": "Trip 2"}
    assert cli.main(["rooms", "mute", "Trip", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "id": GID}


def test_react_prints_the_reactions_under_json(api, capsys) -> None:
    api.canned["react_to_group_message"] = [{"emoji": "🎉", "count": 1}]
    assert cli.main(["messages", "react", "--room", "Trip", MID, "🎉", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"reactions": [{"emoji": "🎉", "count": 1}]}


def test_unknown_room_is_a_json_error_under_json(api, capsys) -> None:
    assert cli.main(["rooms", "mute", "nowhere", "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["code"] == "not_found"


def test_rooms_switched_off_is_one_line_and_a_json_error(api, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        _Api,
        "list_groups",
        lambda self, *, archived=False: api_client.GroupList(False, False, 0, 0, []),
    )
    assert cli.main(["rooms", "mute", "Trip"]) == 1
    assert capsys.readouterr().err.strip() == "error: Rooms are not enabled for your account."
    assert cli.main(["rooms", "mute", "Trip", "--json"]) == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "forbidden"


def test_topics_search_filters_to_one_room_by_name(api, capsys) -> None:
    api.canned["search_group_topics"] = [
        {"group_name": "Trip", "name": "Flights", "topic_id": TID},
        {"group_name": "Work", "name": "Flights", "topic_id": "t-other"},
    ]
    assert cli.main(["topics", "search", "--room", "Trip", "flights", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [row["topic_id"] for row in payload["items"]] == [TID]
    assert api.calls[0] == ("search_group_topics", "flights", {})


def test_download_to_stdout_cannot_be_json(api, capsys) -> None:
    assert cli.main(["attachments", "download", "--room", "Trip", MID, "--out", "-", "--json"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "usage"
    assert api.calls == []


def test_messages_edit_without_text_is_a_usage_error(api, capsys) -> None:
    assert cli.main(["messages", "edit", "--room", "Trip", MID]) == 2
    assert api.calls == []
    assert capsys.readouterr().err.strip()
