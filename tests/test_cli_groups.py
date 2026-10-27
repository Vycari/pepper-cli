"""End-to-end tests for ``pepper rooms``, ``topics``, ``messages`` (the read/list/post paths).

Same approach as ``test_cli_todos.py`` / ``test_cli_scheduled_tasks.py``:
invoke ``cli.main([...])`` with a monkeypatched ``api_client.ApiClient`` so
commands run without the network, and exercise credentials through the real
(tmp-home) module.

Name/id resolution (``_resolve_group`` / ``_resolve_topic``) is exercised
through the CLI commands rather than unit-tested in isolation, since the
whole point of a shared resolver is that every command that takes a
``--room``/``--topic`` argument behaves identically — ``rooms show`` stands in for
all of them for the ambiguous/none/unique cases.
"""

from __future__ import annotations

import json

import httpx
import pytest

from pepper_cli import api_client, cli, credentials
from pepper_cli.secret import SecretStr

_GID_TRIP = "00000000-0000-0000-0000-0000000000a1"
_GID_WORK = "00000000-0000-0000-0000-0000000000a2"
_TID_FLIGHTS = "00000000-0000-0000-0000-0000000000b1"
_TID_GENERAL = "00000000-0000-0000-0000-0000000000b2"
_TID_ARCHIVED = "00000000-0000-0000-0000-0000000000b3"
_MID = "00000000-0000-0000-0000-0000000000c1"


def _login(tmp_home) -> None:
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))


def _group(**over) -> api_client.GroupSummary:
    base = {
        "group_id": _GID_TRIP,
        "display_name": "Trip planning",
        "color": "#ffaa00",
        "sub": "active 2h ago",
        "member_count": 3,
        "archived": False,
        "topics": [],
    }
    base.update(over)
    return api_client.GroupSummary(**base)


def _group_list(*groups, enabled=True, degraded=False, archived_total=0) -> api_client.GroupList:
    return api_client.GroupList(
        enabled=enabled,
        degraded=degraded,
        total=len(groups),
        archived_total=archived_total,
        groups=list(groups),
    )


def _topic(**over) -> api_client.GroupTopicSummary:
    base = {
        "topic_id": _TID_GENERAL,
        "name": "General",
        "last_activity_at": "2026-09-16T17:30:00+00:00",
        "dedicated_conversation": False,
        "archived_at": None,
        "imported_history": False,
    }
    base.update(over)
    return api_client.GroupTopicSummary(**base)


def _message(**over) -> api_client.GroupMessage:
    base = {
        "id": _MID,
        "is_pepper": False,
        "is_you": False,
        "is_deleted": False,
        "is_broken": False,
        "run_start": True,
        "display_name": "Dana",
        "initial": "D",
        "avatar_tint": "t1",
        "raw_body": "hello there",
        "rendered_html": "<p>hello there</p>",
        "created_at": "2026-09-16T17:30:00+00:00",
        "created_at_display": "5:30 PM",
        "day_key": "2026-09-16",
        "day_kicker": "WEDNESDAY",
        "day_heading": "September 16, 2026",
        "day_label": "Wednesday, September 16",
        "quote_excerpt": "",
    }
    base.update(over)
    return api_client.GroupMessage(**base)


def _page(*messages, next_before=None) -> api_client.GroupMessagePage:
    return api_client.GroupMessagePage(messages=list(messages), next_before=next_before)


class _FakeGroupsApi:
    """Records calls and returns canned groups responses (or raises)."""

    instances: list[_FakeGroupsApi] = []

    def __init__(self, *, server_url, token, **canned) -> None:
        self.server_url = server_url
        self.token = token
        self._canned = canned
        self.calls: list[tuple] = []
        _FakeGroupsApi.instances.append(self)

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

    def list_groups(self, *, archived=False):
        self.calls.append(("list_groups", archived))
        key = "archived_groups" if archived else "groups"
        return self._result(key)

    def get_group(self, group_id):
        self.calls.append(("get_group", group_id))
        return self._result("group_detail")

    def list_group_topics(self, group_id):
        self.calls.append(("list_group_topics", group_id))
        # ``messages list`` looks the topic up for a note header; most tests need no topics.
        if "topics" not in self._canned:
            return []
        return self._result("topics")

    def list_group_messages(
        self, group_id, topic_id, *, since=None, before=None, at=None, limit=None
    ):
        self.calls.append(("list_group_messages", group_id, topic_id, since, before, at, limit))
        return self._result("messages_page")

    def post_group_message(
        self,
        group_id,
        topic_id,
        *,
        message,
        attachment_ids=None,
        mentions=None,
        parent_message_id=None,
        idempotency_key=None,
        on_behalf_of_person_id=None,
    ):
        self.calls.append(
            (
                "post_group_message",
                group_id,
                topic_id,
                message,
                attachment_ids,
                idempotency_key,
                mentions,
                parent_message_id,
                on_behalf_of_person_id,
            )
        )
        return self._result("posted")

    def group_roster(self, group_id):
        self.calls.append(("group_roster", group_id))
        return self._result("roster")

    def upload_group_attachment(self, group_id, *, filename, content, content_type):
        self.calls.append(("upload_group_attachment", group_id, filename, content, content_type))
        return self._result("uploaded")


@pytest.fixture
def patch_api(monkeypatch):
    _FakeGroupsApi.instances.clear()

    def install(**canned):
        def factory(**kwargs):
            return _FakeGroupsApi(**canned, **kwargs)

        monkeypatch.setattr(api_client, "ApiClient", factory)

    return install


# ---------------------------------------------------------------------------
# list


def test_rooms_list_renders_rows(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(groups=_group_list(_group(display_name="Trip planning")))
    rc = cli.main(["rooms", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Trip planning" in out
    assert "3 members" in out
    assert "active 2h ago" in out
    assert _GID_TRIP in out
    assert _FakeGroupsApi.instances[-1].calls == [("list_groups", False)]


def test_rooms_list_empty(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(groups=_group_list())
    rc = cli.main(["rooms", "list"])
    assert rc == 0
    assert "(no rooms)" in capsys.readouterr().out


def test_rooms_list_passes_archived_flag(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(archived_groups=_group_list(_group(archived=True)))
    rc = cli.main(["rooms", "list", "--archived"])
    assert rc == 0
    assert _FakeGroupsApi.instances[-1].calls == [("list_groups", True)]


def test_rooms_list_json_shape(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(groups=_group_list(_group()))
    rc = cli.main(["rooms", "list", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["enabled"] is True
    assert payload["items"][0]["group_id"] == _GID_TRIP
    assert payload["items"][0]["display_name"] == "Trip planning"
    assert payload["next_before"] is None
    assert (payload["degraded"], payload["total"], payload["archived_total"]) == (False, 1, 0)


def test_rooms_list_archived_help_says_only_archived(capsys) -> None:
    """``--archived`` replaces the default listing rather than adding to it —
    the help text should say so plainly rather than reading as if archived
    rooms are folded into the default list."""
    with pytest.raises(SystemExit):
        cli._build_parser()[0].parse_args(["rooms", "list", "--help"])
    assert "only archived" in capsys.readouterr().out.lower()


def test_rooms_list_does_not_wrap_a_long_group_id(tmp_home, patch_api, capsys) -> None:
    """A room's id is the one thing on this line a person is likely to
    copy-paste; Rich's default wrapping (80 columns in a non-tty capture)
    would otherwise fold it across two lines, breaking the paste. A
    contiguous ``long_id in out`` only holds if Rich never inserted a
    newline in the middle of it.
    """
    _login(tmp_home)
    long_id = "0" * 100
    patch_api(groups=_group_list(_group(group_id=long_id)))
    rc = cli.main(["rooms", "list"])
    assert rc == 0
    assert long_id in capsys.readouterr().out


def test_rooms_list_capability_off_errors_nonzero(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(groups=_group_list(enabled=False))
    rc = cli.main(["rooms", "list"])
    assert rc == 1
    assert "not enabled" in capsys.readouterr().err


def test_rooms_list_without_credentials_errors(tmp_home, patch_api) -> None:
    patch_api(groups=_group_list())
    rc = cli.main(["rooms", "list"])
    assert rc == 1


def test_rooms_list_degraded_prints_a_note_but_still_renders(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(groups=_group_list(_group(), degraded=True))
    rc = cli.main(["rooms", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "degraded" in out
    assert "Trip planning" in out


# ---------------------------------------------------------------------------
# show / name resolution


def _room_detail(**over) -> dict:
    base = {
        "group_id": _GID_TRIP,
        "display_name": "Trip planning",
        "kind": "open",
        "can_manage": True,
        "viewer_role": "owner",
        "owner_count": 1,
        "archived": False,
        "members": [
            {
                "person_id": "p-you",
                "display_name": "You",
                "is_you": True,
                "started_room": True,
                "has_account": True,
                "role": "owner",
            },
            {
                "person_id": "p-dana",
                "display_name": "Dana",
                "is_you": False,
                "started_room": False,
                "has_account": True,
                "role": "member",
            },
        ],
        "topics": [
            {"topic_id": _TID_GENERAL, "name": "General", "archived": False},
            {"topic_id": _TID_ARCHIVED, "name": "Old planning", "archived": True},
        ],
        "apps": {
            "installed": [{"install_id": "i-1", "app_key": "todo", "title": "To-do"}],
            "catalog": [],
            "unavailable": False,
            "catalog_unavailable": False,
        },
        "context": {"sources": [], "truncated": False, "unavailable": False},
    }
    base.update(over)
    return base


def test_rooms_show_renders_kind_members_topics_apps(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(group_detail=_room_detail())
    rc = cli.main(["rooms", "show", _GID_TRIP])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Trip planning" in out
    assert "channel" in out  # kind "open" -> "channel"
    assert "Dana" in out
    assert "owner" in out
    assert "General" in out
    assert "To-do" in out


def test_rooms_show_marks_an_agent_and_its_sponsor(tmp_home, patch_api, capsys) -> None:
    detail = _room_detail()
    detail["members"].append(
        {
            "person_id": "p-agent",
            "display_name": "Watcher",
            "is_you": False,
            "started_room": False,
            "has_account": False,
            "role": "member",
            "is_agent": True,
            "sponsor_display_name": "Dana",
        }
    )
    _login(tmp_home)
    patch_api(group_detail=detail)
    rc = cli.main(["rooms", "show", _GID_TRIP])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Watcher (agent, run by Dana)" in out
    assert "Dana  —" in out  # a person's row carries no agent tag


def test_messages_list_marks_an_agent_post_and_its_requester(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(
        messages_page=_page(
            _message(id="m1", display_name="Watcher", is_agent=True, for_display_name="Ada"),
            _message(id="m2", display_name="Watcher", is_agent=True),
            _message(
                id="m3",
                display_name="Sentinel",
                is_agent=True,
                for_display_name="Bea",
                is_deleted=True,
            ),
        )
    )
    rc = cli.main(["messages", "list", "--room", _GID_TRIP, "--topic", _TID_GENERAL])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Watcher (agent, for Ada)" in out
    assert "Watcher (agent):" in out
    # A deleted agent post still says it was an agent's.
    assert "Sentinel (agent, for Bea): (message deleted)" in out


def test_rooms_show_sealed_kind_renders_as_email_text_thread(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(group_detail=_room_detail(kind="sealed"))
    rc = cli.main(["rooms", "show", _GID_TRIP])
    assert rc == 0
    out = capsys.readouterr().out
    assert "(email/text thread)" in out
    assert "sealed" not in out


def test_rooms_show_resolves_by_unique_prefix(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(
        groups=_group_list(
            _group(group_id=_GID_TRIP, display_name="Trip planning"),
            _group(group_id=_GID_WORK, display_name="Work sync"),
        ),
        group_detail=_room_detail(),
    )
    rc = cli.main(["rooms", "show", "trip"])
    assert rc == 0
    assert ("get_group", _GID_TRIP) in _FakeGroupsApi.instances[-1].calls


def test_rooms_show_ambiguous_name_lists_candidates_and_exits_nonzero(
    tmp_home, patch_api, capsys
) -> None:
    _login(tmp_home)
    patch_api(
        groups=_group_list(
            _group(group_id=_GID_TRIP, display_name="Trip planning"),
            _group(group_id=_GID_WORK, display_name="Trip logistics"),
        )
    )
    rc = cli.main(["rooms", "show", "trip"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "more than one room" in err
    assert "Trip planning" in err
    assert "Trip logistics" in err
    # never reached the point of calling get_group
    assert not any(c[0] == "get_group" for c in _FakeGroupsApi.instances[-1].calls)


def test_rooms_show_ambiguous_name_error_does_not_wrap_ids(tmp_home, patch_api, capsys) -> None:
    """Same shape as the wrapping fixes in ``render.py`` — the candidate
    listing an ambiguous name produces carries an id a person would want to
    copy-paste to disambiguate, and it must not be folded across lines."""
    _login(tmp_home)
    long_id = "2" * 100
    patch_api(
        groups=_group_list(
            _group(group_id=long_id, display_name="Trip planning"),
            _group(group_id=_GID_WORK, display_name="Trip logistics"),
        )
    )
    rc = cli.main(["rooms", "show", "trip"])
    assert rc == 2
    assert long_id in capsys.readouterr().err


def test_rooms_show_unknown_name_errors(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    # The resolver falls back to the archived fold when nothing matches the
    # unarchived one, so a "genuinely no such room" case has to answer empty
    # there too.
    patch_api(groups=_group_list(_group()), archived_groups=_group_list())
    rc = cli.main(["rooms", "show", "nope"])
    assert rc == 2
    assert "no room named" in capsys.readouterr().err


def test_rooms_show_capability_off_via_name_errors(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(groups=_group_list(enabled=False))
    rc = cli.main(["rooms", "show", "trip"])
    assert rc == 1
    assert "not enabled" in capsys.readouterr().err


def test_rooms_show_404_renders_a_clean_one_line_error(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(group_detail=api_client.ApiError(status=404, body="", detail="No such group."))
    rc = cli.main(["rooms", "show", _GID_TRIP])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not found" in err
    assert err.count("\n") <= 1


def test_rooms_show_403_renders_a_clean_one_line_error(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(group_detail=api_client.ApiError(status=403, body="", detail="Not a member."))
    rc = cli.main(["rooms", "show", _GID_TRIP])
    assert rc == 1
    err = capsys.readouterr().err
    assert "not allowed" in err
    assert "Not a member." in err


# ---------------------------------------------------------------------------
# topics


def test_topics_list_hides_archived_by_default(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(
        topics=[
            _topic(topic_id=_TID_GENERAL, name="General"),
            _topic(topic_id=_TID_ARCHIVED, name="Old planning", archived_at="2026-01-01T00:00:00Z"),
        ]
    )
    rc = cli.main(["topics", "list", "--room", _GID_TRIP])
    assert rc == 0
    out = capsys.readouterr().out
    assert "General" in out
    assert "Old planning" not in out


def test_topics_list_keeps_an_archived_default_identifiable(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(
        topics=[
            _topic(topic_id="t-live", name="Flights"),
            _topic(
                topic_id=_TID_ARCHIVED,
                name="General",
                archived_at="2026-01-01T00:00:00Z",
                is_default=True,
            ),
            _topic(topic_id="t-old", name="Old planning", archived_at="2026-01-01T00:00:00Z"),
        ]
    )
    rc = cli.main(["topics", "list", "--room", _GID_TRIP, "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)["items"]
    assert [t["topic_id"] for t in payload] == ["t-live", _TID_ARCHIVED]
    assert [t["is_default"] for t in payload] == [False, True]
    assert payload[1]["archived_at"] is not None


def test_topics_list_archived_flag_includes_archived(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(
        topics=[
            _topic(topic_id=_TID_GENERAL, name="General"),
            _topic(topic_id=_TID_ARCHIVED, name="Old planning", archived_at="2026-01-01T00:00:00Z"),
        ]
    )
    rc = cli.main(["topics", "list", "--room", _GID_TRIP, "--archived"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "General" in out
    assert "Old planning" in out


def test_topics_list_json_shape(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(topics=[_topic(topic_id=_TID_GENERAL, name="General")])
    rc = cli.main(["topics", "list", "--room", _GID_TRIP, "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)["items"]
    assert payload[0]["topic_id"] == _TID_GENERAL
    assert payload[0]["name"] == "General"
    assert payload[0]["is_default"] is False


def test_topics_list_json_prints_which_topic_is_the_default(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(
        topics=[
            _topic(topic_id=_TID_GENERAL, name="General", is_default=True),
            _topic(topic_id="t-other", name="Flights"),
        ]
    )
    rc = cli.main(["topics", "list", "--room", _GID_TRIP, "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)["items"]
    assert [t["is_default"] for t in payload] == [True, False]


def test_topics_list_empty(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(topics=[])
    rc = cli.main(["topics", "list", "--room", _GID_TRIP])
    assert rc == 0
    assert "(no topics)" in capsys.readouterr().out


def test_topics_list_does_not_wrap_a_long_topic_id(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    long_id = "1" * 100
    patch_api(topics=[_topic(topic_id=long_id)])
    rc = cli.main(["topics", "list", "--room", _GID_TRIP])
    assert rc == 0
    assert long_id in capsys.readouterr().out


# ---------------------------------------------------------------------------
# read


def test_messages_list_renders_transcript_oldest_first(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(
        messages_page=_page(
            _message(display_name="Dana", raw_body="hi there", created_at_display="5:30 PM")
        )
    )
    rc = cli.main(["messages", "list", "--room", _GID_TRIP, "--topic", _TID_GENERAL])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Dana" in out
    assert "hi there" in out
    assert "5:30 PM" in out


def test_messages_list_prints_a_day_header_when_the_day_changes(
    tmp_home, patch_api, capsys
) -> None:
    """A transcript spanning more than one day should say which day each run
    of messages is from, not just a bare clock time — the payload already
    carries ``day_key``/``day_label`` for exactly this."""
    _login(tmp_home)
    patch_api(
        messages_page=_page(
            _message(
                id="m1",
                day_key="2026-09-15",
                day_label="Tuesday, September 15",
                created_at_display="5:30 PM",
            ),
            _message(
                id="m2",
                day_key="2026-09-16",
                day_label="Wednesday, September 16",
                created_at_display="9:00 AM",
            ),
        )
    )
    rc = cli.main(["messages", "list", "--room", _GID_TRIP, "--topic", _TID_GENERAL])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Tuesday, September 15" in out
    assert "Wednesday, September 16" in out
    # The day header comes before that day's messages.
    assert out.index("Tuesday, September 15") < out.index("5:30 PM")
    assert out.index("Wednesday, September 16") < out.index("9:00 AM")


def test_messages_list_does_not_repeat_the_day_header_within_one_day(
    tmp_home, patch_api, capsys
) -> None:
    _login(tmp_home)
    patch_api(
        messages_page=_page(
            _message(id="m1", day_key="2026-09-16", day_label="Wednesday, September 16"),
            _message(id="m2", day_key="2026-09-16", day_label="Wednesday, September 16"),
        )
    )
    rc = cli.main(["messages", "list", "--room", _GID_TRIP, "--topic", _TID_GENERAL])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.count("Wednesday, September 16") == 1


def test_messages_list_placeholder_between_same_day_messages_prints_no_header(
    tmp_home, patch_api, capsys
) -> None:
    """The broken-message placeholder carries an empty ``day_key``/``day_label``
    when its ``created_at`` couldn't be parsed. It must neither print a blank
    header nor reset the current day — else the next same-day message would
    repeat that day's header."""
    _login(tmp_home)
    patch_api(
        messages_page=_page(
            _message(id="m1", day_key="2026-09-16", day_label="Wednesday, September 16"),
            _message(
                id="m2",
                is_broken=True,
                day_key="",
                day_kicker="",
                day_heading="",
                day_label="",
            ),
            _message(id="m3", day_key="2026-09-16", day_label="Wednesday, September 16"),
        )
    )
    rc = cli.main(["messages", "list", "--room", _GID_TRIP, "--topic", _TID_GENERAL])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.count("Wednesday, September 16") == 1
    assert "(message could not be rendered)" in out
    assert "\n\n" not in out


def test_messages_list_placeholder_first_prints_no_blank_header(
    tmp_home, patch_api, capsys
) -> None:
    """A placeholder at the top of the page prints no empty header line, and
    the first real day's header that follows it gets no leading blank line."""
    _login(tmp_home)
    patch_api(
        messages_page=_page(
            _message(
                id="m1",
                is_broken=True,
                day_key="",
                day_kicker="",
                day_heading="",
                day_label="",
            ),
            _message(id="m2", day_key="2026-09-16", day_label="Wednesday, September 16"),
        )
    )
    rc = cli.main(["messages", "list", "--room", _GID_TRIP, "--topic", _TID_GENERAL])
    assert rc == 0
    lines = capsys.readouterr().out.splitlines()
    assert "" not in lines
    assert lines[0].startswith("5:30 PM")
    assert lines.count("Wednesday, September 16") == 1


def test_messages_list_renders_a_message_body_with_bracket_markup_literally(
    tmp_home, patch_api, capsys
) -> None:
    """A participant's message body must never be parsed as Rich markup —
    it's untrusted text, and an unmatched tag would otherwise abort the
    whole render instead of just printing oddly."""
    _login(tmp_home)
    patch_api(messages_page=_page(_message(raw_body="check this out: [bold red]not a tag[/oops")))
    rc = cli.main(["messages", "list", "--room", _GID_TRIP, "--topic", _TID_GENERAL])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[bold red]not a tag[/oops" in out


def test_rooms_list_escapes_a_bracket_shaped_display_name(tmp_home, patch_api, capsys) -> None:
    """A room name is member-settable (a rename); ``[dim]``-shaped text in it
    must render literally, not as markup or a crash."""
    _login(tmp_home)
    patch_api(groups=_group_list(_group(display_name="[dim]Trip[/dim] planning")))
    rc = cli.main(["rooms", "list"])
    assert rc == 0
    assert "[dim]Trip[/dim] planning" in capsys.readouterr().out


def test_messages_list_renders_attachments_reactions_edits_and_deletes(
    tmp_home, patch_api, capsys
) -> None:
    _login(tmp_home)
    patch_api(
        messages_page=_page(
            _message(
                id="m1",
                attachments=[{"id": "att-1", "filename": "notes.txt"}],
                reactions=[{"emoji": "🎉", "count": 2, "mine": True}],
                is_edited=True,
                via_display_name="#general",
                is_bridged=True,
            ),
            _message(id="m2", is_deleted=True, raw_body=""),
        )
    )
    rc = cli.main(["messages", "list", "--room", _GID_TRIP, "--topic", _TID_GENERAL])
    assert rc == 0
    out = capsys.readouterr().out
    assert "notes.txt" in out
    assert "🎉" in out
    assert "edited" in out
    assert "via #general" in out
    assert "deleted" in out


def test_messages_list_hides_the_via_audit_marker_of_an_unbridged_post(
    tmp_home, patch_api, capsys
) -> None:
    _login(tmp_home)
    patch_api(messages_page=_page(_message(id="m1", via_display_name="Pepper")))
    rc = cli.main(["messages", "list", "--room", _GID_TRIP, "--topic", _TID_GENERAL])
    assert rc == 0
    assert "via Pepper" not in capsys.readouterr().out


def test_messages_list_labels_composed_bridged_plain_and_missing_field(
    tmp_home, patch_api, capsys
) -> None:
    _login(tmp_home)
    patch_api(
        messages_page=_page(
            _message(id="m1", display_name="Composed", composed_via=True),
            _message(id="m2", display_name="Bridged", via_display_name="#general", is_bridged=True),
            _message(id="m3", display_name="Plain"),
            _message(
                id="m4",
                display_name="Both",
                via_display_name="#general",
                is_bridged=True,
                composed_via=True,
            ),
        )
    )
    rc = cli.main(["messages", "list", "--room", _GID_TRIP, "--topic", _TID_GENERAL])
    assert rc == 0
    lines = {
        name: line
        for line in capsys.readouterr().out.splitlines()
        for name in ("Composed", "Bridged", "Plain", "Both")
        if name in line
    }
    assert "(via Pepper)" in lines["Composed"]
    assert "(via #general)" in lines["Bridged"]
    assert "(via" not in lines["Plain"]
    assert "(via #general)" in lines["Both"]
    assert "(via Pepper)" not in lines["Both"]


def test_messages_list_help_describes_omitted_topic(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["messages", "list", "--help"])
    assert exc.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "most recently active unarchived topic" in help_text
    assert "most recent topic if all are archived" in help_text
    assert "room's default topic" not in help_text


def test_messages_list_resolves_default_topic_when_omitted(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(
        topics=[
            _topic(topic_id=_TID_ARCHIVED, name="Old", archived_at="2026-01-01T00:00:00Z"),
            _topic(topic_id=_TID_GENERAL, name="General", archived_at=None),
        ],
        messages_page=_page(),
    )
    rc = cli.main(["messages", "list", "--room", _GID_TRIP])
    assert rc == 0
    calls = _FakeGroupsApi.instances[-1].calls
    assert ("list_group_messages", _GID_TRIP, _TID_GENERAL, None, None, None, None) in calls


def test_messages_list_no_topics_errors(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(topics=[])
    rc = cli.main(["messages", "list", "--room", _GID_TRIP])
    assert rc == 2
    assert "no topics" in capsys.readouterr().err


def test_messages_list_prints_next_cursor_when_there_is_more(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(messages_page=_page(_message(), next_before="cursor-42"))
    rc = cli.main(["messages", "list", "--room", _GID_TRIP, "--topic", _TID_GENERAL])
    assert rc == 0
    out = capsys.readouterr().out
    assert "cursor-42" in out
    assert "--before" in out


def test_messages_list_next_cursor_does_not_wrap(tmp_home, patch_api, capsys) -> None:
    """The ``--before`` cursor in the "more" hint is meant to be pasted into
    the next ``read`` invocation verbatim; it must not be folded across
    lines by Rich's default wrapping."""
    _login(tmp_home)
    long_cursor = "cursor-" + "3" * 100
    patch_api(messages_page=_page(_message(), next_before=long_cursor))
    rc = cli.main(["messages", "list", "--room", _GID_TRIP, "--topic", _TID_GENERAL])
    assert rc == 0
    assert long_cursor in capsys.readouterr().out


def test_messages_list_passes_before_cursor(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(messages_page=_page())
    rc = cli.main(
        [
            "messages",
            "list",
            "--room",
            _GID_TRIP,
            "--topic",
            _TID_GENERAL,
            "--before",
            "cursor-1",
            "--limit",
            "5",
        ]
    )
    assert rc == 0
    calls = _FakeGroupsApi.instances[-1].calls
    assert ("list_group_messages", _GID_TRIP, _TID_GENERAL, None, "cursor-1", None, 5) in calls


def test_messages_list_passes_since(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(messages_page=_page())
    rc = cli.main(
        [
            "messages",
            "list",
            "--room",
            _GID_TRIP,
            "--topic",
            _TID_GENERAL,
            "--since",
            "2026-09-16T00:00:00Z",
        ]
    )
    assert rc == 0
    calls = _FakeGroupsApi.instances[-1].calls
    assert (
        "list_group_messages",
        _GID_TRIP,
        _TID_GENERAL,
        "2026-09-16T00:00:00Z",
        None,
        None,
        None,
    ) in calls


def test_messages_list_before_and_since_together_is_a_usage_error(
    tmp_home, patch_api, capsys
) -> None:
    _login(tmp_home)
    patch_api(messages_page=_page())
    argv = ["messages", "list", "--room", _GID_TRIP, "--topic", _TID_GENERAL]
    assert cli.main([*argv, "--before", "c1", "--since", "c2"]) == 2
    assert "cannot be used together" in capsys.readouterr().err
    assert cli.main([*argv, "--before", "c1", "--since", "c2", "--json"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "usage"
    assert _FakeGroupsApi.instances == []


def test_messages_list_json_shape(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(messages_page=_page(_message(), next_before="cursor-1"))
    rc = cli.main(["messages", "list", "--room", _GID_TRIP, "--topic", _TID_GENERAL, "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["next_before"] == "cursor-1"
    assert payload["items"][0]["display_name"] == "Dana"


def test_messages_list_json_carries_the_row_ids(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(
        messages_page=_page(
            _message(
                author_person_id="p-1",
                parent_message_id="m-0",
                mentions=["p-2"],
                mention_names=["Ada"],
            ),
            _message(),
        )
    )
    rc = cli.main(["messages", "list", "--room", _GID_TRIP, "--topic", _TID_GENERAL, "--json"])
    assert rc == 0
    new, old = json.loads(capsys.readouterr().out)["items"]
    assert new["author_person_id"] == "p-1"
    assert new["parent_message_id"] == "m-0"
    assert new["mentions"] == ["p-2"]
    assert old["author_person_id"] is None
    assert old["parent_message_id"] is None
    assert old["mentions"] == []


def test_messages_list_topic_by_name(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(
        topics=[_topic(topic_id=_TID_FLIGHTS, name="Flights")],
        messages_page=_page(),
    )
    rc = cli.main(["messages", "list", "--room", _GID_TRIP, "--topic", "flights"])
    assert rc == 0
    calls = _FakeGroupsApi.instances[-1].calls
    assert ("list_group_messages", _GID_TRIP, _TID_FLIGHTS, None, None, None, None) in calls


def test_messages_list_ambiguous_topic_errors(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(
        topics=[
            _topic(topic_id=_TID_FLIGHTS, name="Flights east"),
            _topic(topic_id=_TID_GENERAL, name="Flights west"),
        ]
    )
    rc = cli.main(["messages", "list", "--room", _GID_TRIP, "--topic", "flights"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "more than one topic" in err


def test_messages_list_unknown_topic_lists_choices(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(topics=[_topic(topic_id=_TID_FLIGHTS, name="Flights")])
    rc = cli.main(["messages", "list", "--room", _GID_TRIP, "--topic", "nope"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "no topic named" in err
    assert "Flights" in err


def test_messages_list_404_renders_clean_error(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(topics=api_client.ApiError(status=404, body="", detail="No such group."))
    rc = cli.main(["messages", "list", "--room", _GID_TRIP])
    assert rc == 2
    assert "not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# post


def test_messages_post_joins_message_words(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(posted={"id": _MID})
    rc = cli.main(
        ["messages", "post", "--room", _GID_TRIP, "--topic", _TID_GENERAL, "hello", "room"]
    )
    assert rc == 0
    call = _FakeGroupsApi.instances[-1].calls[-1]
    assert call[0] == "post_group_message"
    assert call[3] == "hello room"
    out = capsys.readouterr().out
    assert _MID in out


def _posted_message(**over) -> dict:
    import dataclasses

    return {**dataclasses.asdict(_message(**over)), "is_you": True}


def test_messages_post_json_prints_the_created_message_as_groups_read_shapes_it(
    tmp_home, patch_api, capsys
) -> None:
    import dataclasses
    import json

    _login(tmp_home)
    parent = "00000000-0000-0000-0000-0000000000c2"
    patch_api(posted=_posted_message(parent_message_id=parent))
    rc = cli.main(
        [
            "messages",
            "post",
            "--room",
            _GID_TRIP,
            "--topic",
            _TID_GENERAL,
            "hello",
            "--reply-to",
            parent,
            "--json",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out["id"] == _MID
    assert out["created_at"] == "2026-09-16T17:30:00+00:00"
    assert out["topic_id"] == _TID_GENERAL
    assert out["parent_message_id"] == parent
    # Every key a message has in `messages list --json`, plus topic_id and nothing else.
    read_keys = set(dataclasses.asdict(_message()))
    assert set(out) == read_keys | {"topic_id"}
    assert "Posted." not in captured.out + captured.err


def test_messages_post_json_parent_is_null_for_a_top_level_post(
    tmp_home, patch_api, capsys
) -> None:
    import json

    _login(tmp_home)
    patch_api(posted=_posted_message())
    assert (
        cli.main(["messages", "post", "--room", _GID_TRIP, "--topic", _TID_GENERAL, "hi", "--json"])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["parent_message_id"] is None


def test_messages_post_without_json_is_unchanged(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(posted=_posted_message())
    assert cli.main(["messages", "post", "--room", _GID_TRIP, "--topic", _TID_GENERAL, "hi"]) == 0
    captured = capsys.readouterr()
    assert captured.out == f"{_MID}\n"
    assert "Posted." in captured.err


def test_messages_post_reads_stdin_with_dash(tmp_home, patch_api, monkeypatch) -> None:
    _login(tmp_home)
    patch_api(posted={"id": _MID})
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("piped message body\n"))
    rc = cli.main(["messages", "post", "--room", _GID_TRIP, "--topic", _TID_GENERAL, "-"])
    assert rc == 0
    call = _FakeGroupsApi.instances[-1].calls[-1]
    assert call[3] == "piped message body"


def test_messages_post_empty_message_errors_without_any_call(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(posted={"id": _MID})
    rc = cli.main(["messages", "post", "--room", _GID_TRIP, "--topic", _TID_GENERAL])
    assert rc == 2
    assert _FakeGroupsApi.instances == []


def test_messages_post_uploads_attachments_before_posting(
    tmp_home, patch_api, tmp_path, capsys
) -> None:
    _login(tmp_home)
    attachment = tmp_path / "notes.txt"
    attachment.write_text("hello attachment")
    patch_api(uploaded={"id": "att-1", "filename": "notes.txt"}, posted={"id": _MID})
    rc = cli.main(
        [
            "messages",
            "post",
            "--room",
            _GID_TRIP,
            "--topic",
            _TID_GENERAL,
            "see",
            "attached",
            "--attach",
            str(attachment),
        ]
    )
    assert rc == 0
    calls = _FakeGroupsApi.instances[-1].calls
    upload_call = next(c for c in calls if c[0] == "upload_group_attachment")
    post_call = next(c for c in calls if c[0] == "post_group_message")
    # attachment uploaded before the message is posted, and its id rides the post
    assert calls.index(upload_call) < calls.index(post_call)
    assert post_call[4] == ["att-1"]


def test_messages_post_missing_attachment_errors_before_any_network_call(
    tmp_home, patch_api
) -> None:
    _login(tmp_home)
    patch_api(posted={"id": _MID})
    rc = cli.main(
        [
            "messages",
            "post",
            "--room",
            _GID_TRIP,
            "--topic",
            _TID_GENERAL,
            "hi",
            "--attach",
            "/no/such/file-xyz.txt",
        ]
    )
    assert rc == 2
    # Every --attach path is read and validated before anything is uploaded
    # or an ApiClient is even constructed, so a missing file never leaves an
    # earlier attachment orphaned in the groups service.
    assert _FakeGroupsApi.instances == []


def test_messages_post_a_later_bad_attachment_orphans_nothing(
    tmp_home, patch_api, tmp_path
) -> None:
    """A regression test for exactly the failure the ordering above guards
    against: with a *valid* first attachment and an unreadable second one,
    nothing gets uploaded — not even the first — because every path is read
    before any upload starts."""
    _login(tmp_home)
    good = tmp_path / "notes.txt"
    good.write_text("hello attachment")
    patch_api(uploaded={"id": "att-1", "filename": "notes.txt"}, posted={"id": _MID})
    rc = cli.main(
        [
            "messages",
            "post",
            "--room",
            _GID_TRIP,
            "--topic",
            _TID_GENERAL,
            "hi",
            "--attach",
            str(good),
            "--attach",
            "/no/such/file-xyz.txt",
        ]
    )
    assert rc == 2
    assert _FakeGroupsApi.instances == []


def test_messages_post_generates_a_fresh_idempotency_key_per_invocation(
    tmp_home, patch_api
) -> None:
    _login(tmp_home)
    patch_api(posted={"id": _MID})
    cli.main(["messages", "post", "--room", _GID_TRIP, "--topic", _TID_GENERAL, "one"])
    key1 = _FakeGroupsApi.instances[-1].calls[-1][5]
    cli.main(["messages", "post", "--room", _GID_TRIP, "--topic", _TID_GENERAL, "two"])
    key2 = _FakeGroupsApi.instances[-1].calls[-1][5]
    assert key1 and key2
    assert key1 != key2


def test_messages_post_passes_reply_to_mentions_and_idempotency_key(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(posted={"id": _MID})
    rc = cli.main(
        [
            "messages",
            "post",
            "--room",
            _GID_TRIP,
            "--topic",
            _TID_GENERAL,
            "@Sam see above",
            "--reply-to",
            "msg-parent",
            "--mention",
            "person-sam",
            "--mention",
            "person-lee",
            "--idempotency-key",
            "answer-msg-parent",
        ]
    )
    assert rc == 0
    call = _FakeGroupsApi.instances[-1].calls[-1]
    assert call[0] == "post_group_message"
    assert call[5] == "answer-msg-parent"
    assert call[6] == ["person-sam", "person-lee"]
    assert call[7] == "msg-parent"


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--idempotency-key", ""),
        ("--idempotency-key", "  "),
        ("--reply-to", ""),
        ("--mention", ""),
        ("--on-behalf-of", ""),
    ],
)
def test_messages_post_refuses_an_empty_flag_value(tmp_home, patch_api, flag, value) -> None:
    _login(tmp_home)
    patch_api(posted={"id": _MID})
    rc = cli.main(
        ["messages", "post", "--room", _GID_TRIP, "--topic", _TID_GENERAL, "hi", flag, value]
    )
    assert rc == 2
    assert _FakeGroupsApi.instances == []


def test_messages_post_rejects_an_empty_key_before_reading_stdin(
    tmp_home, patch_api, monkeypatch
) -> None:
    class _NoStdin:
        def read(self) -> str:
            raise AssertionError("stdin must not be read when a flag is invalid")

    _login(tmp_home)
    patch_api(posted={"id": _MID})
    monkeypatch.setattr("sys.stdin", _NoStdin())
    rc = cli.main(
        [
            "messages",
            "post",
            "--room",
            _GID_TRIP,
            "--topic",
            _TID_GENERAL,
            "-",
            "--idempotency-key",
            "",
        ]
    )
    assert rc == 2
    assert _FakeGroupsApi.instances == []


def test_messages_post_without_threading_flags_sends_none(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(posted={"id": _MID})
    cli.main(["messages", "post", "--room", _GID_TRIP, "--topic", _TID_GENERAL, "hi"])
    call = _FakeGroupsApi.instances[-1].calls[-1]
    assert call[6] is None
    assert call[7] is None
    assert call[8] is None


def test_messages_post_passes_on_behalf_of(tmp_home, patch_api) -> None:
    """An agent member answering someone names them; the server decides
    whether the caller may (only an agent token can)."""
    _login(tmp_home)
    patch_api(posted={"id": _MID})
    rc = cli.main(
        [
            "messages",
            "post",
            "--room",
            _GID_TRIP,
            "--topic",
            _TID_GENERAL,
            "done",
            "--reply-to",
            "msg-parent",
            "--on-behalf-of",
            "person-sam",
        ]
    )
    assert rc == 0
    assert _FakeGroupsApi.instances[-1].calls[-1][8] == "person-sam"


def test_messages_post_refuses_on_behalf_of_without_a_reply(tmp_home, patch_api) -> None:
    """The requester must be the author of the parent message, so a
    bare ``--on-behalf-of`` is a post the service must reject — refused
    locally before anything is sent."""
    _login(tmp_home)
    patch_api(posted={"id": _MID})
    rc = cli.main(
        [
            "messages",
            "post",
            "--room",
            _GID_TRIP,
            "--topic",
            _TID_GENERAL,
            "done",
            "--on-behalf-of",
            "person-sam",
        ]
    )
    assert rc == 2
    assert _FakeGroupsApi.instances == []


def test_messages_post_prints_the_created_message_id(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(posted={"id": _MID})
    rc = cli.main(["messages", "post", "--room", _GID_TRIP, "--topic", _TID_GENERAL, "hi"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == _MID


def test_messages_post_without_credentials_errors(tmp_home, patch_api) -> None:
    patch_api(posted={"id": _MID})
    rc = cli.main(["messages", "post", "--room", _GID_TRIP, "--topic", _TID_GENERAL, "hi"])
    assert rc == 1


def test_messages_post_unreachable_server(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(posted=httpx.ConnectError("boom"))
    rc = cli.main(["messages", "post", "--room", _GID_TRIP, "--topic", _TID_GENERAL, "hi"])
    assert rc == 1
    assert "Couldn't reach" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# members list


def _roster():
    return [
        api_client.GroupRosterMember(person_id="pepper-id", display_name="Pepper", is_pepper=True),
        api_client.GroupRosterMember(person_id="person-sam", display_name="Sam"),
    ]


def test_members_list_renders_names_and_ids(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(roster=_roster())
    rc = cli.main(["members", "list", "--room", _GID_TRIP])
    assert rc == 0
    assert ("group_roster", _GID_TRIP) in _FakeGroupsApi.instances[-1].calls
    out = capsys.readouterr().out
    assert "Sam" in out
    assert "person-sam" in out
    assert "Pepper" in out


def test_members_list_marks_an_agent(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(
        roster=[
            api_client.GroupRosterMember(person_id="p-agent", display_name="Watcher", is_agent=True)
        ]
    )
    assert cli.main(["members", "list", "--room", _GID_TRIP]) == 0
    assert "Watcher (agent)" in capsys.readouterr().out


def test_members_list_json_is_the_envelope(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(roster=_roster())
    assert cli.main(["members", "list", "--room", _GID_TRIP, "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["next_before"] is None
    assert body["items"][0]["is_pepper"] is True
    assert body["items"][1] == {
        "person_id": "person-sam",
        "display_name": "Sam",
        "picture_url": None,
        "is_pepper": False,
        "is_agent": False,
        "agent_profile": None,
    }


def test_members_list_json_prints_an_agents_profile(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(
        roster=[
            api_client.GroupRosterMember(
                person_id="p-agent",
                display_name="Watcher",
                is_agent=True,
                agent_profile="investigate",
            ),
            api_client.GroupRosterMember(person_id="person-sam", display_name="Sam"),
        ]
    )
    assert cli.main(["members", "list", "--room", _GID_TRIP, "--json"]) == 0
    items = json.loads(capsys.readouterr().out)["items"]
    assert items[0]["agent_profile"] == "investigate"
    assert items[1]["agent_profile"] is None


def test_members_list_empty(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(roster=[])
    assert cli.main(["members", "list", "--room", _GID_TRIP]) == 0
    assert "(no members)" in capsys.readouterr().out


def test_members_list_without_credentials_errors(tmp_home, patch_api) -> None:
    patch_api(roster=_roster())
    assert cli.main(["members", "list", "--room", _GID_TRIP]) == 1


def test_members_list_unreachable_server(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(roster=httpx.ConnectError("boom"))
    assert cli.main(["members", "list", "--room", _GID_TRIP]) == 1
    assert "Couldn't reach" in capsys.readouterr().err


def test_members_list_api_error_is_reported(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(roster=api_client.ApiError(status=403, body="", detail="Not a member."))
    assert cli.main(["members", "list", "--room", _GID_TRIP]) != 0
    assert capsys.readouterr().err.strip()
