"""Tests for the ``ApiClient`` groups methods.

Same ``httpx.MockTransport`` style as the other ``test_api_client_*`` modules:
assert the wire shape (method, URL, query, body, headers) and that responses
parse into the dataclasses. This module is the contract the native clients
reproduce, so what is pinned here is deliberately the *shape* rather than any
server behaviour — the server's own behaviour is covered in
``tests/api/test_groups.py``.
"""

from __future__ import annotations

import httpx

from pepper_cli import api_client
from pepper_cli.secret import SecretStr

_GID = "00000000-0000-0000-0000-0000000000a1"
_TID = "00000000-0000-0000-0000-0000000000b2"
_MID = "00000000-0000-0000-0000-0000000000c3"


def _mock(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _client(handler) -> api_client.ApiClient:
    return api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=_mock(handler),
    )


def _message_json(**over) -> dict:
    base = {
        "id": _MID,
        "is_pepper": False,
        "is_you": True,
        "is_deleted": False,
        "is_broken": False,
        "run_start": True,
        "display_name": "You",
        "initial": "Y",
        "avatar_tint": "t1",
        "picture_url": None,
        "raw_body": "**hi**",
        "rendered_html": "<p><strong>hi</strong></p>",
        "created_at": "2026-09-16T17:30:00+00:00",
        "created_at_display": "5:30 PM",
        "day_key": "2026-09-16",
        "day_kicker": "WEDNESDAY",
        "day_heading": "September 16, 2026",
        "day_label": "Wednesday, September 16",
        "via_display_name": None,
        "is_edited": False,
        "edited_at": None,
        "deleted_at": None,
        "reactions": [{"emoji": "🎉", "count": 1, "mine": True, "reactors": "You"}],
        "link_previews": [],
        "attachments": [],
        "mention_names": [],
        "quote": None,
        "quote_excerpt": "hi",
        "cards": [],
        "is_permalink_target": False,
    }
    base.update(over)
    return base


def test_list_groups_parses_the_capability_signal() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        return httpx.Response(
            200,
            json={
                "enabled": True,
                "degraded": False,
                "total": 1,
                "archived_total": 2,
                "groups": [
                    {
                        "group_id": _GID,
                        "display_name": "Trip planning",
                        "color": "#ffaa00",
                        "sub": "active 2h ago",
                        "member_count": 3,
                        "archived": False,
                        "topics": [
                            {
                                "topic_id": _TID,
                                "name": "Flights",
                                "last_activity_at": "2026-09-16T17:30:00+00:00",
                                "dedicated_conversation": False,
                                "archived_at": None,
                                "imported_history": False,
                            }
                        ],
                    }
                ],
            },
        )

    with _client(handler) as api:
        result = api.list_groups()

    assert seen["url"] == "http://example.com/api/v1/groups?archived=0"
    assert seen["auth"] == "Bearer pep_pat_abc"
    assert result.enabled is True
    assert result.degraded is False
    assert result.archived_total == 2
    (room,) = result.groups
    assert room.display_name == "Trip planning"
    assert room.member_count == 3
    assert [t.name for t in room.topics] == ["Flights"]


def test_list_groups_asks_for_the_archived_fold() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["archived"] == "1"
        return httpx.Response(
            200,
            json={
                "enabled": True,
                "degraded": False,
                "total": 0,
                "archived_total": 0,
                "groups": [],
            },
        )

    with _client(handler) as api:
        assert api.list_groups(archived=True).groups == []


def test_a_disabled_deployment_parses_rather_than_erroring() -> None:
    """``enabled: false`` is an answer, not a failure — it is how a client
    decides whether to offer the surface at all."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "enabled": False,
                "degraded": False,
                "total": 0,
                "archived_total": 0,
                "groups": [],
            },
        )

    with _client(handler) as api:
        assert api.list_groups().enabled is False


def test_list_group_messages_parses_a_full_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/v1/groups/{_GID}/topics/{_TID}/messages"
        return httpx.Response(
            200,
            json={
                "messages": [_message_json()],
                "next_before": "cursor-1",
                "permalink_target_missing": False,
            },
        )

    with _client(handler) as api:
        page = api.list_group_messages(_GID, _TID)

    assert page.next_before == "cursor-1"
    (row,) = page.messages
    # The two fields a native client actually renders from, per contract
    # rules 2 and 3: the markdown source, and the preformatted display string.
    assert row.raw_body == "**hi**"
    assert row.created_at_display == "5:30 PM"
    assert row.reactions[0]["mine"] is True


def test_message_row_ids_parse_and_old_rows_without_them_default() -> None:
    pid, parent, mentioned = "p-1", "m-0", ["p-2", "p-3"]
    with_ids = _message_json(
        author_person_id=pid,
        parent_message_id=parent,
        mentions=mentioned,
        mention_names=["Ada", "Bob"],
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "messages": [with_ids, _message_json()],
                "next_before": None,
                "permalink_target_missing": False,
            },
        )

    with _client(handler) as api:
        new, old = api.list_group_messages(_GID, _TID).messages
    assert new.author_person_id == pid
    assert new.parent_message_id == parent
    assert new.mentions == mentioned
    assert old.author_person_id is None
    assert old.parent_message_id is None
    assert old.mentions == []


def test_is_bridged_parses_and_old_rows_default_to_unbridged() -> None:
    new = api_client.group_message_from_dict(
        _message_json(via_display_name="Pepper", is_bridged=True)
    )
    old = api_client.group_message_from_dict(_message_json(via_display_name="Pepper"))
    assert new.is_bridged is True
    assert old.is_bridged is False


def test_composed_via_parses_and_a_missing_field_defaults_to_false() -> None:
    composed = api_client.group_message_from_dict(_message_json(composed_via=True))
    spelled = api_client.group_message_from_dict(_message_json(is_composed=True))
    missing = api_client.group_message_from_dict(_message_json())
    assert composed.composed_via is True
    assert spelled.composed_via is True
    assert missing.composed_via is False


def test_agent_rows_parse_and_old_rows_default_to_a_person() -> None:
    """``is_agent`` / ``for_display_name`` ("*agent* · for *requester*")
    parse off a new server and default off an old one."""
    agent = _message_json(is_agent=True, for_display_name="Ada")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "messages": [agent, _message_json()],
                "next_before": None,
                "permalink_target_missing": False,
            },
        )

    with _client(handler) as api:
        new, old = api.list_group_messages(_GID, _TID).messages
    assert new.is_agent is True
    assert new.for_display_name == "Ada"
    assert old.is_agent is False
    assert old.for_display_name is None


def test_a_tombstone_parses_into_the_same_dataclass() -> None:
    """The server fills every key on a deleted row precisely so a typed
    client does not need a second model for it."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "messages": [
                    _message_json(
                        is_deleted=True,
                        raw_body="",
                        rendered_html="",
                        quote_excerpt="",
                        deleted_at="2026-09-16T17:31:00+00:00",
                    )
                ],
                "next_before": None,
                "permalink_target_missing": False,
            },
        )

    with _client(handler) as api:
        (row,) = api.list_group_messages(_GID, _TID).messages
    assert row.is_deleted is True
    assert row.raw_body == ""
    assert row.deleted_at


def test_a_card_parses_with_its_summary_lines() -> None:
    """``cards[]`` is the API door's structured replacement for the web
    door's ``card_html`` — the whole point is that it needs no HTML."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "messages": [
                    _message_json(
                        cards=[
                            {
                                "kind": "card",
                                "app_key": "todo",
                                "slug": "todo",
                                "title": "To-do",
                                "view": None,
                                "principal": _GID,
                                "url": f"/apps/todo?principal={_GID}&card=1",
                                "summary": {"lines": ["Book flights", "Pack · Friday"]},
                            }
                        ]
                    )
                ],
                "next_before": None,
                "permalink_target_missing": False,
            },
        )

    with _client(handler) as api:
        (row,) = api.list_group_messages(_GID, _TID).messages
    (card,) = row.cards
    assert card.kind == "card"
    assert card.app_key == "todo"
    assert card.summary_lines == ["Book flights", "Pack · Friday"]


def _one_message(**over: object) -> api_client.GroupMessage:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "messages": [_message_json(**over)],
                "next_before": None,
                "permalink_target_missing": False,
            },
        )

    with _client(handler) as api:
        (row,) = api.list_group_messages(_GID, _TID).messages
    return row


def test_author_mentions_and_parent_default_empty_when_the_server_omits_them() -> None:
    """Today's payload carries none of the three, so they must parse to
    empty rather than fail — the fields are forward-compatible."""
    row = _one_message()
    assert row.author_person_id is None
    assert row.mentions == []
    assert row.parent_message_id is None


def test_author_mentions_and_parent_parse_when_present() -> None:
    row = _one_message(
        author_person_id="person-sam",
        mentions=["person-lee", {"person_id": "person-kim", "display_name": "Kim"}, 7, {}],
        parent_message_id="msg-parent",
    )
    assert row.author_person_id == "person-sam"
    # Bare ids and ``{person_id}`` dicts both reduce to the id; anything
    # else is dropped rather than guessed at.
    assert row.mentions == ["person-lee", "person-kim"]
    assert row.parent_message_id == "msg-parent"


def test_a_non_list_mentions_field_parses_to_empty() -> None:
    assert _one_message(mentions="person-lee").mentions == []


def test_list_group_messages_sends_only_the_narrowing_it_was_given() -> None:
    """``since``/``before``/``at`` are mutually exclusive server-side, so the
    client must not quietly send two."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200, json={"messages": [], "next_before": None, "permalink_target_missing": False}
        )

    with _client(handler) as api:
        api.list_group_messages(_GID, _TID, before="cursor-1", limit=25)

    assert seen["params"] == {"before": "cursor-1", "limit": "25"}


def test_post_group_message_sends_the_idempotency_key() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("Idempotency-Key")
        seen["body"] = api_client._json.loads(request.content)
        return httpx.Response(201, json={"message": {"id": _MID}})

    with _client(handler) as api:
        created = api.post_group_message(
            _GID,
            _TID,
            message="hello room",
            mentions=["11111111-1111-4111-8111-111111111111"],
            idempotency_key="abc-123",
        )

    assert seen["method"] == "POST"
    assert seen["url"] == f"http://example.com/api/v1/groups/{_GID}/topics/{_TID}/messages"
    assert seen["key"] == "abc-123"
    assert seen["body"] == {
        "message": "hello room",
        "mentions": ["11111111-1111-4111-8111-111111111111"],
    }
    assert created == {"id": _MID}


def test_post_group_message_sends_on_behalf_of() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = api_client._json.loads(request.content)
        return httpx.Response(201, json={"message": {"id": _MID}})

    with _client(handler) as api:
        api.post_group_message(
            _GID,
            _TID,
            message="done",
            parent_message_id="22222222-2222-4222-8222-222222222222",
            on_behalf_of_person_id="11111111-1111-4111-8111-111111111111",
        )

    assert seen["body"] == {
        "message": "done",
        "parent_message_id": "22222222-2222-4222-8222-222222222222",
        "on_behalf_of_person_id": "11111111-1111-4111-8111-111111111111",
    }


def test_post_group_message_omits_empty_optional_fields() -> None:
    """An absent list is absent from the body rather than sent as ``[]`` —
    the same shape ``chat_send`` already keeps."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert api_client._json.loads(request.content) == {"message": "hi"}
        assert "Idempotency-Key" not in request.headers
        return httpx.Response(201, json={"message": {}})

    with _client(handler) as api:
        api.post_group_message(_GID, _TID, message="hi")


def test_upload_attachment_sends_multipart() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["content_type"] = request.headers["content-type"]
        seen["body"] = request.content
        return httpx.Response(201, json={"id": "att-1", "filename": "notes.txt"})

    with _client(handler) as api:
        row = api.upload_group_attachment(
            _GID, filename="notes.txt", content=b"hello", content_type="text/plain"
        )

    assert seen["url"] == f"http://example.com/api/v1/groups/{_GID}/attachments"
    assert str(seen["content_type"]).startswith("multipart/form-data")
    assert b"notes.txt" in seen["body"]  # type: ignore[operator]
    assert row["id"] == "att-1"


def test_get_attachment_returns_raw_bytes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/v1/groups/{_GID}/attachments/att-1"
        return httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})

    with _client(handler) as api:
        assert api.get_group_attachment(_GID, "att-1") == b"\x89PNG"


def test_reactions_return_the_whole_chip_set() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/reactions")
        assert api_client._json.loads(request.content) == {"emoji": "🎉"}
        return httpx.Response(
            200,
            json={
                "message_id": _MID,
                "reactions": [{"emoji": "🎉", "count": 2, "mine": True, "reactors": "You, Dana"}],
            },
        )

    with _client(handler) as api:
        chips = api.react_to_group_message(_GID, _MID, emoji="🎉")

    assert chips[0]["mine"] is True
    assert chips[0]["count"] == 2


def test_clearing_a_reaction_uses_the_clear_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/reactions/clear")
        return httpx.Response(200, json={"message_id": _MID, "reactions": []})

    with _client(handler) as api:
        assert api.clear_group_reaction(_GID, _MID, emoji="🎉") == []


def test_topic_archive_and_unarchive_pick_the_right_path() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"topic_id": _TID, "archived_at": None})

    with _client(handler) as api:
        api.set_group_topic_archived(_GID, _TID, archived=True)
        api.set_group_topic_archived(_GID, _TID, archived=False)

    assert seen == [
        f"/api/v1/groups/{_GID}/topics/{_TID}/archive",
        f"/api/v1/groups/{_GID}/topics/{_TID}/unarchive",
    ]


def test_group_archive_and_unarchive_pick_the_right_path() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"group_id": _GID, "archived": True})

    with _client(handler) as api:
        api.set_group_archived(_GID, archived=True)
        api.set_group_archived(_GID, archived=False)

    assert seen == [f"/api/v1/groups/{_GID}/archive", f"/api/v1/groups/{_GID}/unarchive"]


def test_create_topic_sends_an_origin_only_when_given() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(api_client._json.loads(request.content))
        return httpx.Response(201, json={"topic": {"topic_id": _TID, "name": "Flights"}})

    with _client(handler) as api:
        api.create_group_topic(_GID, name="Flights")
        api.create_group_topic(_GID, name="Flights", origin_message_id=_MID)

    assert bodies == [{"name": "Flights"}, {"name": "Flights", "origin_message_id": _MID}]


def test_create_group_sends_people_as_a_list() -> None:
    """The one deliberate shape change from the web form, which takes a single
    comma-joined string."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = api_client._json.loads(request.content)
        return httpx.Response(201, json={"group_id": _GID, "display_name": "Trip"})

    with _client(handler) as api:
        created = api.create_group(people=["dana@example.com", "+15551234567"], name="Trip")

    assert seen["body"] == {"people": ["dana@example.com", "+15551234567"], "name": "Trip"}
    assert created["group_id"] == _GID


def test_create_group_without_a_name_omits_the_name_field() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert api_client._json.loads(request.content) == {"people": ["dana@example.com"]}
        return httpx.Response(201, json={"group_id": _GID, "display_name": "Dana"})

    with _client(handler) as api:
        api.create_group(people=["dana@example.com"])


def test_roster_parses_pepper_first() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "members": [
                    {
                        "person_id": "p-pepper",
                        "display_name": "Pepper",
                        "picture_url": None,
                        "is_pepper": True,
                    },
                    {
                        "person_id": "p-dana",
                        "display_name": "Dana",
                        "picture_url": "/img/dana.png",
                        "is_pepper": False,
                    },
                    {
                        "person_id": "p-claude",
                        "display_name": "Claude",
                        "is_agent": True,
                        "agent_profile": "investigate",
                    },
                ]
            },
        )

    with _client(handler) as api:
        roster = api.group_roster(_GID)

    assert [m.display_name for m in roster] == ["Pepper", "Dana", "Claude"]
    assert roster[0].is_pepper is True
    assert roster[1].picture_url == "/img/dana.png"
    assert [m.agent_profile for m in roster] == [None, None, "investigate"]


def test_activity_joins_topic_ids_with_commas() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"topics": []})

    with _client(handler) as api:
        api.group_activity(since="2026-09-16T00:00:00+00:00", topic_ids=[_TID, _MID])

    assert seen["params"] == {
        "since": "2026-09-16T00:00:00+00:00",
        "topic_ids": f"{_TID},{_MID}",
    }


def test_activity_without_topic_ids_sends_only_since() -> None:
    """There is no "just tell me what's new" mode; naming the set is the
    contract."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {"since": "2026-09-16T00:00:00+00:00"}
        return httpx.Response(200, json={"topics": []})

    with _client(handler) as api:
        assert api.group_activity(since="2026-09-16T00:00:00+00:00") == []


def test_topic_search_sends_the_query() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/groups/topics/search"
        assert request.url.params["q"] == "flights"
        return httpx.Response(200, json={"topics": [{"topic_id": _TID}]})

    with _client(handler) as api:
        assert api.search_group_topics("flights") == [{"topic_id": _TID}]


def test_install_and_uninstall_an_app() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(201, json={"install_id": "i-1", "app_key": "todo", "title": "To-do"})

    with _client(handler) as api:
        installed = api.install_group_app(_GID, app_key="todo")
        assert api.uninstall_group_app(_GID, "i-1") is None

    assert installed["install_id"] == "i-1"
    assert seen == [
        ("POST", f"/api/v1/groups/{_GID}/apps"),
        ("DELETE", f"/api/v1/groups/{_GID}/apps/i-1"),
    ]


def test_launcher_returns_tiles_and_the_outage_flag() -> None:
    """``installed_unavailable`` is not "no apps": a client must not wipe
    tiles it already drew because a registry read failed."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "tiles": [
                    {
                        "app_key": "todo",
                        "slug": "todo",
                        "title": "To-do",
                        "icon": "check",
                        "principal": _GID,
                    }
                ],
                "installed_unavailable": False,
            },
        )

    with _client(handler) as api:
        tiles, unavailable = api.group_app_launcher(_GID)

    assert unavailable is False
    assert tiles[0].principal == _GID
    assert tiles[0].icon == "check"


def test_app_chat_posts_the_topic_it_was_opened_from() -> None:
    """One card per (room, app, topic), in the topic the app was opened from —
    which is why ``topic_id`` is on the request rather than derived."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = api_client._json.loads(request.content)
        return httpx.Response(200, json={"reply": "two things left", "steps": []})

    with _client(handler) as api:
        reply = api.group_app_chat(_GID, "todo", message="what's left?", topic_id=_TID)

    assert seen["url"] == f"http://example.com/api/v1/groups/{_GID}/apps/todo/chat"
    assert seen["body"] == {"message": "what's left?", "topic_id": _TID}
    assert reply["reply"] == "two things left"


def test_app_transcript_reads_the_shared_thread() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(
            200, json={"exchanges": [{"is_pepper": False, "speaker": "Dana", "text": "hi"}]}
        )

    with _client(handler) as api:
        assert api.group_app_transcript(_GID, "todo")[0]["speaker"] == "Dana"


def test_contacts_search_lives_under_people() -> None:
    """Not under ``groups``: what it returns is a Contacts search, and groups
    is merely its first caller."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/people/contacts/search"
        assert request.url.params["q"] == "dana"
        return httpx.Response(
            200,
            json={
                "contacts": [
                    {
                        "name": "Dana",
                        "email": "dana@example.com",
                        "phone": None,
                        "handle": "dana@example.com",
                    }
                ]
            },
        )

    with _client(handler) as api:
        (row,) = api.search_contacts("dana")

    assert row["handle"] == "dana@example.com"


def test_list_group_topics_parses_the_default_flag() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "topics": [
                    {
                        "topic_id": "t-1",
                        "name": "General",
                        "last_activity_at": "2026-09-16T17:30:00+00:00",
                        "is_default": True,
                    },
                    {
                        "topic_id": "t-2",
                        "name": "Flights",
                        "last_activity_at": "2026-09-16T17:30:00+00:00",
                    },
                ],
                "group": {"group_id": _GID, "default_topic_id": "t-1"},
            },
        )

    with _client(handler) as api:
        topics = api.list_group_topics(_GID)

    # A server predating the field reads as "not the default", never an error.
    assert [t.is_default for t in topics] == [True, False]


def test_a_non_member_room_raises_api_error() -> None:
    """The server's uniform 404 reaches the caller as an :class:`ApiError`
    carrying the status, not as a ``None`` the caller has to guess about."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "No such group."})

    with _client(handler) as api:
        try:
            api.list_group_topics(_GID)
        except api_client.ApiError as exc:
            assert exc.status == 404
            assert exc.detail == "No such group."
        else:  # pragma: no cover - the call above must raise
            raise AssertionError("expected ApiError")


def test_room_mute_read_mark_and_sharing_wire_shapes() -> None:
    seen: list[tuple] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.content.decode()))
        return httpx.Response(200, json={})

    c = _client(handler)
    c.set_group_muted(_GID, muted=True)
    c.set_group_muted(_GID, muted=False)
    c.set_group_read_mark(_GID, _TID, read_through="2026-10-02T10:00:00+00:00")
    c.get_group_sharing(_GID)
    c.set_group_sharing(_GID, calendars={"c1": "busy"})
    base = f"/api/v1/groups/{_GID}"
    assert seen == [
        ("POST", f"{base}/mute", ""),
        ("POST", f"{base}/unmute", ""),
        ("PUT", f"{base}/topics/{_TID}/read", '{"read_through":"2026-10-02T10:00:00+00:00"}'),
        ("GET", f"{base}/sharing", ""),
        ("POST", f"{base}/sharing", '{"calendars":{"c1":"busy"}}'),
    ]


def test_get_groups_unread_parses_rows_marks_and_the_deduped_total() -> None:
    seen: list[httpx.Request] = []
    body = {
        "topics": [
            {
                "group_id": _GID,
                "topic_id": _TID,
                "unread_count": 2,
                "has_unread_mention": True,
                "floor": "2026-01-01T00:00:00+00:00",
                "read_through": "2026-09-16T10:05:00+00:00",
                "a_future_field": 1,
            },
            {"topic_id": "t2", "unread_count": 0, "read_through": None},  # no mark: no cursor
            {"topic_id": "t3", "unread_count": "many"},  # malformed rows are dropped
            {"unread_count": 1},
            "nonsense",
        ],
        "unread_total": 1,  # not the 2 the rows add up to
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body)

    state = _client(handler).get_groups_unread()
    assert (seen[0].method, seen[0].url.path) == ("GET", "/api/v1/groups/unread")
    assert state.marks == {_TID: "2026-09-16T10:05:00+00:00"}
    assert state.unread == [
        api_client.GroupUnreadRow(_TID, 2, True, "2026-01-01T00:00:00+00:00"),
        api_client.GroupUnreadRow("t2", 0, False, None),
    ]
    assert state.unread_total == 1


def test_get_groups_unread_tolerates_a_missing_or_malformed_total() -> None:
    for body in ({"topics": []}, {"topics": [], "unread_total": True}):
        state = _client(lambda request, b=body: httpx.Response(200, json=b)).get_groups_unread()
        assert state.unread == [] and state.unread_total is None
