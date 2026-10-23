"""Tests for the ApiClient room-skill methods.

Same ``httpx.MockTransport`` style as ``test_api_client_scheduled_tasks.py``:
assert the wire shape (method, URL, body) and that a response parses into
the dataclasses. What a client could get wrong by analogy with ``/skills``:
a room's skill is addressed by its **grant id**, never its name, and the
share body carries a name and nothing else.
"""

from __future__ import annotations

import json

import httpx
import pytest

from pepper_cli import api_client
from pepper_cli.secret import SecretStr

_ROOM = "00000000-0000-0000-0000-0000000000c1"
_GRANT = "00000000-0000-0000-0000-0000000000d1"
_ROOM_SKILLS = f"http://example.com/api/v1/groups/{_ROOM}/skills"


def _client(handler) -> api_client.ApiClient:
    return api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _capture(response: httpx.Response) -> tuple[dict, object]:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content) if request.content else None
        return response

    return seen, handler


def _row_json(**over) -> dict:
    base = {
        "id": _GRANT,
        "name": "meal-plan",
        "description": "Plan the week's dinners.",
        "granted_by": "Sam",
        "owner_is_you": False,
        "builtin": False,
        "toolsets": ["github"],
        "state": "inactive",
        "ungrantable_toolsets": ["github"],
        "can_revoke": True,
    }
    base.update(over)
    return base


def test_list_room_skills_hits_the_room_and_parses_every_field() -> None:
    seen, handler = _capture(httpx.Response(200, json=[_row_json()]))
    with _client(handler) as api:
        rows = api.list_room_skills(_ROOM)

    assert (seen["method"], seen["url"]) == ("GET", _ROOM_SKILLS)
    assert rows == [
        api_client.RoomSkill(
            id=_GRANT,
            name="meal-plan",
            description="Plan the week's dinners.",
            granted_by="Sam",
            owner_is_you=False,
            builtin=False,
            toolsets=("github",),
            state="inactive",
            ungrantable_toolsets=("github",),
            can_revoke=True,
        )
    ]


def test_list_room_skills_parses_builtin_and_tolerates_its_absence() -> None:
    absent = {k: v for k, v in _row_json().items() if k != "builtin"}
    _, handler = _capture(httpx.Response(200, json=[_row_json(builtin=True), absent]))
    with _client(handler) as api:
        built_in, older = api.list_room_skills(_ROOM)
    assert built_in.builtin is True
    assert older.builtin is False


def test_list_room_skills_keeps_a_departed_granter_as_none() -> None:
    _, handler = _capture(httpx.Response(200, json=[_row_json(granted_by=None)]))
    with _client(handler) as api:
        assert api.list_room_skills(_ROOM)[0].granted_by is None


def test_get_room_skill_is_addressed_by_grant_id_and_carries_the_skill() -> None:
    skill = {
        "name": "meal-plan",
        "description": "Plan the week's dinners.",
        "body": "Ask what is in the fridge.",
        "model": None,
        "toolsets": [],
        "max_tool_rounds": None,
        "builtin": False,
    }
    seen, handler = _capture(httpx.Response(200, json=_row_json(skill=skill)))
    with _client(handler) as api:
        detail = api.get_room_skill(_ROOM, _GRANT)

    assert (seen["method"], seen["url"]) == ("GET", f"{_ROOM_SKILLS}/{_GRANT}")
    assert detail.row.id == _GRANT
    assert detail.row.granted_by == "Sam"
    assert isinstance(detail.skill, api_client.SkillDetail)
    assert detail.skill.body == "Ask what is in the fridge."


def test_share_room_skill_posts_the_name_and_nothing_else() -> None:
    seen, handler = _capture(httpx.Response(201, json=_row_json(state="active")))
    with _client(handler) as api:
        row = api.share_room_skill(_ROOM, "meal-plan")

    assert (seen["method"], seen["url"]) == ("POST", _ROOM_SKILLS)
    assert seen["body"] == {"name": "meal-plan"}
    assert row.id == _GRANT
    assert row.state == "active"


def test_revoke_room_skill_deletes_the_grant_not_the_skill() -> None:
    seen, handler = _capture(httpx.Response(204))
    with _client(handler) as api:
        assert api.revoke_room_skill(_ROOM, _GRANT) is None

    # The grant under the room — never ``/api/v1/skills/{name}``, which
    # would delete the owner's skill itself.
    assert (seen["method"], seen["url"]) == ("DELETE", f"{_ROOM_SKILLS}/{_GRANT}")
    assert seen["body"] is None


@pytest.mark.parametrize("status", [403, 404, 409, 422, 503])
def test_a_refusal_surfaces_status_and_detail(status) -> None:
    _, handler = _capture(httpx.Response(status, json={"detail": "why not"}))
    with _client(handler) as api, pytest.raises(api_client.ApiError) as err:
        api.share_room_skill(_ROOM, "meal-plan")
    assert (err.value.status, err.value.detail) == (status, "why not")


@pytest.mark.parametrize(
    "call",
    [
        lambda api: api.list_room_skills(_ROOM),
        lambda api: api.get_room_skill(_ROOM, _GRANT),
        lambda api: api.share_room_skill(_ROOM, "meal-plan"),
    ],
)
def test_a_body_of_the_wrong_shape_is_an_api_error_not_a_crash(call) -> None:
    _, handler = _capture(httpx.Response(200, json="nope"))
    with _client(handler) as api, pytest.raises(api_client.ApiError) as err:
        call(api)
    assert err.value.detail == "malformed response"
