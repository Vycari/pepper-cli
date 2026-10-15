"""Tests for ``pepper skills … --room``.

Same approach as ``test_cli_skills.py``: ``cli.main([...])`` against a fake
``ApiClient``. What these pin is what only a room has — a skill there is a
*grant*, addressed by grant id, so a name has to be resolved (and refused
when it is ambiguous); who may revoke is the server's answer, said in one
sentence; and a room never owns a skill, so the authoring verbs refuse it.
"""

from __future__ import annotations

import pytest

from pepper_cli import api_client, cli, credentials
from pepper_cli.secret import SecretStr

_ROOM = "00000000-0000-0000-0000-0000000000c1"
_G1 = "00000000-0000-0000-0000-0000000000d1"
_G2 = "00000000-0000-0000-0000-0000000000d2"


@pytest.fixture
def logged_in(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    credentials.save(server_url="https://pepper.test", token=SecretStr("pep_pat_test"))
    return tmp_path


def _row(**over) -> api_client.RoomSkill:
    base = {
        "id": _G1,
        "name": "meal-plan",
        "description": "Plan the week's dinners.",
        "granted_by": "Sam",
        "owner_is_you": False,
        "toolsets": (),
        "state": "active",
        "ungrantable_toolsets": (),
        "can_revoke": False,
    }
    base.update(over)
    return api_client.RoomSkill(**base)


def _skill(**over) -> api_client.SkillDetail:
    base = {
        "name": "meal-plan",
        "description": "Plan the week's dinners.",
        "body": "Ask what is in the fridge.",
        "model": None,
        "toolsets": (),
        "max_tool_rounds": None,
        "builtin": False,
    }
    base.update(over)
    return api_client.SkillDetail(**base)


class _FakeApi:
    """Records every call. A canned value that is an exception is raised."""

    calls: list[tuple] = []
    rows: list[api_client.RoomSkill] = []
    shared: object = None
    revoked: object = None

    def __init__(self, *, server_url, token) -> None:
        del server_url, token

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __getattr__(self, name):
        # Anything not a room-skill call (``create_skill``, ``delete_skill``,
        # …) must never be reached by these commands.
        raise AssertionError(f"unexpected ApiClient.{name}")

    def list_groups(self, *, archived=False):
        del archived
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

    def list_room_skills(self, group_id):
        self.calls.append(("list", group_id))
        return list(self.rows)

    def get_room_skill(self, group_id, grant_id):
        self.calls.append(("get", group_id, grant_id))
        row = next(r for r in self.rows if r.id == grant_id)
        return api_client.RoomSkillDetail(row=row, skill=_skill(name=row.name))

    def share_room_skill(self, group_id, name):
        self.calls.append(("share", group_id, name))
        if isinstance(self.shared, Exception):
            raise self.shared
        return self.shared

    def revoke_room_skill(self, group_id, grant_id):
        self.calls.append(("revoke", group_id, grant_id))
        if isinstance(self.revoked, Exception):
            raise self.revoked


@pytest.fixture
def api(monkeypatch, logged_in):
    del logged_in
    _FakeApi.calls = []
    _FakeApi.rows = []
    _FakeApi.shared = None
    _FakeApi.revoked = None
    monkeypatch.setattr(cli.api_client, "ApiClient", _FakeApi)
    return _FakeApi


def _err(status: int, detail: str) -> api_client.ApiError:
    return api_client.ApiError(status=status, body="", detail=detail)


def _flat(text: str) -> str:
    """Rich wraps at the captured terminal's width; compare unwrapped."""
    return " ".join(text.split())


# --- list -------------------------------------------------------------------


def test_list_reads_the_room_and_says_who_shared_each(api, capsys) -> None:
    api.rows = [
        _row(owner_is_you=True, granted_by="Allen", can_revoke=True),
        _row(id=_G2, name="trip-budget", description="Track spend."),
    ]
    assert cli.main(["skills", "list", "--room", "trip"]) == 0
    assert api.calls == [("list", _ROOM)]
    captured = capsys.readouterr()
    mine, theirs = captured.out.splitlines()
    assert mine == f"* meal-plan — Plan the week's dinners.  [shared by you]  {_G1}"
    # No marker: not the caller's skill and not a room they manage.
    assert theirs == f"  trip-budget — Track spend.  [shared by Sam]  {_G2}"
    assert "* you can revoke it" in captured.err


def test_list_without_a_revocable_row_prints_no_legend(api, capsys) -> None:
    api.rows = [_row()]
    assert cli.main(["skills", "list", "--room", _ROOM]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("  meal-plan")
    assert "revoke" not in captured.err


def test_list_names_what_made_a_skill_inactive_or_shadowed(api, capsys) -> None:
    api.rows = [
        _row(state="inactive", toolsets=("github", "web"), ungrantable_toolsets=("github",)),
        _row(id=_G2, state="shadowed", granted_by=None),
    ]
    assert cli.main(["skills", "list", "--room", _ROOM]) == 0
    inactive, shadowed = capsys.readouterr().out.splitlines()
    # The toolset that did it — not every toolset the skill declares.
    assert "[shared by Sam · inactive: a room can't run github]" in inactive
    assert "web" not in inactive
    assert "shadowed: an earlier 'meal-plan' shared here wins" in shadowed
    # A granter whose account is gone is said, not printed as "None".
    assert "shared by someone no longer here" in shadowed
    assert "None" not in shadowed


def test_list_of_an_empty_room_says_so(api, capsys) -> None:
    assert cli.main(["skills", "list", "--room", _ROOM]) == 0
    assert "no skills shared with this room" in capsys.readouterr().out


def test_list_without_room_is_still_your_own_library(api, monkeypatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(
        _FakeApi, "list_skills", lambda self: seen.append("own") or [], raising=False
    )
    assert cli.main(["skills", "list"]) == 0
    assert seen == ["own"]
    assert api.calls == []


def test_an_unknown_room_is_exit_2_and_reads_nothing(api, capsys) -> None:
    assert cli.main(["skills", "list", "--room", "nowhere"]) == 2
    assert api.calls == []
    assert "no room named 'nowhere'" in _flat(capsys.readouterr().err)


def test_a_server_refusal_on_list_is_exit_1_with_its_detail(api, monkeypatch, capsys) -> None:
    def boom(self, group_id):
        raise _err(404, "No such group.")

    monkeypatch.setattr(_FakeApi, "list_room_skills", boom)
    assert cli.main(["skills", "list", "--room", _ROOM]) == 1
    assert "Server returned 404: No such group." in _flat(capsys.readouterr().err)


# --- show -------------------------------------------------------------------


def test_show_resolves_the_name_to_its_grant_and_prints_skill_md(api, capsys) -> None:
    api.rows = [_row(id=_G2, name="trip-budget"), _row()]
    assert cli.main(["skills", "show", "meal-plan", "--room", "Trip"]) == 0
    assert api.calls == [("list", _ROOM), ("get", _ROOM, _G1)]
    captured = capsys.readouterr()
    assert captured.out.startswith("---\nname: meal-plan\n")
    assert "Ask what is in the fridge." in captured.out
    # Who shared it is chrome: stderr, so stdout stays a valid SKILL.md.
    assert "shared with this room by Sam" in _flat(captured.err)
    assert "Sam" not in captured.out


def test_show_takes_a_grant_id_without_listing(api, capsys) -> None:
    api.rows = [_row(state="shadowed")]
    assert cli.main(["skills", "show", _G1, "--room", _ROOM]) == 0
    assert api.calls == [("get", _ROOM, _G1)]
    assert "shadowed" in _flat(capsys.readouterr().err)


def test_show_of_a_name_the_room_does_not_hold_is_exit_2(api, capsys) -> None:
    api.rows = [_row()]
    assert cli.main(["skills", "show", "nope", "--room", _ROOM]) == 2
    assert api.calls == [("list", _ROOM)]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no skill named 'nope' is shared with that room" in _flat(captured.err)


# --- share ------------------------------------------------------------------


def test_share_posts_the_name_to_the_room(api, capsys) -> None:
    api.shared = _row(owner_is_you=True, can_revoke=True)
    assert cli.main(["skills", "share", "meal-plan", "--room", "trip"]) == 0
    assert api.calls == [("share", _ROOM, "meal-plan")]
    err = _flat(capsys.readouterr().err)
    assert "Shared 'meal-plan' with the room." in err
    assert "It is" not in err


def test_share_says_when_the_room_cannot_run_what_it_now_holds(api, capsys) -> None:
    api.shared = _row(state="shadowed")
    assert cli.main(["skills", "share", "meal-plan", "--room", _ROOM]) == 0
    assert "It is shadowed: an earlier 'meal-plan' shared here wins." in _flat(
        capsys.readouterr().err
    )


def test_share_into_a_sealed_room_says_that_kind_of_room_is_not_allowed_yet(api, capsys) -> None:
    api.shared = _err(409, "the server's own wording")
    assert cli.main(["skills", "share", "meal-plan", "--room", _ROOM]) == 1
    err = _flat(capsys.readouterr().err)
    assert "sharing a skill into this kind of room isn't allowed yet" in err
    assert "named channel" in err


@pytest.mark.parametrize(
    ("status", "detail"),
    [
        (404, "You have no skill named 'nope'. Only a skill you wrote can be shared."),
        (
            422,
            "'meal-plan' declares toolsets, and a skill that declares toolsets cannot be shared.",
        ),
        (422, "'meal-plan' uses github, which a room cannot run."),
        (503, "Groups service is unavailable."),
    ],
)
def test_share_refusals_carry_the_servers_reason(api, capsys, status, detail) -> None:
    """Unknown skill, not shareable, room unavailable: three different things
    to do next, so each is the server's sentence rather than one blanket line."""
    api.shared = _err(status, detail)
    assert cli.main(["skills", "share", "meal-plan", "--room", _ROOM]) == 1
    assert f"Server returned {status}: {detail}" in _flat(capsys.readouterr().err)


def test_share_to_an_unknown_room_writes_nothing(api) -> None:
    assert cli.main(["skills", "share", "meal-plan", "--room", "nowhere"]) == 2
    assert api.calls == []


# --- revoke -----------------------------------------------------------------


def test_revoke_resolves_the_name_and_deletes_that_grant(api, capsys) -> None:
    api.rows = [_row(id=_G2, name="trip-budget"), _row()]
    assert cli.main(["skills", "revoke", "meal-plan", "--room", "trip", "--yes"]) == 0
    assert api.calls == [("list", _ROOM), ("revoke", _ROOM, _G1)]
    err = _flat(capsys.readouterr().err)
    assert "Stopped sharing 'meal-plan' with the room." in err
    assert "The skill itself is untouched." in err


def test_revoke_refuses_an_ambiguous_name_and_shows_the_grant_ids(api, capsys) -> None:
    api.rows = [_row(owner_is_you=True), _row(id=_G2, granted_by="Kim", state="shadowed")]
    assert cli.main(["skills", "revoke", "meal-plan", "--room", _ROOM, "--yes"]) == 2
    # Neither was guessed at.
    assert api.calls == [("list", _ROOM)]
    err = capsys.readouterr().err
    assert "2 skills named 'meal-plan' are shared with that room" in _flat(err)
    assert "pass the grant id in place of the name" in _flat(err)
    # Each id whole on its own line, with enough to tell them apart.
    assert f"  {_G1}  shared by you (active)\n" in err
    assert f"  {_G2}  shared by Kim (shadowed)\n" in err


def test_revoke_by_grant_id_picks_one_of_two_same_named(api, capsys) -> None:
    api.rows = [_row(), _row(id=_G2)]
    assert cli.main(["skills", "revoke", _G2, "--room", _ROOM, "--yes"]) == 0
    assert api.calls == [("revoke", _ROOM, _G2)]
    assert "Stopped sharing that skill with the room." in _flat(capsys.readouterr().err)


def test_revoke_of_a_name_the_room_does_not_hold_is_exit_2(api, capsys) -> None:
    assert cli.main(["skills", "revoke", "meal-plan", "--room", _ROOM, "--yes"]) == 2
    assert api.calls == [("list", _ROOM)]
    assert "no skill named 'meal-plan' is shared with that room" in _flat(capsys.readouterr().err)


def test_revoke_refused_says_who_can(api, capsys) -> None:
    api.rows = [_row()]
    api.revoked = _err(403, "the server's own wording")
    assert cli.main(["skills", "revoke", "meal-plan", "--room", _ROOM, "--yes"]) == 1
    err = _flat(capsys.readouterr().err)
    assert "only the skill's owner or a room owner can revoke it." in err
    assert "Stopped sharing" not in err


def test_revoke_of_a_grant_that_is_gone_carries_the_servers_reason(api, capsys) -> None:
    api.revoked = _err(404, "No such skill is shared with this room.")
    assert cli.main(["skills", "revoke", _G1, "--room", _ROOM, "--yes"]) == 1
    assert "Server returned 404: No such skill is shared with this room." in _flat(
        capsys.readouterr().err
    )


# --- usage ------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["share", "revoke"])
def test_share_and_revoke_require_a_room(api, capsys, verb) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["skills", verb, "meal-plan"])
    assert exit_info.value.code == 2
    assert "--room" in capsys.readouterr().err
    assert api.calls == []


@pytest.mark.parametrize(
    ("argv", "verb"),
    [
        (["skills", "create", "meal-plan", "--room", "trip"], "write"),
        (["skills", "edit", "meal-plan", "--room", "trip"], "edit"),
        (["skills", "delete", "meal-plan", "--yes", "--room", "trip"], "delete"),
    ],
)
def test_authoring_verbs_refuse_a_room_before_touching_anything(
    api, monkeypatch, capsys, argv, verb
) -> None:
    """A room never owns a skill. The fake raises on any call the authoring
    path would make, and the editor is never opened."""
    monkeypatch.setattr(
        cli, "_open_in_editor", lambda initial: pytest.fail("the editor must not open")
    )
    assert cli.main(argv) == 2
    assert api.calls == []
    err = _flat(capsys.readouterr().err)
    assert f"a room never owns a skill, so there is nothing to {verb} there" in err
    assert "it updates everywhere it is shared" in err
    assert "pepper skills share NAME --room ROOM" in err
