"""The rest of the typed parsers (todos, groups, artifacts, notes, skills, chat send, sign-in)
turn a malformed successful response into ``ApiError`` (status 200) naming the field or shape,
never a ``KeyError``/``TypeError``, and never echo the offending value. A bad row fails the
whole list, so nothing is silently left out."""

from __future__ import annotations

import httpx
import pytest

from pepper_cli import api_client, cli
from pepper_cli.api_client import ApiError
from pepper_cli.secret import SecretStr
from pepper_cli.tui import app as tui_app
from pepper_cli.tui import poller
from tests import tui_support as ts


def _api(body: object) -> api_client.ApiClient:
    return api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=httpx.Client(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=body))
        ),
    )


def _todo(**over: object) -> dict:
    row = {"id": "t1", "title": "x", "status": "open", "source": "user", "created_at": "z"}
    return {**row, **over}


def _topic(**over: object) -> dict:
    return {"topic_id": "tp", "name": "General", "last_activity_at": None, **over}


def _group(**over: object) -> dict:
    row = {
        "group_id": "g1",
        "display_name": "G",
        "color": "c",
        "sub": "s",
        "member_count": 2,
        "archived": False,
        "topics": [_topic()],
    }
    return {**row, **over}


def _groups(**over: object) -> dict:
    body = {"enabled": True, "degraded": False, "total": 1, "archived_total": 0}
    return {**body, "groups": [_group()], **over}


def _message(**over: object) -> dict:
    row = {k: "s" for k in api_client.GroupMessage.__dataclass_fields__ if k in _MSG_STR}
    row |= dict.fromkeys(_MSG_BOOL, False)
    return {**row, **over}


_MSG_STR = [
    "id",
    "display_name",
    "initial",
    "avatar_tint",
    "raw_body",
    "rendered_html",
    "created_at",
    "created_at_display",
    "day_key",
    "day_kicker",
    "day_heading",
    "day_label",
    "quote_excerpt",
]
_MSG_BOOL = ["is_pepper", "is_you", "is_deleted", "is_broken", "run_start"]


def _without(row: dict, key: str) -> dict:
    return {k: v for k, v in row.items() if k != key}


def _malformed(raised: pytest.ExceptionInfo[ApiError]) -> str:
    assert raised.value.status == 200
    assert len(raised.value.body) <= 200
    return str(raised.value.detail)


# --- a missing key, a wrong container type and a wrong scalar type, per parser ----------------


@pytest.mark.parametrize(
    ("call", "body", "names"),
    [
        (lambda a: a.list_todos(), [_without(_todo(), "title")], "'title'"),
        (lambda a: a.list_todos(), [_todo(id=7)], "'id'"),
        (lambda a: a.list_todos(), {"items": []}, "not a list"),
        (lambda a: a.list_todos(), ["row"], "not an object"),
        (lambda a: a.list_skills(), [{"name": "n"}], "'description'"),
        (lambda a: a.list_skills(), [{"name": 3, "description": "d"}], "'name'"),
        (lambda a: a.list_skills(), "nope", "not a list"),
        (lambda a: a.list_room_skills("g"), [{"id": "1", "name": "n"}], "'description'"),
        (lambda a: a.get_skill("n"), {"name": "n", "description": "d", "body": 1}, "'body'"),
        (
            lambda a: a.get_skill("n"),
            {"name": "n", "description": "d", "body": "b", "toolsets": "x"},
            "'toolsets'",
        ),
        (lambda a: a.list_groups(), _groups(groups="x"), "'groups'"),
        (lambda a: a.list_groups(), _without(_groups(), "total"), "'total'"),
        (lambda a: a.list_groups(), _groups(enabled="yes"), "'enabled'"),
        (lambda a: a.list_groups(), [], "not an object"),
        (
            lambda a: a.list_group_topics("g"),
            {"topics": [_without(_topic(), "name")]},
            "'name'",
        ),
        (lambda a: a.list_group_topics("g"), {"topics": 1}, "'topics'"),
        (lambda a: a.group_roster("g"), {"members": [{"person_id": "p"}]}, "'display_name'"),
        (lambda a: a.group_roster("g"), {"members": {"a": 1}}, "'members'"),
        (
            lambda a: a.group_app_launcher("g"),
            {"tiles": [{"app_key": "k", "slug": "s", "title": "t"}]},
            "'principal'",
        ),
        (lambda a: a.list_artifacts(), {"items": []}, "'truncated'"),
        (lambda a: a.list_artifacts(), {"truncated": False, "items": 3}, "'items'"),
        (
            lambda a: a.list_artifacts(),
            {"truncated": "no", "items": []},
            "'truncated'",
        ),
        (lambda a: a.get_artifact_detail("a"), {"id": "a", "name": "n"}, "'kind'"),
        (lambda a: a.get_artifact_detail("a"), [], "not an object"),
        (
            lambda a: a.get_artifact_detail("a"),
            {"id": "a", "name": "n", "kind": "k", "updated_at": "u", "etag": "e", "labels": 5},
            "'labels'",
        ),
        (
            lambda a: a.list_note_comments("a"),
            {"threads": [{"replies": []}]},
            "'message'",
        ),
        (lambda a: a.list_note_comments("a"), {"threads": "x"}, "'threads'"),
        (lambda a: a.list_note_comments("a"), {"threads": [{"message": []}]}, "'message'"),
        (lambda a: a.chat_send("hi"), {"reply": "r"}, "'user_message_id'"),
        (
            lambda a: a.chat_send("hi"),
            {"reply": 1, "user_message_id": "u", "assistant_message_id": "a"},
            "'reply'",
        ),
        (lambda a: a.whoami(), {"user_id": "u"}, "'display_name'"),
        (lambda a: a.whoami(), {"user_id": 3, "display_name": "d"}, "'user_id'"),
        (
            lambda a: a.whoami(),
            {"user_id": "u", "display_name": "d", "merged_from": "x"},
            "'merged_from'",
        ),
        (
            lambda a: a.trajectory_list(),
            [{"id": "t", "channel": "c", "created_at": "z"}],
            "'event_count'",
        ),
    ],
)
def test_malformed_response_names_the_shape_or_field(call, body, names) -> None:
    with _api(body) as api, pytest.raises(ApiError) as raised:
        call(api)
    assert names in _malformed(raised)


def test_the_diagnostic_never_echoes_the_value() -> None:
    with _api([_todo(title=None, secret="hunter2-sentinel")]) as api, pytest.raises(ApiError) as r:
        api.list_todos()
    assert "hunter2-sentinel" not in str(r.value.detail)
    assert "'title'" in str(r.value.detail)


def test_a_non_object_raw_helper_response_is_malformed_not_a_crash() -> None:
    with _api([1]) as api, pytest.raises(ApiError) as raised:
        api.get_app_data("slug")
    assert "not an object" in _malformed(raised)


def test_a_reply_missing_the_sub_object_field_is_malformed() -> None:
    with _api({"id": "a"}) as api, pytest.raises(ApiError) as raised:
        api.edit_artifact("a", body="b")
    assert "'artifact'" in _malformed(raised)


def test_a_group_message_response_without_the_message_is_malformed() -> None:
    with _api({}) as api, pytest.raises(ApiError) as raised:
        api.post_group_message("g", "tp", message="x")
    assert "'message'" in _malformed(raised)


# --- compatibility: optional fields an older server leaves out keep their defaults ------------


def test_older_server_rows_keep_their_defaults() -> None:
    with _api([_todo(future_field=1)]) as api:
        (todo,) = api.list_todos()
    assert (todo.id, todo.details, todo.assignee, todo.next_action) == ("t1", None, None, None)
    with _api({"name": "n", "description": "d", "body": "b"}) as api:
        skill = api.get_skill("n")
    assert (skill.toolsets, skill.builtin, skill.model) == ((), False, None)
    with _api({"user_id": "u", "display_name": "d"}) as api:
        who = api.whoami()
    assert (who.merged_from, who.date_format, who.time_format, who.person_id) == (
        [],
        "mdy",
        "12h",
        None,
    )
    with _api(_groups(groups=[_group(topics=None, future=1)])) as api:
        listed = api.list_groups()
    assert (listed.groups[0].topics, listed.features) == ([], ())
    with _api({"truncated": False}) as api:
        assert api.list_artifacts().items == []


# --- every list fails whole on a bad row: nothing is silently left out ------------------------


@pytest.mark.parametrize(
    "rooms",
    [
        [_group(), _group(group_id=None)],
        [_group(), "x"],
        [_group(topics=[{"topic_id": "x"}]), _group(group_id="g2")],
    ],
)
def test_a_malformed_room_fails_the_group_list(rooms: list) -> None:
    with _api(_groups(groups=rooms)) as api, pytest.raises(ApiError) as raised:
        api.list_groups()
    _malformed(raised)


@pytest.mark.parametrize(
    "messages", [[_message(), _message(id=None)], [_message(), 4], [_message(cards=[{"a": 1}])]]
)
def test_a_malformed_group_message_fails_the_page(messages: list) -> None:
    with _api({"messages": messages}) as api, pytest.raises(ApiError) as raised:
        api.list_group_messages("g", "tp", at="m1")
    _malformed(raised)
    # A refresh that fails this way is retried (200 is not a permanent refusal), so a stale
    # row is never left in place as if it had been read again.
    assert raised.value.status not in tui_app._PUBLISH_PERMANENT


@pytest.mark.parametrize("bad", [{"messages": "x"}, []])
def test_group_messages_that_are_not_a_list_fail_the_page(bad: object) -> None:
    with _api(bad) as api, pytest.raises(ApiError):
        api.list_group_messages("g", "tp")


def test_a_malformed_artifact_fails_the_list() -> None:
    good = {"id": "a", "name": "n", "kind": "k", "updated_at": "u", "etag": "e"}
    body = {"truncated": False, "items": [good, {"id": "b"}]}
    with _api(body) as api, pytest.raises(ApiError) as raised:
        api.list_artifacts()
    _malformed(raised)


def test_a_malformed_todo_fails_the_list_and_the_pane_read() -> None:
    rows = [_todo(), _todo(id="t2", title=None)]
    with _api(rows) as api, pytest.raises(ApiError):
        api.list_todos()
    with _api(rows) as api, pytest.raises(ApiError):
        poller.fetch_todos(api, None, mine=True)
    with _api({"todos": []}) as api, pytest.raises(ApiError):
        api.list_todos()


def test_room_todos_leave_out_non_objects_but_a_malformed_todo_fails_the_read() -> None:
    body = {
        "rooms": [{"name": "no id", "items": []}, "junk", {"group_id": "g", "items": [_todo(), 1]}]
    }
    with _api(body) as api:
        listed = api.list_room_todos()
    assert [(r.group_id, len(r.items)) for r in listed.rooms] == [("g", 1)]
    bad = {"rooms": [{"group_id": "g", "items": [{"id": "x"}]}]}
    with _api(bad) as api, pytest.raises(ApiError) as raised:
        api.list_room_todos()
    assert "'title'" in _malformed(raised)
    with _api({"rooms": "x"}) as api, pytest.raises(ApiError) as raised:
        api.list_room_todos()
    assert "'rooms'" in _malformed(raised)


def test_a_malformed_same_named_note_fails_the_lookup_rather_than_resolving_the_other() -> None:
    good = {"id": "A", "name": "Runbook", "kind": "note", "updated_at": "u", "etag": "e"}
    bad = {"id": "B", "name": "Runbook", "kind": "note", "updated_at": "u"}
    with _api({"truncated": False, "items": [good, bad]}) as api, pytest.raises(ApiError):
        cli._resolve_note(api, room_principal=None, value="Runbook")


def test_the_rail_keeps_its_etag_when_a_room_is_malformed() -> None:
    server = ts.standard_server()
    with server.client() as api:
        rail = poller.RailPoller(api)
        first = rail.step()
        assert first.groups is not None
        rail.commit(first)
        kept = first.etag
        assert len(first.groups.groups) == 2
        good_topic = dict(server.rooms[1]["topics"][0])
        server.rooms[1]["topics"][0] = {"topic_id": "x"}  # no name
        server.bump()
        with pytest.raises(ApiError):
            rail.step()
        assert rail._etag == kept  # nothing committed: the partial list is never adopted
        server.rooms[1]["topics"][0] = good_topic
        server.bump()
        again = rail.step()
    assert again.groups is not None
    assert len(again.groups.groups) == 2


def test_a_malformed_device_poll_never_carries_the_token_in_the_error() -> None:
    body = {"status": "approved", "token": "pep_pat_sentinel"}
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=body)))
    with pytest.raises(ApiError) as raised:
        api_client.device_poll("http://example.com", SecretStr("dc"), client=client)
    assert "pep_pat_sentinel" not in raised.value.body
    assert "pep_pat_sentinel" not in str(raised.value)
    assert "pep_pat_sentinel" not in repr(raised.value)


# --- an optional scalar keeps its default when absent, and fails typed when wrong ------------


@pytest.mark.parametrize("bad", [{"bad": 1}, True, "3", 1.5])
def test_a_wrong_typed_resolved_count_fails_the_comment_read(bad: object) -> None:
    with _api({"threads": [], "resolved_count": bad}) as api, pytest.raises(ApiError) as raised:
        api.list_note_comments("a")
    assert "'resolved_count'" in _malformed(raised)


def test_an_absent_resolved_count_keeps_its_default() -> None:
    for body in ({"threads": []}, {"threads": [], "resolved_count": None}):
        with _api(body) as api:
            assert api.list_note_comments("a").resolved_count == 0
    with _api({"threads": [], "resolved_count": 2}) as api:
        assert api.list_note_comments("a").resolved_count == 2


@pytest.mark.parametrize(
    ("message", "names"),
    [
        ({"id": "c", "body": {"x": 1}}, "'body'"),
        ({"id": "c", "created_at": 5}, "'created_at'"),
        ({"id": "c", "author": {"display_name": ["x"]}}, "'display_name'"),
    ],
)
def test_a_wrong_typed_comment_field_fails_the_read(message: dict, names: str) -> None:
    with _api({"threads": [{"message": message}]}) as api, pytest.raises(ApiError) as raised:
        api.list_note_comments("a")
    assert names in _malformed(raised)


def test_a_wrong_typed_blog_card_or_room_todos_name_fails_the_read() -> None:
    with (
        _api({"post_id": "p", "already": False, "card": 1}) as api,
        pytest.raises(ApiError) as raised,
    ):
        api.blog_withdraw("g", "p")
    assert "'card'" in _malformed(raised)
    with (
        _api({"rooms": [{"group_id": "g", "name": 3, "items": []}]}) as api,
        pytest.raises(ApiError) as raised,
    ):
        api.list_room_todos()
    assert "'name'" in _malformed(raised)


# --- sign-in ----------------------------------------------------------------------------------


def _device(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_device_poll_approved_without_a_token_or_user_is_malformed() -> None:
    user = {"user_id": "u", "display_name": "d"}
    for body, name in (
        ({"status": "approved", "user": user}, "'token'"),
        ({"status": "approved", "token": "t"}, "'user'"),
        ({"status": "approved", "token": "t", "user": {"user_id": "u"}}, "'display_name'"),
        ([], "not an object"),
    ):
        with pytest.raises(ApiError) as raised:
            api_client.device_poll(
                "http://x",
                SecretStr("dc"),
                client=_device(lambda _r, b=body: httpx.Response(200, json=b)),
            )
        assert name in _malformed(raised)
    poll = api_client.device_poll(
        "http://x",
        SecretStr("dc"),
        client=_device(
            lambda _r: httpx.Response(200, json={"status": "approved", "token": "t", "user": user})
        ),
    )
    assert poll.user is not None and poll.user.user_id == "u"
