"""End-to-end tests for the ``pepper notes`` subcommands (the read side:
``list``/``search``/``show``, plus the write side).

Same approach as ``test_cli_groups.py``: invoke ``cli.main([...])`` with a
monkeypatched ``api_client.ApiClient`` so commands run without the network.

Room-name resolution reuses ``_resolve_group`` (exercised in full in
``test_cli_groups.py``); here it's only checked end-to-end through one
``--room`` case, since the resolver itself doesn't change. Note-name
resolution (``_resolve_note``) gets its own ambiguous/unknown/unique
coverage, since that's new here.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from pepper_cli import api_client, cli, credentials
from pepper_cli.secret import SecretStr

_GID_TRIP = "00000000-0000-0000-0000-0000000000a1"
_NID_PACKING = "00000000-0000-0000-0000-0000000000d1"
_NID_RUNBOOK_1 = "00000000-0000-0000-0000-0000000000d2"
_NID_RUNBOOK_2 = "00000000-0000-0000-0000-0000000000d3"


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


def _note(**over) -> api_client.ArtifactListItem:
    base = {
        "id": _NID_PACKING,
        "name": "Packing list",
        "kind": "inline_text",
        "updated_at": "2026-09-16T17:30:00+00:00",
        "etag": "abc123",
        "labels": [],
        "summary": None,
    }
    base.update(over)
    return api_client.ArtifactListItem(**base)


def _note_list(*notes, truncated=False) -> api_client.ArtifactListResponse:
    return api_client.ArtifactListResponse(truncated=truncated, items=list(notes))


def _detail(**over) -> api_client.ArtifactDetail:
    base = {
        "id": _NID_PACKING,
        "name": "Packing list",
        "kind": "inline_text",
        "updated_at": "2026-09-16T17:30:00+00:00",
        "etag": "abc123",
        "shared": False,
        "body": "- sunscreen\n- passport\n",
        "labels": [],
        "summary": None,
    }
    base.update(over)
    return api_client.ArtifactDetail(**base)


class _FakeNotesApi:
    """Records calls and returns canned notes/groups responses (or raises)."""

    instances: list[_FakeNotesApi] = []

    def __init__(self, *, server_url, token, **canned) -> None:
        self.server_url = server_url
        self.token = token
        self._canned = canned
        self.calls: list[tuple] = []
        self.name_filters: list[tuple[str | None, str | None]] = []
        self._edit_call_count = 0
        _FakeNotesApi.instances.append(self)

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

    def list_artifacts(
        self,
        *,
        principal=None,
        kind=None,
        q=None,
        label=None,
        name=None,
        name_prefix=None,
        limit=None,
    ):
        self.calls.append(("list_artifacts", principal, kind, q, label, limit))
        self.name_filters.append((name, name_prefix))
        # ``notes_named`` answers the resolver's exact-name listing when a
        # test needs it to differ from the prefix one; otherwise ``notes``
        # answers every listing.
        if name is not None and "notes_named" in self._canned:
            return self._result("notes_named")
        return self._result("notes")

    def get_artifact_detail(self, artifact_id):
        self.calls.append(("get_artifact_detail", artifact_id))
        return self._result("detail")

    def create_artifact(self, *, name, body, principal=None):
        self.calls.append(("create_artifact", name, body, principal))
        return self._result("create")

    def edit_artifact(self, artifact_id, *, name=None, body=None, principal=None, if_match=None):
        self.calls.append(("edit_artifact", artifact_id, name, body, principal, if_match))
        # ``edit`` may be a single canned result/exception (the common
        # case) or a list, consumed one per call in order — needed for the
        # conflict-then-retry tests, where the first call must 412 and a
        # later one must succeed.
        value = self._canned.get("edit")
        if isinstance(value, list):
            value = value[self._edit_call_count]
            self._edit_call_count += 1
            if isinstance(value, Exception):
                raise value
            return value
        return self._result("edit")

    def delete_artifact(self, artifact_id, *, principal=None):
        self.calls.append(("delete_artifact", artifact_id, principal))
        return self._result("delete")


@pytest.fixture
def patch_api(monkeypatch):
    _FakeNotesApi.instances.clear()

    def install(**canned):
        def factory(**kwargs):
            return _FakeNotesApi(**canned, **kwargs)

        monkeypatch.setattr(api_client, "ApiClient", factory)

    return install


# ---------------------------------------------------------------------------
# list


def test_notes_list_renders_rows(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(notes=_note_list(_note()))
    rc = cli.main(["notes", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Packing list" in out
    assert _NID_PACKING in out
    call = _FakeNotesApi.instances[-1].calls[-1]
    assert call == ("list_artifacts", None, "inline_text", None, None, None)


def test_notes_list_prints_labels_verbatim(tmp_home, patch_api, capsys) -> None:
    """Labels are user data interpolated beside Rich markup: a plain label
    must not be swallowed as a style tag, and a markup-shaped one must
    neither break the render nor restyle the line."""
    _login(tmp_home)
    patch_api(notes=_note_list(_note(labels=["recipe", "[/dim]", "[bold]x"])))
    rc = cli.main(["notes", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[recipe, [/dim], [bold]x]" in out


def test_notes_list_empty(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(notes=_note_list())
    rc = cli.main(["notes", "list"])
    assert rc == 0
    assert "(no notes)" in capsys.readouterr().out


def test_notes_list_json_shape(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(notes=_note_list(_note(labels=["trip"])))
    rc = cli.main(["notes", "list", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["truncated"] is False
    assert payload["items"][0]["id"] == _NID_PACKING
    assert payload["items"][0]["labels"] == ["trip"]


def test_notes_list_passes_the_label_filter(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(notes=_note_list())
    rc = cli.main(["notes", "list", "--label", "trip"])
    assert rc == 0
    assert _FakeNotesApi.instances[-1].calls == [
        ("list_artifacts", None, "inline_text", None, "trip", None)
    ]


def test_notes_list_resolves_room_by_name(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(groups=_group_list(_group()), notes=_note_list())
    rc = cli.main(["notes", "list", "--room", "trip"])
    assert rc == 0
    calls = _FakeNotesApi.instances[-1].calls
    assert ("list_groups", False) in calls
    assert ("list_artifacts", _GID_TRIP, "inline_text", None, None, None) in calls


def test_notes_list_ambiguous_room_exits_2(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(
        groups=_group_list(
            _group(group_id=_GID_TRIP, display_name="Trip planning"),
            _group(group_id="00000000-0000-0000-0000-0000000000a2", display_name="Trip logistics"),
        )
    )
    rc = cli.main(["notes", "list", "--room", "trip"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "more than one room" in err
    assert not any(c[0] == "list_artifacts" for c in _FakeNotesApi.instances[-1].calls)


def test_notes_list_capability_off_via_room_errors(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(groups=_group_list(enabled=False))
    rc = cli.main(["notes", "list", "--room", "trip"])
    assert rc == 1
    assert "not enabled" in capsys.readouterr().err


def test_notes_list_404_prints_the_uniform_message(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(notes=api_client.ApiError(status=404, body="", detail="not found"))
    rc = cli.main(["notes", "list", "--room", _GID_TRIP])
    assert rc == 2
    assert "no such room or note, or you are not a member" in capsys.readouterr().err


def test_notes_list_503_prints_retry_message(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(notes=api_client.ApiError(status=503, body="", detail="down"))
    rc = cli.main(["notes", "list", "--room", _GID_TRIP])
    assert rc == 1
    assert "membership check unavailable, try again" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# search


def test_notes_search_sends_the_query_and_filters(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(notes=_note_list())
    rc = cli.main(["notes", "search", "flights", "to", "paris"])
    assert rc == 0
    assert _FakeNotesApi.instances[-1].calls == [
        ("list_artifacts", None, "inline_text", "flights to paris", None, None)
    ]


def test_notes_search_empty_query_exits_2_without_a_call(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api()
    rc = cli.main(["notes", "search", "   "])
    assert rc == 2
    assert "cannot be empty" in capsys.readouterr().err
    assert _FakeNotesApi.instances == []


def test_notes_search_renders_results(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(notes=_note_list(_note(name="Packing list", summary="Bring sunscreen")))
    rc = cli.main(["notes", "search", "packing"])
    assert rc == 0
    assert "Packing list" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# show


def test_notes_show_by_uuid_prints_the_raw_body(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(detail=_detail(body="- sunscreen\n- passport\n"))
    rc = cli.main(["notes", "show", _NID_PACKING])
    assert rc == 0
    out = capsys.readouterr().out
    # Byte-exact: no header, no trailing newline added beyond what's stored.
    assert out == "- sunscreen\n- passport\n"
    calls = _FakeNotesApi.instances[-1].calls
    assert calls == [("get_artifact_detail", _NID_PACKING)]


def test_notes_show_a_uuid_never_lists_to_resolve_it(tmp_home, patch_api) -> None:
    """A bare UUID passes straight through — same contract as
    ``_resolve_group``/``_resolve_task_id`` — so no ``list_artifacts`` call
    is spent just to resolve it."""
    _login(tmp_home)
    patch_api(detail=_detail())
    cli.main(["notes", "show", _NID_PACKING])
    assert not any(c[0] == "list_artifacts" for c in _FakeNotesApi.instances[-1].calls)


def test_notes_show_by_unique_name_resolves_then_reads(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(
        notes=_note_list(_note(name="Packing list", id=_NID_PACKING)),
        detail=_detail(body="hi"),
    )
    rc = cli.main(["notes", "show", "Packing list"])
    assert rc == 0
    assert capsys.readouterr().out == "hi"
    calls = _FakeNotesApi.instances[-1].calls
    assert ("get_artifact_detail", _NID_PACKING) in calls


def test_notes_show_exact_name_resolves_in_one_listing(tmp_home, patch_api) -> None:
    """An exact name is looked up with ``name`` (never ``q``: the route has
    no offset, so body mentions must not fill the one page) and, when found,
    no prefix listing follows."""
    _login(tmp_home)
    patch_api(notes=_note_list(_note(name="Packing list")), detail=_detail())
    rc = cli.main(["notes", "show", "Packing list"])
    assert rc == 0
    fake = _FakeNotesApi.instances[-1]
    lists = [c for c in fake.calls if c[0] == "list_artifacts"]
    assert lists == [("list_artifacts", None, "inline_text", None, None, 200)]
    assert fake.name_filters == [("Packing list", None)]


def test_notes_show_exact_name_wins_however_many_names_share_its_prefix(
    tmp_home, patch_api
) -> None:
    """The exact listing runs first, so a crowded (truncated) prefix page
    is never consulted when a note is named exactly the value."""
    _login(tmp_home)
    patch_api(
        notes_named=_note_list(_note(name="Runbook", id=_NID_RUNBOOK_1)),
        notes=_note_list(_note(name="Runbook v2", id=_NID_RUNBOOK_2), truncated=True),
        detail=_detail(body="steps"),
    )
    rc = cli.main(["notes", "show", "runbook"])
    assert rc == 0
    fake = _FakeNotesApi.instances[-1]
    assert fake.name_filters == [("runbook", None)]
    assert ("get_artifact_detail", _NID_RUNBOOK_1) in fake.calls


def test_notes_show_resolves_a_unique_prefix(tmp_home, patch_api) -> None:
    """Nothing is named exactly ``Runb``, so a ``name_prefix`` listing
    follows — the ``_NOTE_ARG_HELP`` promise, same as groups and topics."""
    _login(tmp_home)
    patch_api(
        notes_named=_note_list(),
        notes=_note_list(_note(name="Runbook", id=_NID_RUNBOOK_1)),
        detail=_detail(body="x"),
    )
    rc = cli.main(["notes", "show", "Runb"])
    assert rc == 0
    fake = _FakeNotesApi.instances[-1]
    assert fake.name_filters == [("Runb", None), (None, "Runb")]
    assert ("get_artifact_detail", _NID_RUNBOOK_1) in fake.calls


def test_notes_show_ambiguous_prefix_exits_2(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(
        notes_named=_note_list(),
        notes=_note_list(
            _note(name="Runbook", id=_NID_RUNBOOK_1),
            _note(name="Runbook v2", id=_NID_RUNBOOK_2),
        ),
    )
    rc = cli.main(["notes", "show", "Run"])
    assert rc == 2
    assert "more than one note" in capsys.readouterr().err
    assert not any(c[0] == "get_artifact_detail" for c in _FakeNotesApi.instances[-1].calls)


def test_notes_show_truncated_prefix_listing_refuses(tmp_home, patch_api, capsys) -> None:
    """A unique prefix inside a truncated page may not be unique."""
    _login(tmp_home)
    patch_api(
        notes_named=_note_list(),
        notes=_note_list(_note(name="Runbook", id=_NID_RUNBOOK_1), truncated=True),
    )
    rc = cli.main(["notes", "show", "Runb"])
    assert rc == 2
    assert "too many notes" in capsys.readouterr().err
    assert not any(c[0] == "get_artifact_detail" for c in _FakeNotesApi.instances[-1].calls)


def test_notes_show_truncated_resolution_refuses_rather_than_guessing(
    tmp_home, patch_api, capsys
) -> None:
    """A truncated exact-name page means more same-named notes than fit, so
    any pick could be the wrong one: refuse and point at the id instead."""
    _login(tmp_home)
    patch_api(notes=_note_list(_note(name="Packing list"), truncated=True))
    rc = cli.main(["notes", "show", "Packing list"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "too many notes" in err
    assert "id" in err
    assert not any(c[0] == "get_artifact_detail" for c in _FakeNotesApi.instances[-1].calls)


def test_notes_show_ambiguous_name_exits_2_and_lists_candidates(
    tmp_home, patch_api, capsys
) -> None:
    _login(tmp_home)
    patch_api(
        notes=_note_list(
            _note(name="Runbook", id=_NID_RUNBOOK_1),
            _note(name="Runbook", id=_NID_RUNBOOK_2),
        )
    )
    rc = cli.main(["notes", "show", "Runbook"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "more than one note" in err
    assert _NID_RUNBOOK_1 in err
    assert _NID_RUNBOOK_2 in err
    assert not any(c[0] == "get_artifact_detail" for c in _FakeNotesApi.instances[-1].calls)


def test_notes_show_unknown_name_exits_2(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(notes=_note_list())
    rc = cli.main(["notes", "show", "nope"])
    assert rc == 2
    assert "no note named" in capsys.readouterr().err


def test_notes_show_json_includes_etag_and_shared(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(detail=_detail(shared=True, etag="xyz789", labels=["trip"]))
    rc = cli.main(["notes", "show", _NID_PACKING, "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["etag"] == "xyz789"
    assert payload["shared"] is True
    assert payload["labels"] == ["trip"]


def test_notes_show_other_kind_is_treated_as_not_a_note(tmp_home, patch_api, capsys) -> None:
    """A UUID that resolves to some other artifact kind (an image, a link)
    answers the same "doesn't exist for you" message a 404 would."""
    _login(tmp_home)
    patch_api(detail=_detail(kind="image"))
    rc = cli.main(["notes", "show", _NID_PACKING])
    assert rc == 2
    assert "no such room or note, or you are not a member" in capsys.readouterr().err


def test_notes_show_does_not_forward_room_to_the_detail_call(tmp_home, patch_api, capsys) -> None:
    """``--room`` scopes name resolution only — the detail read itself is
    viewer-scoped server-side and takes no principal."""
    _login(tmp_home)
    patch_api(groups=_group_list(_group()), detail=_detail())
    rc = cli.main(["notes", "show", _NID_PACKING, "--room", "trip"])
    assert rc == 0
    calls = _FakeNotesApi.instances[-1].calls
    assert ("get_artifact_detail", _NID_PACKING) in calls
    # A bare UUID resolves without even needing the room list.
    assert not any(c[0] == "list_artifacts" for c in calls)


def test_notes_show_404_prints_the_uniform_message(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(detail=api_client.ApiError(status=404, body="", detail="not found"))
    rc = cli.main(["notes", "show", _NID_PACKING])
    assert rc == 2
    assert "no such room or note, or you are not a member" in capsys.readouterr().err


def test_notes_show_503_prints_retry_message(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(detail=api_client.ApiError(status=503, body="", detail="down"))
    rc = cli.main(["notes", "show", _NID_PACKING])
    assert rc == 1
    assert "membership check unavailable, try again" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# new


def _create_result(**over) -> api_client.ArtifactCreateResult:
    base = {"id": _NID_PACKING, "name": "Packing list", "etag": "abc123"}
    base.update(over)
    return api_client.ArtifactCreateResult(**base)


def test_notes_new_editor_round_trip_creates_the_note(
    tmp_home, patch_api, capsys, monkeypatch
) -> None:
    _login(tmp_home)
    patch_api(create=_create_result())
    monkeypatch.setattr(cli, "_open_in_editor", lambda initial: "- sunscreen\n")
    rc = cli.main(["notes", "create", "Packing list"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "Created 'Packing list'" in captured.err
    assert _NID_PACKING in captured.out
    calls = _FakeNotesApi.instances[-1].calls
    assert calls == [("create_artifact", "Packing list", "- sunscreen\n", None)]


def test_notes_new_aborted_editor_exits_2_without_a_call(
    tmp_home, patch_api, capsys, monkeypatch
) -> None:
    _login(tmp_home)
    patch_api()
    monkeypatch.setattr(cli, "_open_in_editor", lambda initial: None)
    rc = cli.main(["notes", "create", "Packing list"])
    assert rc == 2
    assert not any(c[0] == "create_artifact" for c in _FakeNotesApi.instances[-1].calls)


def test_notes_new_empty_body_exits_2_without_a_call(
    tmp_home, patch_api, capsys, monkeypatch
) -> None:
    _login(tmp_home)
    patch_api()
    monkeypatch.setattr(cli, "_open_in_editor", lambda initial: "   \n")
    rc = cli.main(["notes", "create", "Packing list"])
    assert rc == 2
    assert "empty" in capsys.readouterr().err
    assert not any(c[0] == "create_artifact" for c in _FakeNotesApi.instances[-1].calls)


def test_notes_new_reads_body_from_file(tmp_home, patch_api, tmp_path) -> None:
    _login(tmp_home)
    patch_api(create=_create_result())
    body_path = tmp_path / "body.md"
    body_path.write_text("- passport\n", encoding="utf-8")
    rc = cli.main(["notes", "create", "Packing list", "--file", str(body_path)])
    assert rc == 0
    calls = _FakeNotesApi.instances[-1].calls
    assert calls == [("create_artifact", "Packing list", "- passport\n", None)]


def test_notes_new_reads_body_from_stdin(tmp_home, patch_api, monkeypatch) -> None:
    _login(tmp_home)
    patch_api(create=_create_result())
    monkeypatch.setattr(sys, "stdin", io.StringIO("- passport\n"))
    rc = cli.main(["notes", "create", "Packing list", "--file", "-"])
    assert rc == 0
    calls = _FakeNotesApi.instances[-1].calls
    assert calls == [("create_artifact", "Packing list", "- passport\n", None)]


def test_notes_new_unreadable_file_exits_2_before_any_client_is_built(
    tmp_home, patch_api, capsys, tmp_path
) -> None:
    _login(tmp_home)
    patch_api()
    missing = tmp_path / "nope.md"
    rc = cli.main(["notes", "create", "Packing list", "--file", str(missing)])
    assert rc == 2
    assert "couldn't read" in capsys.readouterr().err
    assert _FakeNotesApi.instances == []


def test_notes_new_resolves_room_by_name(tmp_home, patch_api, monkeypatch) -> None:
    _login(tmp_home)
    patch_api(groups=_group_list(_group()), create=_create_result())
    monkeypatch.setattr(cli, "_open_in_editor", lambda initial: "body\n")
    rc = cli.main(["notes", "create", "Packing list", "--room", "trip"])
    assert rc == 0
    calls = _FakeNotesApi.instances[-1].calls
    assert ("create_artifact", "Packing list", "body\n", _GID_TRIP) in calls


def test_notes_new_404_prints_the_uniform_message(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(groups=api_client.ApiError(status=404, body="", detail="not found"))
    rc = cli.main(["notes", "create", "Packing list", "--room", "trip"])
    assert rc == 2
    assert "no such room or note, or you are not a member" in capsys.readouterr().err


def test_notes_new_503_prints_retry_message(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(groups=api_client.ApiError(status=503, body="", detail="down"))
    rc = cli.main(["notes", "create", "Packing list", "--room", "trip"])
    assert rc == 1
    assert "membership check unavailable, try again" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# edit


def test_notes_edit_unchanged_text_sends_nothing(tmp_home, patch_api, capsys, monkeypatch) -> None:
    _login(tmp_home)
    patch_api(detail=_detail(body="same\n"))
    monkeypatch.setattr(cli, "_open_in_editor", lambda initial: "same\n")
    rc = cli.main(["notes", "edit", _NID_PACKING])
    assert rc == 0
    assert "Nothing to change." in capsys.readouterr().err
    assert not any(c[0] == "edit_artifact" for c in _FakeNotesApi.instances[-1].calls)


def test_notes_edit_editor_trailing_newline_tolerance_sends_nothing(
    tmp_home, patch_api, monkeypatch
) -> None:
    """Vi appends a trailing newline on save — that alone must not count
    as a change (``_texts_match``)."""
    _login(tmp_home)
    patch_api(detail=_detail(body="same"))
    monkeypatch.setattr(cli, "_open_in_editor", lambda initial: "same\n")
    rc = cli.main(["notes", "edit", _NID_PACKING])
    assert rc == 0
    assert not any(c[0] == "edit_artifact" for c in _FakeNotesApi.instances[-1].calls)


def test_notes_edit_changed_body_sends_it_with_the_fetched_etag(
    tmp_home, patch_api, monkeypatch
) -> None:
    _login(tmp_home)
    patch_api(detail=_detail(body="old\n", etag="abc123"), edit=_detail(body="new\n"))
    monkeypatch.setattr(cli, "_open_in_editor", lambda initial: "new\n")
    rc = cli.main(["notes", "edit", _NID_PACKING])
    assert rc == 0
    calls = [c for c in _FakeNotesApi.instances[-1].calls if c[0] == "edit_artifact"]
    assert calls == [("edit_artifact", _NID_PACKING, None, "new\n", None, "abc123")]


def test_notes_edit_rename_only_sends_name_with_no_body(tmp_home, patch_api, monkeypatch) -> None:
    _login(tmp_home)
    patch_api(
        detail=_detail(name="Packing list", body="same\n", etag="abc123"),
        edit=_detail(name="Renamed"),
    )
    monkeypatch.setattr(cli, "_open_in_editor", lambda initial: "same\n")
    rc = cli.main(["notes", "edit", _NID_PACKING, "--name", "Renamed"])
    assert rc == 0
    calls = [c for c in _FakeNotesApi.instances[-1].calls if c[0] == "edit_artifact"]
    assert calls == [("edit_artifact", _NID_PACKING, "Renamed", None, None, "abc123")]


def test_notes_edit_rename_to_same_name_and_unchanged_body_is_a_noop(
    tmp_home, patch_api, capsys, monkeypatch
) -> None:
    _login(tmp_home)
    patch_api(detail=_detail(name="Packing list", body="same\n"))
    monkeypatch.setattr(cli, "_open_in_editor", lambda initial: "same\n")
    rc = cli.main(["notes", "edit", _NID_PACKING, "--name", "Packing list"])
    assert rc == 0
    assert "Nothing to change." in capsys.readouterr().err
    assert not any(c[0] == "edit_artifact" for c in _FakeNotesApi.instances[-1].calls)


def test_notes_edit_empty_body_exits_2_without_a_call(
    tmp_home, patch_api, capsys, monkeypatch
) -> None:
    _login(tmp_home)
    patch_api(detail=_detail(body="old\n"))
    monkeypatch.setattr(cli, "_open_in_editor", lambda initial: "   \n")
    rc = cli.main(["notes", "edit", _NID_PACKING])
    assert rc == 2
    assert "empty" in capsys.readouterr().err
    assert not any(c[0] == "edit_artifact" for c in _FakeNotesApi.instances[-1].calls)


def test_notes_edit_aborted_editor_exits_2(tmp_home, patch_api, monkeypatch) -> None:
    _login(tmp_home)
    patch_api(detail=_detail(body="old\n"))
    monkeypatch.setattr(cli, "_open_in_editor", lambda initial: None)
    rc = cli.main(["notes", "edit", _NID_PACKING])
    assert rc == 2
    assert not any(c[0] == "edit_artifact" for c in _FakeNotesApi.instances[-1].calls)


def test_notes_edit_file_uses_the_freshly_fetched_etag(tmp_home, patch_api, tmp_path) -> None:
    _login(tmp_home)
    patch_api(detail=_detail(body="old\n", etag="fresh1"), edit=_detail(body="new\n"))
    body_path = tmp_path / "body.md"
    body_path.write_text("new\n", encoding="utf-8")
    rc = cli.main(["notes", "edit", _NID_PACKING, "--file", str(body_path)])
    assert rc == 0
    calls = [c for c in _FakeNotesApi.instances[-1].calls if c[0] == "edit_artifact"]
    assert calls == [("edit_artifact", _NID_PACKING, None, "new\n", None, "fresh1")]


def test_notes_edit_stdin_body(tmp_home, patch_api, monkeypatch) -> None:
    _login(tmp_home)
    patch_api(detail=_detail(body="old\n"), edit=_detail(body="new\n"))
    monkeypatch.setattr(sys, "stdin", io.StringIO("new\n"))
    rc = cli.main(["notes", "edit", _NID_PACKING, "--file", "-"])
    assert rc == 0
    calls = [c for c in _FakeNotesApi.instances[-1].calls if c[0] == "edit_artifact"]
    assert calls[0][3] == "new\n"


def test_notes_edit_if_match_overrides_the_fetched_etag(tmp_home, patch_api, monkeypatch) -> None:
    _login(tmp_home)
    patch_api(detail=_detail(body="old\n", etag="fresh1"), edit=_detail(body="new\n"))
    monkeypatch.setattr(cli, "_open_in_editor", lambda initial: "new\n")
    rc = cli.main(["notes", "edit", _NID_PACKING, "--if-match", "pinned"])
    assert rc == 0
    calls = [c for c in _FakeNotesApi.instances[-1].calls if c[0] == "edit_artifact"]
    assert calls == [("edit_artifact", _NID_PACKING, None, "new\n", None, "pinned")]


def test_notes_edit_force_sends_no_precondition(tmp_home, patch_api, monkeypatch) -> None:
    _login(tmp_home)
    patch_api(detail=_detail(body="old\n", etag="fresh1"), edit=_detail(body="new\n"))
    monkeypatch.setattr(cli, "_open_in_editor", lambda initial: "new\n")
    rc = cli.main(["notes", "edit", _NID_PACKING, "--force"])
    assert rc == 0
    calls = [c for c in _FakeNotesApi.instances[-1].calls if c[0] == "edit_artifact"]
    assert calls == [("edit_artifact", _NID_PACKING, None, "new\n", None, None)]


def test_notes_edit_if_match_and_force_are_mutually_exclusive() -> None:
    parser, _ = cli._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["notes", "edit", _NID_PACKING, "--if-match", "x", "--force"])


def test_notes_edit_conflict_off_tty_exits_1_with_saved_path(
    tmp_home, patch_api, capsys, monkeypatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    _login(tmp_home)
    conflict = api_client.ArtifactConflictError(
        412,
        "",
        detail="conflict",
        current_body="server text\n",
        current_etag="newetag1",
        current_updated_at="2026-09-27T00:00:00+00:00",
    )
    patch_api(detail=_detail(name="Packing list", body="old\n", etag="stale1"), edit=conflict)
    monkeypatch.setattr(cli, "_open_in_editor", lambda initial: "mine text\n")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    rc = cli.main(["notes", "edit", _NID_PACKING])
    assert rc == 1
    err = capsys.readouterr().err
    assert "your text is saved at" in err
    saved = tmp_path / "packing-list.newetag1.mine.md"
    assert saved.exists()
    assert saved.read_text(encoding="utf-8") == "mine text\n"
    assert "-server text" in err or "server text" in err


def test_notes_edit_conflict_on_tty_overwrite_retries_with_new_etag(
    tmp_home, patch_api, monkeypatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    _login(tmp_home)
    conflict = api_client.ArtifactConflictError(
        412,
        "",
        detail="conflict",
        current_body="server text\n",
        current_etag="newetag1",
        current_updated_at="2026-09-27T00:00:00+00:00",
    )
    patch_api(
        detail=_detail(name="Packing list", body="old\n", etag="stale1"),
        edit=[conflict, _detail(body="mine text\n")],
    )
    monkeypatch.setattr(cli, "_open_in_editor", lambda initial: "mine text\n")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_prompt", lambda prompt: "o")
    rc = cli.main(["notes", "edit", _NID_PACKING])
    assert rc == 0
    calls = [c for c in _FakeNotesApi.instances[-1].calls if c[0] == "edit_artifact"]
    assert len(calls) == 2
    assert calls[0][5] == "stale1"
    assert calls[1][5] == "newetag1"
    assert calls[1][3] == "mine text\n"


def test_notes_edit_conflict_on_tty_reopen_seeds_the_editor_with_users_text(
    tmp_home, patch_api, monkeypatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    _login(tmp_home)
    conflict = api_client.ArtifactConflictError(
        412,
        "",
        detail="conflict",
        current_body="server text\n",
        current_etag="newetag1",
        current_updated_at="2026-09-27T00:00:00+00:00",
    )
    patch_api(
        detail=_detail(name="Packing list", body="old\n", etag="stale1"),
        edit=[conflict, _detail(body="mine text v2\n")],
    )
    editor_calls: list[str] = []

    def fake_editor(initial):
        editor_calls.append(initial)
        return "mine text\n" if len(editor_calls) == 1 else "mine text v2\n"

    monkeypatch.setattr(cli, "_open_in_editor", fake_editor)
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_prompt", lambda prompt: "r")
    rc = cli.main(["notes", "edit", _NID_PACKING])
    assert rc == 0
    assert editor_calls == ["old\n", "mine text\n"]
    calls = [c for c in _FakeNotesApi.instances[-1].calls if c[0] == "edit_artifact"]
    assert len(calls) == 2
    assert calls[1][3] == "mine text v2\n"
    assert calls[1][5] == "newetag1"


def test_notes_edit_conflict_on_tty_quit_exits_1(
    tmp_home, patch_api, capsys, monkeypatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    _login(tmp_home)
    conflict = api_client.ArtifactConflictError(
        412,
        "",
        detail="conflict",
        current_body="server text\n",
        current_etag="newetag1",
        current_updated_at="2026-09-27T00:00:00+00:00",
    )
    patch_api(detail=_detail(name="Packing list", body="old\n", etag="stale1"), edit=conflict)
    monkeypatch.setattr(cli, "_open_in_editor", lambda initial: "mine text\n")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_prompt", lambda prompt: "q")
    rc = cli.main(["notes", "edit", _NID_PACKING])
    assert rc == 1
    assert "Aborted." in capsys.readouterr().err
    calls = [c for c in _FakeNotesApi.instances[-1].calls if c[0] == "edit_artifact"]
    assert len(calls) == 1


def _conflict(body="server text\n", etag="newetag1") -> api_client.ArtifactConflictError:
    return api_client.ArtifactConflictError(
        412,
        "",
        detail="conflict",
        current_body=body,
        current_etag=etag,
        current_updated_at="2026-09-27T00:00:00+00:00",
    )


def test_notes_edit_file_trailing_newline_is_a_real_change(tmp_home, patch_api, tmp_path) -> None:
    """``--file`` is byte-exact: the editor's one-newline tolerance must not
    swallow a scripted change that only appends a newline."""
    _login(tmp_home)
    patch_api(detail=_detail(body="same", etag="fresh1"), edit=_detail(body="same\n"))
    body_path = tmp_path / "body.md"
    body_path.write_text("same\n", encoding="utf-8")
    rc = cli.main(["notes", "edit", _NID_PACKING, "--file", str(body_path)])
    assert rc == 0
    calls = [c for c in _FakeNotesApi.instances[-1].calls if c[0] == "edit_artifact"]
    assert calls == [("edit_artifact", _NID_PACKING, None, "same\n", None, "fresh1")]


def test_notes_edit_file_identical_body_sends_nothing(
    tmp_home, patch_api, capsys, tmp_path
) -> None:
    _login(tmp_home)
    patch_api(detail=_detail(body="same\n"))
    body_path = tmp_path / "body.md"
    body_path.write_text("same\n", encoding="utf-8")
    rc = cli.main(["notes", "edit", _NID_PACKING, "--file", str(body_path)])
    assert rc == 0
    assert "Nothing to change." in capsys.readouterr().err
    assert not any(c[0] == "edit_artifact" for c in _FakeNotesApi.instances[-1].calls)


def test_notes_edit_invalid_utf8_file_exits_2_before_any_client_is_built(
    tmp_home, patch_api, capsys, tmp_path
) -> None:
    _login(tmp_home)
    patch_api(detail=_detail())
    body_path = tmp_path / "body.md"
    body_path.write_bytes(b"\xff\xfe not utf-8")
    rc = cli.main(["notes", "edit", _NID_PACKING, "--file", str(body_path)])
    assert rc == 2
    assert "couldn't read" in capsys.readouterr().err
    assert _FakeNotesApi.instances == []


def test_notes_new_invalid_utf8_stdin_exits_2_before_any_client_is_built(
    tmp_home, patch_api, capsys, monkeypatch
) -> None:
    _login(tmp_home)
    patch_api(create=_create_result())
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"\xff"), encoding="utf-8"))
    rc = cli.main(["notes", "create", "Trip plan", "--file", "-"])
    assert rc == 2
    assert "couldn't read stdin" in capsys.readouterr().err
    assert _FakeNotesApi.instances == []


def test_notes_edit_rename_only_conflict_overwrite_never_sends_the_body(
    tmp_home, patch_api, capsys, monkeypatch, tmp_path
) -> None:
    """A rename-only edit that loses a race to someone's body edit must
    resend the rename alone — the old body would clobber theirs."""
    monkeypatch.chdir(tmp_path)
    _login(tmp_home)
    patch_api(
        detail=_detail(name="Packing list", body="same\n", etag="stale1"),
        edit=[_conflict(body="their new text\n"), _detail(name="Renamed")],
    )
    monkeypatch.setattr(cli, "_open_in_editor", lambda initial: "same\n")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    prompts: list[str] = []
    monkeypatch.setattr(cli, "_prompt", lambda prompt: prompts.append(prompt) or "o")
    rc = cli.main(["notes", "edit", _NID_PACKING, "--name", "Renamed"])
    assert rc == 0
    calls = [c for c in _FakeNotesApi.instances[-1].calls if c[0] == "edit_artifact"]
    assert calls == [
        ("edit_artifact", _NID_PACKING, "Renamed", None, None, "stale1"),
        ("edit_artifact", _NID_PACKING, "Renamed", None, None, "newetag1"),
    ]
    assert "re-open" not in prompts[0]
    # No text of the user's to save or diff.
    assert list(tmp_path.glob("*.mine*.md")) == []
    assert "their new text" not in capsys.readouterr().err


def test_notes_edit_rename_only_conflict_off_tty_exits_1(
    tmp_home, patch_api, capsys, monkeypatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    _login(tmp_home)
    patch_api(detail=_detail(body="same\n", etag="stale1"), edit=_conflict())
    monkeypatch.setattr(cli, "_open_in_editor", lambda initial: "same\n")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    rc = cli.main(["notes", "edit", _NID_PACKING, "--name", "Renamed"])
    assert rc == 1
    assert "that note has changed" in capsys.readouterr().err
    assert list(tmp_path.glob("*.mine*.md")) == []


def test_notes_edit_conflict_never_overwrites_an_earlier_saved_copy(
    tmp_home, patch_api, capsys, monkeypatch, tmp_path
) -> None:
    """Two attempts against the same server version save to two files."""
    monkeypatch.chdir(tmp_path)
    earlier = tmp_path / "packing-list.newetag1.mine.md"
    earlier.write_text("earlier attempt\n", encoding="utf-8")
    _login(tmp_home)
    patch_api(detail=_detail(name="Packing list", body="old\n", etag="stale1"), edit=_conflict())
    monkeypatch.setattr(cli, "_open_in_editor", lambda initial: "mine text\n")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    rc = cli.main(["notes", "edit", _NID_PACKING])
    assert rc == 1
    assert earlier.read_text(encoding="utf-8") == "earlier attempt\n"
    second = tmp_path / "packing-list.newetag1.mine.1.md"
    assert second.read_text(encoding="utf-8") == "mine text\n"
    assert "packing-list.newetag1.mine.1.md" in capsys.readouterr().err


def test_notes_edit_conflict_copy_falls_back_to_the_temp_dir(
    tmp_home, patch_api, capsys, monkeypatch, tmp_path
) -> None:
    """An unwritable working directory must not lose the user's text."""
    import os

    cwd = tmp_path / "readonly"
    cwd.mkdir()
    fallback = tmp_path / "tmpdir"
    fallback.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(cli, "_conflict_fallback_dir", lambda: str(fallback))
    real_open = os.open

    def refuse_cwd(path, flags, mode=0o777, **kwargs):
        if os.path.dirname(os.fspath(path)) == "":
            raise PermissionError("read-only")
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(os, "open", refuse_cwd)
    _login(tmp_home)
    patch_api(detail=_detail(name="Packing list", body="old\n", etag="stale1"), edit=_conflict())
    monkeypatch.setattr(cli, "_open_in_editor", lambda initial: "mine text\n")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    rc = cli.main(["notes", "edit", _NID_PACKING])
    assert rc == 1
    saved = fallback / "packing-list.newetag1.mine.md"
    assert saved.read_text(encoding="utf-8") == "mine text\n"
    assert str(saved) in capsys.readouterr().err


def test_notes_edit_conflict_copy_write_failure_falls_back_and_keeps_the_flow(
    tmp_home, patch_api, capsys, monkeypatch, tmp_path
) -> None:
    """Opened but not writable (disk full): the partial file goes, the temp
    dir gets the copy, and the diff and prompt still run."""
    import os

    cwd = tmp_path / "full"
    cwd.mkdir()
    fallback = tmp_path / "tmpdir"
    fallback.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(cli, "_conflict_fallback_dir", lambda: str(fallback))
    real_fdopen = os.fdopen
    cwd_fds: set[int] = set()
    real_open = os.open

    def tracking_open(path, flags, mode=0o777, **kwargs):
        fd = real_open(path, flags, mode, **kwargs)
        if os.path.dirname(os.fspath(path)) == "":
            cwd_fds.add(fd)
        return fd

    class _FullDisk:
        def __init__(self, fd):
            self._handle = real_fdopen(fd, "w", encoding="utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            self._handle.close()

        def write(self, text):
            raise OSError(28, "No space left on device")

    def fdopen(fd, *args, **kwargs):
        if fd in cwd_fds:
            cwd_fds.discard(fd)  # the number is reused by the next open
            return _FullDisk(fd)
        return real_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "fdopen", fdopen)
    _login(tmp_home)
    patch_api(detail=_detail(name="Packing list", body="old\n", etag="stale1"), edit=_conflict())
    monkeypatch.setattr(cli, "_open_in_editor", lambda initial: "mine text\n")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_prompt", lambda prompt: "q")
    rc = cli.main(["notes", "edit", _NID_PACKING])
    assert rc == 1
    assert list(cwd.iterdir()) == []
    saved = fallback / "packing-list.newetag1.mine.md"
    assert saved.read_text(encoding="utf-8") == "mine text\n"
    err = capsys.readouterr().err
    assert "+mine text" in err
    assert "Aborted." in err


def _refuse_every_conflict_copy(monkeypatch, tmp_path) -> None:
    """Neither the working directory nor the temp dir accepts the ``.mine`` copy."""
    import os

    fallback = tmp_path / "tmpdir"
    fallback.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_conflict_fallback_dir", lambda: str(fallback))
    real_open = os.open

    def refuse_copies(path, flags, mode=0o777, **kwargs):
        if ".mine" in os.fspath(path):
            raise PermissionError("read-only")
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(os, "open", refuse_copies)


def test_notes_edit_json_conflict_with_no_writable_copy_is_one_j8_line(
    tmp_home, patch_api, capsys, monkeypatch, tmp_path
) -> None:
    """J8: the save failure folds into the single ``conflict`` line — no text line before it."""
    monkeypatch.delenv("PEPPER_JSON", raising=False)
    body_path = tmp_path / "body.md"
    body_path.write_text("mine text\n", encoding="utf-8")
    _login(tmp_home)
    patch_api(detail=_detail(name="Packing list", body="old\n", etag="stale1"), edit=_conflict())
    _refuse_every_conflict_copy(monkeypatch, tmp_path)
    rc = cli.main(["notes", "edit", _NID_PACKING, "--file", str(body_path), "--json"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    lines = [ln for ln in captured.err.splitlines() if ln.strip()]
    assert len(lines) == 1
    error = json.loads(lines[0])["error"]
    assert error["code"] == "conflict"
    assert error["status"] == 412
    assert "couldn't save your text to a file" in error["message"]


def test_notes_edit_conflict_with_no_writable_copy_still_says_why_in_text(
    tmp_home, patch_api, capsys, monkeypatch, tmp_path
) -> None:
    """The human path keeps its own ``couldn't save`` line and the diff."""
    _login(tmp_home)
    patch_api(detail=_detail(name="Packing list", body="old\n", etag="stale1"), edit=_conflict())
    monkeypatch.setattr(cli, "_open_in_editor", lambda initial: "mine text\n")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    _refuse_every_conflict_copy(monkeypatch, tmp_path)
    rc = cli.main(["notes", "edit", _NID_PACKING])
    assert rc == 1
    err = capsys.readouterr().err
    assert "couldn't save your text to a file" in err
    assert "+mine text" in err
    assert "your text is in the diff above" in err


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX mode bits; the Windows model is tested below (#45)"
)
def test_notes_edit_conflict_copy_is_owner_only_whatever_the_umask(
    tmp_home, patch_api, monkeypatch, tmp_path
) -> None:
    """The copy can land in the shared temp dir; a note is private text."""
    import os

    monkeypatch.chdir(tmp_path)
    old_umask = os.umask(0o022)
    try:
        _login(tmp_home)
        patch_api(
            detail=_detail(name="Packing list", body="old\n", etag="stale1"), edit=_conflict()
        )
        monkeypatch.setattr(cli, "_open_in_editor", lambda initial: "mine text\n")
        monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
        rc = cli.main(["notes", "edit", _NID_PACKING])
    finally:
        os.umask(old_umask)
    assert rc == 1
    saved = tmp_path / "packing-list.newetag1.mine.md"
    assert saved.stat().st_mode & 0o777 == 0o600


def test_conflict_fallback_dir_is_the_temp_dir_on_posix(monkeypatch) -> None:
    import tempfile

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: "/shared/tmp")
    assert cli._conflict_fallback_dir() == "/shared/tmp"


def test_conflict_fallback_dir_on_windows_is_per_user_not_temp(monkeypatch, tmp_path) -> None:
    """Windows ignores mode bits, so the folder is the protection: never %TEMP%."""
    import tempfile

    local = tmp_path / "Local"
    shared = tmp_path / "SharedTemp"
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(shared))
    assert cli._conflict_fallback_dir() == str(local / "pepper")
    assert (local / "pepper").is_dir()
    assert not shared.exists()


def test_conflict_fallback_dir_on_windows_without_localappdata_uses_the_profile(
    monkeypatch, tmp_home
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert cli._conflict_fallback_dir() == str(tmp_home / "AppData" / "Local" / "pepper")


def test_conflict_fallback_dir_on_windows_ignores_a_relative_localappdata(
    monkeypatch, tmp_home
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", "relative\\dir")
    assert cli._conflict_fallback_dir() == str(tmp_home / "AppData" / "Local" / "pepper")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only: real %LOCALAPPDATA% fallback")
def test_notes_edit_conflict_copy_on_windows_lands_in_cwd_or_the_per_user_folder(
    tmp_home, patch_api, capsys, monkeypatch, tmp_path
) -> None:
    """No ACL is set; the protection is where the copy lands (#45)."""
    import os

    local = tmp_path / "Local"
    cwd = tmp_path / "work"
    cwd.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.chdir(cwd)
    real_open = os.open

    def refuse_cwd(path, flags, mode=0o777, **kwargs):
        if os.path.dirname(os.fspath(path)) == "":
            raise PermissionError("read-only")
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(os, "open", refuse_cwd)
    _login(tmp_home)
    patch_api(detail=_detail(name="Packing list", body="old\n", etag="stale1"), edit=_conflict())
    monkeypatch.setattr(cli, "_open_in_editor", lambda initial: "mine text\n")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    rc = cli.main(["notes", "edit", _NID_PACKING])
    assert rc == 1
    saved = local / "pepper" / "packing-list.newetag1.mine.md"
    assert saved.read_text(encoding="utf-8") == "mine text\n"
    assert not saved.is_symlink()
    assert list(cwd.iterdir()) == []
    assert str(saved) in capsys.readouterr().err


def test_notes_edit_conflict_copy_never_writes_through_a_planted_symlink(
    tmp_home, patch_api, monkeypatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "victim.txt"
    target.write_text("untouched\n", encoding="utf-8")
    (tmp_path / "packing-list.newetag1.mine.md").symlink_to(target)
    _login(tmp_home)
    patch_api(detail=_detail(name="Packing list", body="old\n", etag="stale1"), edit=_conflict())
    monkeypatch.setattr(cli, "_open_in_editor", lambda initial: "mine text\n")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    rc = cli.main(["notes", "edit", _NID_PACKING])
    assert rc == 1
    assert target.read_text(encoding="utf-8") == "untouched\n"
    second = tmp_path / "packing-list.newetag1.mine.1.md"
    assert second.read_text(encoding="utf-8") == "mine text\n"


def test_notes_edit_conflict_reopen_to_blank_exits_2_without_a_retry(
    tmp_home, patch_api, capsys, monkeypatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    _login(tmp_home)
    patch_api(
        detail=_detail(name="Packing list", body="old\n", etag="stale1"),
        edit=[_conflict(), _detail(body="unreachable\n")],
    )
    editor_outputs = iter(["mine text\n", "  \n"])
    monkeypatch.setattr(cli, "_open_in_editor", lambda initial: next(editor_outputs))
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_prompt", lambda prompt: "r")
    rc = cli.main(["notes", "edit", _NID_PACKING])
    assert rc == 2
    assert "empty" in capsys.readouterr().err
    calls = [c for c in _FakeNotesApi.instances[-1].calls if c[0] == "edit_artifact"]
    assert len(calls) == 1


def test_notes_edit_404_prints_the_uniform_message(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(notes=api_client.ApiError(status=404, body="", detail="not found"))
    rc = cli.main(["notes", "edit", "nope"])
    assert rc == 2
    assert "no such room or note, or you are not a member" in capsys.readouterr().err


def test_notes_edit_503_prints_retry_message(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(notes=api_client.ApiError(status=503, body="", detail="down"))
    rc = cli.main(["notes", "edit", "nope"])
    assert rc == 1
    assert "membership check unavailable, try again" in capsys.readouterr().err


def test_notes_edit_other_kind_is_treated_as_not_a_note(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(detail=_detail(kind="image"))
    rc = cli.main(["notes", "edit", _NID_PACKING])
    assert rc == 2
    assert "no such room or note, or you are not a member" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# delete


def test_notes_delete_confirms_before_deleting(tmp_home, patch_api, capsys, monkeypatch) -> None:
    _login(tmp_home)
    patch_api(detail=_detail(name="Packing list"), delete="Packing list")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_prompt", lambda prompt: "y")
    rc = cli.main(["notes", "delete", _NID_PACKING])
    assert rc == 0
    assert "Deleted 'Packing list'" in capsys.readouterr().err
    assert any(c[0] == "delete_artifact" for c in _FakeNotesApi.instances[-1].calls)


def test_notes_delete_declining_the_prompt_aborts_without_deleting(
    tmp_home, patch_api, capsys, monkeypatch
) -> None:
    _login(tmp_home)
    patch_api(detail=_detail(name="Packing list"))
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_prompt", lambda prompt: "n")
    rc = cli.main(["notes", "delete", _NID_PACKING])
    assert rc == 0
    assert "Aborted." in capsys.readouterr().err
    assert not any(c[0] == "delete_artifact" for c in _FakeNotesApi.instances[-1].calls)


def test_notes_delete_off_a_terminal_without_yes_is_exit_2(
    tmp_home, patch_api, capsys, monkeypatch
) -> None:
    _login(tmp_home)
    patch_api(detail=_detail(name="Packing list"), delete="Packing list")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    rc = cli.main(["notes", "delete", _NID_PACKING])
    assert rc == 2
    assert "--yes" in capsys.readouterr().err
    assert not any(c[0] == "delete_artifact" for c in _FakeNotesApi.instances[-1].calls)


def test_notes_old_spellings_are_rejected(tmp_home, capsys) -> None:
    for argv in (["notes", "new", "x"], ["notes", "rm", "x"]):
        with pytest.raises(SystemExit) as ei:
            cli.main(argv)
        assert ei.value.code == 2


def test_notes_delete_yes_skips_the_prompt(tmp_home, patch_api, monkeypatch) -> None:
    _login(tmp_home)
    patch_api(detail=_detail(name="Packing list"), delete="Packing list")

    def fail_prompt(prompt):
        raise AssertionError("should not prompt with --yes")

    monkeypatch.setattr(cli, "_prompt", fail_prompt)
    rc = cli.main(["notes", "delete", _NID_PACKING, "--yes"])
    assert rc == 0
    assert any(c[0] == "delete_artifact" for c in _FakeNotesApi.instances[-1].calls)


def test_notes_delete_404_prints_the_uniform_message(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(notes=api_client.ApiError(status=404, body="", detail="not found"))
    rc = cli.main(["notes", "delete", "nope", "--yes"])
    assert rc == 2
    assert "no such room or note, or you are not a member" in capsys.readouterr().err


def test_notes_delete_503_prints_retry_message(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(notes=api_client.ApiError(status=503, body="", detail="down"))
    rc = cli.main(["notes", "delete", "nope", "--yes"])
    assert rc == 1
    assert "membership check unavailable, try again" in capsys.readouterr().err


def test_notes_delete_other_kind_is_refused_without_deleting(tmp_home, patch_api, capsys) -> None:
    """The delete action deletes any artifact kind; a raw UUID of an image
    must not reach it through ``pepper notes rm``."""
    _login(tmp_home)
    patch_api(detail=_detail(kind="image"), delete="photo.png")
    rc = cli.main(["notes", "delete", _NID_PACKING, "--yes"])
    assert rc == 2
    assert "no such room or note, or you are not a member" in capsys.readouterr().err
    assert not any(c[0] == "delete_artifact" for c in _FakeNotesApi.instances[-1].calls)


def test_notes_delete_prompt_names_the_note_not_the_raw_argument(
    tmp_home, patch_api, monkeypatch
) -> None:
    _login(tmp_home)
    patch_api(detail=_detail(name="Packing list"), delete="Packing list")
    prompts: list[str] = []
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_prompt", lambda prompt: prompts.append(prompt) or "y")
    rc = cli.main(["notes", "delete", _NID_PACKING])
    assert rc == 0
    assert "'Packing list'" in prompts[0]
    assert _NID_PACKING not in prompts[0]


def test_notes_delete_422_from_the_action_is_the_uniform_missing_note(
    tmp_home, patch_api, capsys
) -> None:
    """Deleted between the read and the delete, or a ``--room`` that doesn't
    own the UUID: the action's 422 "isn't in your collection"."""
    _login(tmp_home)
    patch_api(
        detail=_detail(),
        delete=api_client.ApiError(status=422, body="", detail="not in your collection"),
    )
    rc = cli.main(["notes", "delete", _NID_PACKING, "--yes"])
    assert rc == 2
    assert "no such room or note, or you are not a member" in capsys.readouterr().err


def test_notes_delete_404_on_the_detail_read_is_the_uniform_missing_note(
    tmp_home, patch_api, capsys
) -> None:
    _login(tmp_home)
    patch_api(detail=api_client.ApiError(status=404, body="", detail="not found"))
    rc = cli.main(["notes", "delete", _NID_PACKING, "--yes"])
    assert rc == 2
    assert "no such room or note, or you are not a member" in capsys.readouterr().err
    assert not any(c[0] == "delete_artifact" for c in _FakeNotesApi.instances[-1].calls)


def test_notes_delete_other_api_errors_keep_the_generic_handling(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(detail=_detail(), delete=api_client.ApiError(status=500, body="", detail="boom"))
    rc = cli.main(["notes", "delete", _NID_PACKING, "--yes"])
    assert rc == 1
