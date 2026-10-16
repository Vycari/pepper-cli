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
        (
            ["members", "invite", "--room", "Trip", "a@b.co"],
            ("invite_to_group", GID, {"handle": "a@b.co"}),
        ),
        (
            ["apps", "install", "--room", "Trip", "notes"],
            ("install_group_app", GID, {"app_key": "notes"}),
        ),
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
        (
            ["members", "remove", "--room", "Trip", "dana"],
            ("remove_group_member", GID, PID, {}),
        ),
        (
            ["members", "edit", "--room", "Trip", PID, "--role", "owner"],
            ("set_group_member_role", GID, PID, {"role": "owner"}),
        ),
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


@pytest.mark.parametrize("name_args", [[], ["--name", ""], ["--name", "   "]])
def test_create_without_a_name_fails_locally_before_any_request(api, capsys, name_args) -> None:
    before = len(api.calls)
    assert cli.main(["rooms", "create", *name_args, "dana@x.co"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "a room needs a name" in captured.err
    assert "--name" in captured.err
    assert len(api.calls) == before
    assert not any(call[0] in {"create_group", "search_contacts"} for call in api.calls)


def test_create_without_a_name_json_error_is_a_usage_error(api, capsys) -> None:
    assert cli.main(["rooms", "create", "--json", "dana@x.co"]) == 2
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["code"] == "usage"
    assert not any(call[0] == "create_group" for call in api.calls)


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


def _installed_notes(api) -> None:
    api.canned["get_group"] = {
        "apps": {"installed": [{"install_id": "i1", "app_key": "k", "title": "Notes"}]}
    }


def test_uninstall_resolves_the_installed_app_by_title(api, capsys) -> None:
    _installed_notes(api)
    argv = ["apps", "uninstall", "--room", "Trip", "notes"]
    assert cli.main(argv) == 2
    assert not any(call[0] == "uninstall_group_app" for call in api.calls)
    assert cli.main([*argv, "--yes"]) == 0
    assert api.calls[-1] == ("uninstall_group_app", GID, "i1", {})


def test_uninstall_json_reports_the_install_id(api, capsys) -> None:
    _installed_notes(api)
    assert cli.main(["apps", "uninstall", "--room", "Trip", "k", "--yes", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "id": "i1"}


def test_uninstall_of_an_unknown_app_is_a_json_error(api, capsys) -> None:
    _installed_notes(api)
    assert cli.main(["apps", "uninstall", "--room", "Trip", "nope", "--yes", "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["code"] == "not_found"


def test_remove_by_name_reports_the_person_id_under_json(api, capsys) -> None:
    api.canned["remove_group_member"] = {}
    assert cli.main(["members", "remove", "--room", "Trip", "dana", "--yes", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "id": PID}


def test_remove_of_an_unknown_member_is_a_json_error(api, capsys) -> None:
    assert cli.main(["members", "remove", "--room", "Trip", "zed", "--yes", "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)["error"]
    assert error["code"] == "not_found"
    assert "members list" in error["message"]


def test_members_list_json_is_the_envelope(api, capsys) -> None:
    assert cli.main(["members", "list", "--room", "Trip", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["next_before"] is None
    assert [m["person_id"] for m in body["items"]] == [PID]


def test_members_edit_needs_a_role(api, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["members", "edit", "--room", "Trip", PID, "--yes"])
    assert exc.value.code == 2
    assert api.calls == []


_SHARING = {
    "available_calendars": [
        {"calendar_id": "c1", "summary": "Work"},
        {"calendar_id": "c2", "summary": "Home"},
    ],
    "available_levels": ["none", "busy", "details"],
    "calendar": {"calendars": {"c1": "busy"}},
}


def test_sharing_show_lists_each_calendar_and_its_level(api, capsys) -> None:
    api.canned["get_group_sharing"] = {**_SHARING, "default_levels": {"c2": "busy"}}
    assert cli.main(["sharing", "show", "--room", "Trip"]) == 0
    assert capsys.readouterr().out.splitlines() == ["Work  busy", "Home  busy (default)"]
    assert [c[0] for c in api.calls] == ["get_group_sharing"]


def test_sharing_edit_keeps_the_calendars_it_was_not_asked_to_change(api) -> None:
    api.canned["get_group_sharing"] = _SHARING
    assert cli.main(["sharing", "edit", "--room", "Trip", "--set", "home=details"]) == 0
    assert api.calls[-1] == (
        "set_group_sharing",
        GID,
        {"calendars": {"c1": "busy", "c2": "details"}},
    )


def test_sharing_default_drops_the_explicit_choice(api) -> None:
    api.canned["get_group_sharing"] = _SHARING
    assert cli.main(["sharing", "edit", "--room", "Trip", "--set", "Work=default"]) == 0
    assert api.calls[-1] == ("set_group_sharing", GID, {"calendars": {}})


def test_sharing_edit_json_prints_the_saved_record(api, capsys) -> None:
    api.canned["get_group_sharing"] = _SHARING
    api.canned["set_group_sharing"] = {**_SHARING, "calendar": {"calendars": {"c2": "busy"}}}
    assert cli.main(["sharing", "edit", "--room", "Trip", "--set", "Home=busy", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["calendar"] == {"calendars": {"c2": "busy"}}


@pytest.mark.parametrize("item", ["Nowhere=busy", "Work=bogus"])
def test_sharing_edit_rejects_an_unknown_calendar_or_level(api, capsys, item) -> None:
    api.canned["get_group_sharing"] = _SHARING
    assert cli.main(["sharing", "edit", "--room", "Trip", "--set", item, "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["code"] == "usage"
    assert not any(call[0] == "set_group_sharing" for call in api.calls)


@pytest.mark.parametrize("item", ["busy", "=busy", " =busy", "Work="])
def test_sharing_edit_needs_a_calendar_and_a_level_in_each_set(api, capsys, item) -> None:
    api.canned["get_group_sharing"] = {
        **_SHARING,
        "available_calendars": [{"calendar_id": "c1", "summary": "Work"}],
    }
    assert cli.main(["sharing", "edit", "--room", "Trip", "--set", item, "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["code"] == "usage"
    assert api.calls == []


def test_apps_list_says_when_the_catalog_could_not_be_loaded(api, capsys) -> None:
    api.canned["group_app_launcher"] = ([], False)
    api.canned["get_group"] = {"apps": {"catalog": [], "catalog_unavailable": True}}
    assert cli.main(["apps", "list", "--room", "Trip", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["catalog"] == []
    assert body["catalog_unavailable"] is True
    assert cli.main(["apps", "list", "--room", "Trip"]) == 0
    assert "installable apps unavailable right now" in capsys.readouterr().err


def test_apps_uninstall_reports_an_outage_not_absence(api, capsys) -> None:
    api.canned["get_group"] = {"apps": {"installed": [], "unavailable": True}}
    argv = ["apps", "uninstall", "--room", "Trip", "notes", "--yes", "--json"]
    assert cli.main(argv) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["code"] == "server"
    assert not any(call[0] == "uninstall_group_app" for call in api.calls)


def test_apps_list_json_is_the_envelope_with_the_catalog(api, capsys) -> None:
    api.canned["group_app_launcher"] = (
        [api_client.GroupAppTile("notes", "notes", "Notes", GID)],
        False,
    )
    api.canned["get_group"] = {"apps": {"catalog": [{"app_key": "shopping"}]}}
    assert cli.main(["apps", "list", "--room", "Trip", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert [t["app_key"] for t in body["items"]] == ["notes"]
    assert body["next_before"] is None
    assert body["installed_unavailable"] is False
    assert body["catalog"] == [{"app_key": "shopping"}]
    assert body["catalog_unavailable"] is False


def test_apps_list_text_says_when_nothing_is_installed(api, capsys) -> None:
    api.canned["group_app_launcher"] = ([], False)
    api.canned["get_group"] = {"apps": {"catalog": [{"app_key": "shopping"}]}}
    assert cli.main(["apps", "list", "--room", "Trip"]) == 0
    captured = capsys.readouterr()
    assert "(no apps installed)" in captured.out
    assert "installable: shopping" in captured.err
    api.canned["group_app_launcher"] = ([], True)
    assert cli.main(["apps", "list", "--room", "Trip"]) == 0
    assert "unavailable right now" in capsys.readouterr().err
