"""Tests for the ApiClient todo methods.

Same ``httpx.MockTransport`` style as ``test_api_client.py`` — assert
the wire shape (method, URL, body) and that responses parse into
``TodoEntry`` dataclasses. This module is the contract the native
clients reproduce.
"""

from __future__ import annotations

import json

import httpx

from pepper_cli import api_client
from pepper_cli.secret import SecretStr


def _mock(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _todo_json(**over) -> dict:
    base = {
        "id": "00000000-0000-0000-0000-000000000010",
        "title": "Buy milk",
        "details": None,
        "status": "open",
        "source": "user",
        "priority": None,
        "due_date": None,
        "created_at": "2026-06-05T00:00:00Z",
        "done_at": None,
        "source_ref": None,
    }
    base.update(over)
    return base


def _client(handler) -> api_client.ApiClient:
    return api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=_mock(handler),
    )


def test_list_todos_default_status_and_parse() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        return httpx.Response(200, json=[_todo_json(priority="high")])

    with _client(handler) as api:
        rows = api.list_todos()

    assert seen["method"] == "GET"
    assert seen["url"] == "http://example.com/api/v1/todos?status=open"
    assert len(rows) == 1
    assert isinstance(rows[0], api_client.TodoEntry)
    assert rows[0].title == "Buy milk"
    assert rows[0].priority == "high"


def test_list_todos_passes_status() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=[])

    with _client(handler) as api:
        assert api.list_todos(status="all") == []

    assert seen["url"] == "http://example.com/api/v1/todos?status=all"


def test_create_todo_posts_only_provided_fields() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json=_todo_json(title="Call dentist", priority="high"))

    with _client(handler) as api:
        todo = api.create_todo(title="Call dentist", priority="high")

    assert seen["method"] == "POST"
    assert seen["url"] == "http://example.com/api/v1/todos"
    # details / due_date omitted entirely when not provided
    assert seen["body"] == {"title": "Call dentist", "priority": "high"}
    assert todo.title == "Call dentist"


def test_create_todo_includes_due_and_details() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json=_todo_json())

    with _client(handler) as api:
        api.create_todo(title="x", details="more", due_date="2026-06-10T15:00:00-07:00")

    assert seen["body"] == {
        "title": "x",
        "details": "more",
        "due_date": "2026-06-10T15:00:00-07:00",
    }


def test_complete_and_dismiss_hit_right_urls() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url)))
        status = "done" if request.url.path.endswith("/done") else "dismissed"
        return httpx.Response(200, json=_todo_json(status=status))

    tid = "00000000-0000-0000-0000-000000000010"
    with _client(handler) as api:
        done = api.complete_todo(tid)
        dismissed = api.dismiss_todo(tid)

    assert seen == [
        ("POST", f"http://example.com/api/v1/todos/{tid}/done"),
        ("POST", f"http://example.com/api/v1/todos/{tid}/dismiss"),
    ]
    assert done.status == "done"
    assert dismissed.status == "dismissed"


def test_complete_todo_404_raises_api_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "todo not found"})

    with _client(handler) as api:
        try:
            api.complete_todo("00000000-0000-0000-0000-000000000099")
        except api_client.ApiError as exc:
            assert exc.status == 404
            assert exc.detail == "todo not found"
        else:  # pragma: no cover - failure path
            raise AssertionError("expected ApiError")


def test_room_todo_calls_use_the_typed_room_routes() -> None:
    seen: list[tuple] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.url.query.decode()))
        if request.method == "GET":
            return httpx.Response(200, json=[_todo_json()])
        return httpx.Response(200, json=_todo_json())

    c = _client(handler)
    assert [t.id for t in c.list_todos(status="all", group_id="g1")] == [_todo_json()["id"]]
    c.create_todo(title="x", due="2026-10-09", group_id="g1")
    c.complete_todo("t1", group_id="g1")
    c.reopen_todo("t1", group_id="g1")
    c.dismiss_todo("t1", group_id="g1")
    assert seen == [
        ("GET", "/api/v1/groups/g1/todos", "status=all"),
        ("POST", "/api/v1/groups/g1/todos", ""),
        ("POST", "/api/v1/groups/g1/todos/t1/done", ""),
        ("POST", "/api/v1/groups/g1/todos/t1/reopen", ""),
        ("POST", "/api/v1/groups/g1/todos/t1/dismiss", ""),
    ]


_G = "00000000-0000-0000-0000-0000000000a1"
_T = "00000000-0000-0000-0000-000000000010"


def _recording(response: httpx.Response | None = None):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response or httpx.Response(200, json=[])

    return seen, handler


def test_list_todos_mine_uses_each_surfaces_own_filter() -> None:
    seen, handler = _recording()
    with _client(handler) as api:
        api.list_todos(mine=True)
        api.list_todos(mine=True, group_id=_G)
        api.list_todos(asked=True, group_id=_G)
    urls = [str(r.url) for r in seen]
    assert urls[0] == "http://example.com/api/v1/todos?status=open&asked_of_me=true"
    assert urls[1].endswith(f"/groups/{_G}/todos?status=open&assignee=me")
    assert urls[2].endswith(f"/groups/{_G}/todos?status=open&requester=me")


def test_list_todos_asked_without_a_room_is_refused_before_the_wire() -> None:
    seen, handler = _recording()
    with _client(handler) as api:
        try:
            api.list_todos(asked=True)
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")
    assert seen == []


def test_assigned_todo_parses_people_and_ignores_a_malformed_one() -> None:
    row = _todo_json(
        assignee={"user_id": "u2", "label": "Bob"},
        requested_by={"user_id": "u1", "label": "Ann"},
    )
    bad = _todo_json(assignee="Bob", requested_by={"label": "no id"})
    _, handler = _recording(httpx.Response(200, json=[row, bad]))
    with _client(handler) as api:
        first, second = api.list_todos(group_id=_G)
    assert first.assignee == api_client.TodoPerson(user_id="u2", label="Bob")
    assert first.requested_by == api_client.TodoPerson(user_id="u1", label="Ann")
    assert second.assignee is None
    assert second.requested_by is None


def test_todo_person_keeps_null_fields_null() -> None:
    row = _todo_json(
        assignee={"user_id": None, "label": None},
        requested_by={"user_id": "u1", "label": None},
    )
    _, handler = _recording(httpx.Response(200, json=[row]))
    with _client(handler) as api:
        (todo,) = api.list_todos(group_id=_G)
    assert todo.assignee == api_client.TodoPerson(user_id=None, label=None)
    assert todo.requested_by == api_client.TodoPerson(user_id="u1", label=None)


def test_create_todo_sends_assignee_person_id_only_when_given() -> None:
    seen, handler = _recording(httpx.Response(200, json=_todo_json()))
    with _client(handler) as api:
        api.create_todo(title="a", group_id=_G)
        api.create_todo(title="b", group_id=_G, assignee_person_id="p1")
    assert "assignee_person_id" not in json.loads(seen[0].content)
    assert json.loads(seen[1].content)["assignee_person_id"] == "p1"


def test_edit_todo_patches_exactly_the_given_fields() -> None:
    seen, handler = _recording(httpx.Response(200, json=_todo_json(title="New")))
    with _client(handler) as api:
        personal = api.edit_todo(_T, {"title": "New", "details": None})
        api.edit_todo(_T, {"assignee_person_id": "p1"}, group_id=_G)
    assert [r.method for r in seen] == ["PATCH", "PATCH"]
    assert str(seen[0].url) == f"http://example.com/api/v1/todos/{_T}"
    assert json.loads(seen[0].content) == {"title": "New", "details": None}
    assert str(seen[1].url) == f"http://example.com/api/v1/groups/{_G}/todos/{_T}"
    assert personal.title == "New"


def test_edit_todo_conflict_raises_with_the_status() -> None:
    _, handler = _recording(httpx.Response(409, json={"detail": "the ask has changed"}))
    with _client(handler) as api:
        try:
            api.edit_todo(_T, {"title": "x"})
        except api_client.ApiError as exc:
            assert exc.status == 409
            assert "the ask has changed" in exc.detail
        else:
            raise AssertionError("expected ApiError")


def test_dismiss_todo_posts_a_reason_only_when_given() -> None:
    seen, handler = _recording(httpx.Response(200, json=_todo_json(status="dismissed")))
    with _client(handler) as api:
        api.dismiss_todo(_T)
        api.dismiss_todo(_T, group_id=_G, reason="out of town")
    assert not seen[0].content
    assert json.loads(seen[1].content) == {"reason": "out of town"}


def test_next_action_fields_parse_and_are_optional() -> None:
    row = _todo_json(
        next_action={"text": "Call Bob", "by": "model", "updated_at": "2026-10-20T00:00:00Z"},
        next_action_suggestion={"text": "Book it", "updated_at": "2026-10-21T00:00:00Z"},
    )
    with _client(lambda _r: httpx.Response(200, json=[row, _todo_json()])) as api:
        with_fields, without = api.list_todos()
    assert with_fields.next_action == api_client.TodoNextAction(
        text="Call Bob", by="model", updated_at="2026-10-20T00:00:00Z"
    )
    assert with_fields.next_action_suggestion is not None
    assert with_fields.next_action_suggestion.text == "Book it"
    assert without.next_action is None and without.next_action_suggestion is None


def _stored(**over) -> dict:
    """An item as ``/api/v1/todos/rooms`` carries it: the stored document, no ``assignee``."""
    return {**_todo_json(), "dedup_key": None, **over}


def test_list_room_todos_reads_each_rooms_items_and_the_degraded_flag() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        body = {
            "rooms": [
                {"group_id": "g1", "name": "Family", "items": [_stored(), _stored(id="2")]},
                {"group_id": "g2", "items": []},
                "junk",
                {"name": "no id"},
            ],
            "degraded": False,
        }
        return httpx.Response(200, json=body)

    with _client(handler) as api:
        found = api.list_room_todos()
        asked = api.list_room_todos(mine=True)

    assert seen == [
        "http://example.com/api/v1/todos/rooms",
        "http://example.com/api/v1/todos/rooms?assignee=me",
    ]
    assert [(r.group_id, r.name, len(r.items)) for r in found.rooms] == [
        ("g1", "Family", 2),
        ("g2", "", 0),
    ]
    assert isinstance(found.rooms[0].items[0], api_client.TodoEntry)
    assert not found.degraded
    assert asked.rooms == found.rooms


def test_list_room_todos_degraded_and_malformed() -> None:
    def degraded(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"rooms": [], "degraded": True})

    with _client(degraded) as api:
        assert api.list_room_todos().degraded

    def malformed(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "an", "object"])

    with _client(malformed) as api:
        import pytest

        with pytest.raises(api_client.ApiError):
            api.list_room_todos()


def test_a_room_items_ask_is_read_from_source_ref_as_the_server_does() -> None:
    ask = _stored(
        source="request",
        source_ref={
            "assignee_user_id": "u-me",
            "assignee_label": "Me",
            "requester_id": "u-sam",
            "requester_label": "Sam",
            "state": "pending",
        },
    )
    label_only = _stored(
        id="3", source="request", source_ref={"requester_label": "Pat", "assignee_user_id": None}
    )
    # Only a request's copy names parties; another producer's source_ref is left alone.
    not_an_ask = _stored(id="4", source="notes", source_ref={"assignee_user_id": "u-me"})
    bare = _stored(id="5", source="request", source_ref=None)

    def handler(_request: httpx.Request) -> httpx.Response:
        items = [ask, label_only, not_an_ask, bare]
        return httpx.Response(
            200, json={"rooms": [{"group_id": "g", "name": "R", "items": items}], "degraded": False}
        )

    with _client(handler) as api:
        got = api.list_room_todos().rooms[0].items

    assert got[0].assignee == api_client.TodoPerson(user_id="u-me", label="Me")
    assert got[0].requested_by == api_client.TodoPerson(user_id="u-sam", label="Sam")
    assert got[1].assignee is None
    assert got[1].requested_by == api_client.TodoPerson(user_id=None, label="Pat")
    assert got[2].assignee is None
    assert got[3].assignee is None


def test_list_room_todos_skips_an_item_that_is_not_an_object() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        body = {"rooms": [{"group_id": "g", "name": "R", "items": ["junk", _stored()]}]}
        return httpx.Response(200, json=body)

    with _client(handler) as api:
        assert len(api.list_room_todos().rooms[0].items) == 1
