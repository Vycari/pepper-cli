"""The rest of the typed parsers (todos, groups, artifacts, notes, skills, chat send, sign-in)
turn a malformed successful response into ``ApiError`` (status 200) naming the field or shape,
never a ``KeyError``/``TypeError``, and never echo the offending value. Lists a poller reads on
a timer skip and count a bad row; every other list fails whole."""

from __future__ import annotations

import httpx
import pytest

from pepper_cli import api_client
from pepper_cli.api_client import ApiError
from pepper_cli.secret import SecretStr


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
    assert (listed.groups[0].topics, listed.skipped, listed.features) == ([], 0, ())
    with _api({"truncated": False}) as api:
        assert api.list_artifacts().items == []


# --- polled lists skip and count a bad row; the others fail whole -----------------------------


def test_the_group_list_skips_and_counts_a_malformed_room() -> None:
    body = _groups(groups=[_group(), _group(group_id=None), "x", _group(group_id="g3")])
    with _api(body) as api:
        listed = api.list_groups()
    assert [g.group_id for g in listed.groups] == ["g1", "g3"]
    assert listed.skipped == 2


def test_a_room_with_a_malformed_topic_is_skipped_not_the_whole_list() -> None:
    body = _groups(groups=[_group(topics=[{"topic_id": "x"}]), _group(group_id="g2")])
    with _api(body) as api:
        listed = api.list_groups()
    assert [g.group_id for g in listed.groups] == ["g2"]
    assert listed.skipped == 1


def test_group_messages_skip_and_count_a_malformed_row() -> None:
    body = {"messages": [_message(), _message(id=None), 4, _message(id="m3")]}
    with _api(body) as api:
        page = api.list_group_messages("g", "tp")
    assert [m.id for m in page.messages] == ["s", "m3"]
    assert page.skipped == 2


@pytest.mark.parametrize("bad", [{"messages": "x"}, []])
def test_group_messages_that_are_not_a_list_fail_the_page(bad: object) -> None:
    with _api(bad) as api, pytest.raises(ApiError):
        api.list_group_messages("g", "tp")


def test_a_group_message_with_a_malformed_card_is_a_bad_row() -> None:
    body = {"messages": [_message(cards=[{"app_key": "k"}]), _message(id="ok")]}
    with _api(body) as api:
        page = api.list_group_messages("g", "tp")
    assert [m.id for m in page.messages] == ["ok"]
    assert page.skipped == 1


def test_artifacts_skip_and_count_a_malformed_item() -> None:
    good = {"id": "a", "name": "n", "kind": "k", "updated_at": "u", "etag": "e"}
    with _api({"truncated": False, "items": [good, {"id": "b"}, good]}) as api:
        listed = api.list_artifacts()
    assert (len(listed.items), listed.skipped) == (2, 1)


def test_todos_skip_only_when_the_caller_polls() -> None:
    rows = [_todo(), _without(_todo(id="t2"), "status"), _todo(id="t3")]
    with _api(rows) as api:
        polled = api.list_todos(skip_bad_rows=True)
    assert ([t.id for t in polled], polled.skipped) == (["t1", "t3"], 1)
    with _api(rows) as api, pytest.raises(ApiError):
        api.list_todos()


def test_a_todo_list_that_is_not_a_list_fails_even_when_skipping() -> None:
    with _api({"todos": []}) as api, pytest.raises(ApiError):
        api.list_todos(skip_bad_rows=True)


def test_room_todos_skip_non_objects_but_a_malformed_todo_fails_the_read() -> None:
    body = {
        "rooms": [{"name": "no id", "items": []}, "junk", {"group_id": "g", "items": [_todo(), 1]}]
    }
    with _api(body) as api:
        listed = api.list_room_todos()
    assert [(r.group_id, len(r.items)) for r in listed.rooms] == [("g", 1)]
    assert listed.skipped == 3
    bad = {"rooms": [{"group_id": "g", "items": [{"id": "x"}]}]}
    with _api(bad) as api, pytest.raises(ApiError) as raised:
        api.list_room_todos()
    assert "'title'" in _malformed(raised)
    with _api({"rooms": "x"}) as api, pytest.raises(ApiError) as raised:
        api.list_room_todos()
    assert "'rooms'" in _malformed(raised)


# --- sign-in ----------------------------------------------------------------------------------


def _device(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_device_start_names_a_missing_or_mistyped_field() -> None:
    body = {
        "device_code": "d",
        "user_code": "u",
        "verification_url": "v",
        "expires_in": 60,
        "interval": 5,
    }
    ok = api_client.device_start(
        "http://x", "n", client=_device(lambda _r: httpx.Response(200, json=body))
    )
    assert (ok.user_code, ok.interval) == ("u", 5)
    for bad, name in (
        (_without(body, "interval"), "'interval'"),
        ({**body, "expires_in": "60"}, "'expires_in'"),
    ):
        with pytest.raises(ApiError) as raised:
            api_client.device_start(
                "http://x", "n", client=_device(lambda _r, b=bad: httpx.Response(200, json=b))
            )
        assert name in _malformed(raised)


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
