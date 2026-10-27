"""End-to-end tests for ``pepper comments`` and the note lines of ``messages list``.

Same approach as ``test_cli_notes.py``: ``cli.main([...])`` against a monkeypatched
``api_client.ApiClient``, so nothing touches the network.
"""

from __future__ import annotations

import io
import json
import sys

import httpx
import pytest

from pepper_cli import api_client, cli, credentials
from pepper_cli.secret import SecretStr

_GID = "00000000-0000-0000-0000-0000000000a1"
_NID = "00000000-0000-0000-0000-0000000000d1"
_TID = "00000000-0000-0000-0000-0000000000b1"


def _login(tmp_home) -> None:
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))


def _c(cid: str, body: str = "hello", **over) -> api_client.NoteComment:
    base = {"id": cid, "body": body, "author": "Ada", "created_at": "2026-10-26T10:00:00+00:00"}
    base.update(over)
    return api_client.NoteComment(**base)


def _thread(root: api_client.NoteComment, *replies, **over) -> api_client.NoteCommentThread:
    base = {"message": root, "position": None, "detached": False, "replies": list(replies)}
    base.update(over)
    return api_client.NoteCommentThread(**base)


def _comments(*threads, resolved_count=0) -> api_client.NoteComments:
    return api_client.NoteComments(
        artifact_id=_NID,
        group_id=_GID,
        topic_id=_TID,
        etag="e1",
        threads=list(threads),
        resolved_count=resolved_count,
    )


class _FakeApi:
    instances: list[_FakeApi] = []

    def __init__(self, *, server_url, token, **canned) -> None:
        self._canned = canned
        self.calls: list[tuple] = []
        self.is_agent_token = token.get_secret_value().startswith(api_client.AGENT_TOKEN_PREFIX)
        _FakeApi.instances.append(self)

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

    def list_artifacts(self, *, principal=None, **kwargs):
        # The server's rule for an agent token: notes of a room it names, nothing else.
        self.calls.append(("artifacts", principal, kwargs))
        if self.is_agent_token and principal is None:
            raise _err(422, {"detail": "an agent names the room with ?principal="})
        return self._result("notes")

    def list_note_comments(self, artifact_id):
        self.calls.append(("list", artifact_id))
        return self._result("comments")

    def create_note_comment(self, artifact_id, **kwargs):
        self.calls.append(("create", artifact_id, kwargs))
        return self._result("created")

    def resolve_note_thread(self, artifact_id, thread_id):
        self.calls.append(("resolve", artifact_id, thread_id))
        return self._result("thread")

    def reopen_note_thread(self, artifact_id, thread_id):
        self.calls.append(("reopen", artifact_id, thread_id))
        return self._result("thread")


@pytest.fixture
def patch_api(monkeypatch):
    _FakeApi.instances.clear()

    def install(**canned):
        monkeypatch.setattr(api_client, "ApiClient", lambda **kw: _FakeApi(**canned, **kw))

    return install


def _calls() -> list[tuple]:
    return _FakeApi.instances[0].calls


def _err(status: int, payload: object = None) -> api_client.ApiError:
    body = json.dumps(payload) if payload is not None else ""
    return api_client.ApiError(status=status, body=body, detail="x")


# --- note names under an agent token -----------------------------------------------------


def _agent_login(tmp_home) -> None:
    credentials.save(server_url="http://example.com", token=SecretStr("pep_agt_abc"))


def _notes(*names: str) -> api_client.ArtifactListResponse:
    items = [
        api_client.ArtifactListItem(
            id=_NID, name=n, kind="inline_text", updated_at="2026-10-26T10:00:00+00:00", etag="e"
        )
        for n in names
    ]
    return api_client.ArtifactListResponse(truncated=False, items=items)


def test_agent_resolves_a_note_name_within_the_named_room(tmp_home, patch_api, capsys) -> None:
    _agent_login(tmp_home)
    patch_api(notes=_notes("Runbook"), comments=_comments(_thread(_c("m1", "hi"))))
    assert cli.main(["comments", "list", "--room", _GID, "--note", "Runbook"]) == 0
    assert _calls()[0][:2] == ("artifacts", _GID)
    assert _calls()[-1] == ("list", _NID)
    assert "hi" in capsys.readouterr().out


def test_agent_note_name_without_room_says_to_add_one(tmp_home, patch_api, capsys) -> None:
    _agent_login(tmp_home)
    patch_api()
    assert cli.main(["comments", "list", "--note", "Runbook"]) == 2
    assert "--room" in capsys.readouterr().err
    assert _calls() == []


def test_agent_note_id_without_room_needs_no_lookup(tmp_home, patch_api) -> None:
    _agent_login(tmp_home)
    patch_api(comments=_comments())
    assert cli.main(["comments", "list", "--note", _NID]) == 0
    assert _calls() == [("list", _NID)]


# --- list ---------------------------------------------------------------------------------


def test_list_renders_quote_replies_and_deleted(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    quoted = _thread(
        _c("m1", "Is this right?", anchor={"exact": "sunscreen"}),
        _c("m2", "Yes", author="Bo"),
        _c("m3", "", author="Cy", deleted=True),
    )
    whole = _thread(_c("m4", "General remark"), detached=True)
    patch_api(comments=_comments(quoted, whole))
    assert cli.main(["comments", "list", "--note", _NID, "--room", _GID]) == 0
    out = capsys.readouterr().out
    assert "“sunscreen”" in out
    assert "Is this right?" in out and "Yes" in out
    assert "(deleted)" in out
    assert "whole note" in out and "detached" in out
    assert _calls() == [("list", _NID)]


def test_list_empty(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(comments=_comments())
    assert cli.main(["comments", "list", "--note", _NID]) == 0
    assert "(no comments)" in capsys.readouterr().out


def _mixed():
    done = _thread(
        _c("m1", "Done thing", resolved_at="2026-10-26T11:00:00+00:00", resolved_by="Bo")
    )
    return _comments(done, _thread(_c("m2", "Open thing")), resolved_count=1)


def test_list_hides_resolved_threads_and_points_at_all(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(comments=_mixed())
    assert cli.main(["comments", "list", "--note", _NID]) == 0
    out = capsys.readouterr().out
    assert "Open thing" in out and "Done thing" not in out
    assert "1 resolved hidden" in out and "--all" in out


def test_list_all_shows_resolved_with_who_resolved(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(comments=_mixed())
    assert cli.main(["comments", "list", "--note", _NID, "--all"]) == 0
    out = capsys.readouterr().out
    assert "Open thing" in out and "Done thing" in out
    assert "resolved by Bo" in out and "hidden" not in out


def test_list_with_only_resolved_threads_says_no_open_comments(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    done = _thread(_c("m1", resolved_at="2026-10-26T11:00:00+00:00"))
    patch_api(comments=_comments(done, resolved_count=1))
    assert cli.main(["comments", "list", "--note", _NID]) == 0
    assert "(no open comments)" in capsys.readouterr().out


def test_list_json_always_has_every_thread_whatever_all_says(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(comments=_mixed())
    assert cli.main(["comments", "list", "--note", _NID, "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert [t["message"]["id"] for t in doc["items"]] == ["m1", "m2"]
    assert doc["items"][0]["message"]["resolved_by"] == "Bo"
    assert doc["items"][0]["message"]["deleted"] is False
    assert doc["resolved_count"] == 1
    assert (doc["artifact_id"], doc["group_id"], doc["topic_id"], doc["etag"]) == (
        _NID,
        _GID,
        _TID,
        "e1",
    )


def test_list_requires_note_and_names_the_flag(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    with pytest.raises(SystemExit) as exc:
        cli.main(["comments", "list"])
    assert exc.value.code == 2
    assert "the note is a flag here" in capsys.readouterr().err


def test_list_404_prints_the_uniform_message(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(comments=_err(404))
    assert cli.main(["comments", "list", "--note", _NID]) == 2
    assert "no such room or note" in capsys.readouterr().err


def test_list_503_exits_1(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(comments=_err(503))
    assert cli.main(["comments", "list", "--note", _NID]) == 1
    assert "try again" in capsys.readouterr().err


def test_list_409_personal_note_json_error(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(comments=_err(409, {"detail": {"error": "personal_note"}}))
    assert cli.main(["comments", "list", "--note", _NID, "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)["error"]
    assert error["code"] == "conflict" and error["status"] == 409


# --- create -------------------------------------------------------------------------------


_CREATED = {"thread_root_id": "m9", "topic_id": _TID, "message": {"id": "m9"}}


def test_create_sends_quote_mentions_and_a_fresh_idempotency_key(
    tmp_home, patch_api, capsys
) -> None:
    _login(tmp_home)
    patch_api(created=_CREATED)
    rc = cli.main(
        [
            "comments", "create", "--note", _NID, "--quote", "sunscreen",
            "--mention", "p1", "--mention", "p2", "Pack", "more", "of", "it",
        ]
    )  # fmt: skip
    assert rc == 0
    cap = capsys.readouterr()
    assert "m9" in cap.out and "Commented." in cap.err + cap.out
    (_, note, kwargs) = _calls()[0]
    assert note == _NID
    assert kwargs["body"] == "Pack more of it"
    assert kwargs["quote"] == "sunscreen" and kwargs["reply_to"] is None
    assert kwargs["mentions"] == ["p1", "p2"]
    assert kwargs["idempotency_key"]


def test_create_reply_with_explicit_key(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api(created=_CREATED)
    rc = cli.main(
        ["comments", "create", "--note", _NID, "--reply-to", "m1", "--idempotency-key", "k1", "ok"]
    )
    assert rc == 0
    kwargs = _calls()[0][2]
    assert kwargs["reply_to"] == "m1" and kwargs["idempotency_key"] == "k1"
    assert kwargs["quote"] is None and kwargs["mentions"] is None


def test_create_reads_the_body_from_stdin(tmp_home, patch_api, monkeypatch) -> None:
    _login(tmp_home)
    patch_api(created=_CREATED)
    monkeypatch.setattr(sys, "stdin", io.StringIO("  line one\nline two\n"))
    assert cli.main(["comments", "create", "--note", _NID, "-"]) == 0
    assert _calls()[0][2]["body"] == "line one\nline two"


def test_create_json_prints_the_server_answer(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(created=_CREATED)
    assert cli.main(["comments", "create", "--note", _NID, "hi", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == _CREATED


@pytest.mark.parametrize(
    "argv",
    [
        ["comments", "create", "--note", _NID],
        ["comments", "create", "--note", _NID, "   "],
        ["comments", "create", "--note", _NID, "--quote", " ", "x"],
        ["comments", "create", "--note", _NID, "--reply-to", "", "x"],
        ["comments", "create", "--note", _NID, "--mention", "", "x"],
        ["comments", "create", "--note", _NID, "--idempotency-key", "", "x"],
        ["comments", "create", "--note", _NID, "--quote", "q", "--reply-to", "m1", "x"],
    ],
)
def test_create_bad_input_exits_2_without_a_call(tmp_home, patch_api, argv) -> None:
    _login(tmp_home)
    patch_api(created=_CREATED)
    assert cli.main(argv) == 2
    assert not _FakeApi.instances or _calls() == []


def test_create_empty_stdin_exits_2(tmp_home, patch_api, monkeypatch) -> None:
    _login(tmp_home)
    patch_api(created=_CREATED)
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))
    assert cli.main(["comments", "create", "--note", _NID, "-"]) == 2


@pytest.mark.parametrize(
    ("status", "payload", "expect"),
    [
        (422, {"detail": {"error": "quote_not_found"}}, "does not appear in the note"),
        (422, {"detail": {"error": "quote_ambiguous", "count": 3}}, "appears 3 times"),
        (422, {"error": "quote_ambiguous"}, "more than once"),
        (422, {"detail": {"error": "quote_mismatch"}}, "does not match"),
        (409, {"detail": {"error": "personal_note"}}, "personal notes"),
    ],
)
def test_create_maps_server_refusals_to_messages(
    tmp_home, patch_api, capsys, status, payload, expect
) -> None:
    _login(tmp_home)
    patch_api(created=_err(status, payload))
    assert cli.main(["comments", "create", "--note", _NID, "--quote", "q", "x"]) == 2
    assert expect in capsys.readouterr().err


def test_create_quote_invalid_and_unparseable_bodies_fall_back_to_generic(
    tmp_home, patch_api, capsys
) -> None:
    _login(tmp_home)
    patch_api(created=_err(422, {"detail": {"error": "quote_invalid"}}))
    assert cli.main(["comments", "create", "--note", _NID, "--quote", "q", "x"]) != 0
    assert capsys.readouterr().err.strip()
    patch_api(created=api_client.ApiError(status=422, body="not json", detail="x"))
    assert cli.main(["comments", "create", "--note", _NID, "--quote", "q", "x"]) != 0


# --- resolve / reopen ---------------------------------------------------------------------


@pytest.mark.parametrize(("verb", "word"), [("resolve", "Resolved."), ("reopen", "Reopened.")])
def test_resolve_and_reopen(tmp_home, patch_api, capsys, verb, word) -> None:
    _login(tmp_home)
    patch_api(thread=_thread(_c("m1")))
    assert cli.main(["comments", verb, "--note", _NID, "m1"]) == 0
    cap = capsys.readouterr()
    assert word in cap.out + cap.err and "m1" in cap.out
    assert _calls() == [(verb, _NID, "m1")]


def test_resolve_json_prints_the_thread(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(thread=_thread(_c("m1", resolved_at="2026-10-26T11:00:00+00:00", resolved_by="Bo")))
    assert cli.main(["comments", "resolve", "--note", _NID, "m1", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["message"]["resolved_by"] == "Bo"


def test_resolve_unknown_thread_is_the_uniform_404(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(thread=_err(404))
    assert cli.main(["comments", "resolve", "--note", _NID, "nope"]) == 2
    assert "no such room or note" in capsys.readouterr().err


def test_resolve_blank_thread_id_exits_2(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(thread=_thread(_c("m1")))
    assert cli.main(["comments", "resolve", "--note", _NID, " "]) == 2
    assert "THREAD_ID cannot be empty" in capsys.readouterr().err


# --- messages list: note header, quote, resolved --------------------------------------------


def _group_message(**over) -> api_client.GroupMessage:
    base = {
        "id": "m1",
        "is_pepper": False,
        "is_you": False,
        "is_deleted": False,
        "is_broken": False,
        "run_start": True,
        "display_name": "Dana",
        "initial": "D",
        "avatar_tint": "t1",
        "raw_body": "see this",
        "rendered_html": "<p>see this</p>",
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


class _FakeRoomApi(_FakeApi):
    def list_group_topics(self, group_id):
        return self._result("topics")

    def list_group_messages(self, group_id, topic_id, **kwargs):
        return self._result("page")


def _topics(subject: str | None):
    return [
        api_client.GroupTopicSummary(
            topic_id=_TID,
            name="Note: Packing",
            last_activity_at="2026-09-16T17:30:00+00:00",
            dedicated_conversation=False,
            archived_at=None,
            imported_history=False,
            subject_artifact_id=subject,
        )
    ]


def _patch_room(monkeypatch, subject, *messages) -> None:
    page = api_client.GroupMessagePage(messages=list(messages), next_before=None)
    monkeypatch.setattr(
        api_client,
        "ApiClient",
        lambda **kw: _FakeRoomApi(topics=_topics(subject), page=page, **kw),
    )


def test_messages_list_in_a_note_topic_prints_header_quote_and_resolved(
    tmp_home, monkeypatch, capsys
) -> None:
    _login(tmp_home)
    _patch_room(
        monkeypatch,
        _NID,
        _group_message(
            anchor={"exact": "sunscreen  SPF\n50"},
            resolved_at="2026-10-26T11:00:00+00:00",
            resolved_by_name="Bo",
        ),
        _group_message(id="m2", anchor={"exact": "x" * 300}, resolved_at="t"),
    )
    assert cli.main(["messages", "list", "--room", _GID, "--topic", _TID]) == 0
    out = capsys.readouterr().out
    assert f"pepper comments list --note {_NID}" in out.splitlines()[0]
    assert '> "sunscreen SPF 50"' in out
    assert "(resolved by Bo)" in out and "(resolved)" in out
    long_line = next(ln for ln in out.splitlines() if ln.startswith('> "x'))
    assert long_line.endswith('…"') and len(long_line) < 175


def test_messages_list_note_header_keeps_the_profile(tmp_home, monkeypatch, capsys) -> None:
    credentials.set_profile("helper")
    _login(tmp_home)
    _patch_room(monkeypatch, _NID, _group_message())
    argv = ["--profile", "helper", "messages", "list", "--room", _GID, "--topic", _TID]
    assert cli.main(argv) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert f"pepper --profile helper comments list --note {_NID}" in first


def test_messages_list_in_an_ordinary_topic_has_no_note_lines(
    tmp_home, monkeypatch, capsys
) -> None:
    _login(tmp_home)
    _patch_room(monkeypatch, None, _group_message())
    assert cli.main(["messages", "list", "--room", _GID, "--topic", _TID]) == 0
    out = capsys.readouterr().out
    assert "note comments" not in out and '> "' not in out and "resolved" not in out


def test_messages_list_json_skips_the_topic_lookup(tmp_home, monkeypatch, capsys) -> None:
    _login(tmp_home)
    _patch_room(monkeypatch, _NID, _group_message(anchor={"exact": "a"}, resolved_at="t"))
    assert cli.main(["messages", "list", "--room", _GID, "--topic", _TID, "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    row = doc["items"][0]
    assert row["anchor"] == {"exact": "a"} and row["resolved_at"] == "t"


@pytest.mark.parametrize(
    "failure",
    [_err(500), httpx.ReadTimeout("timed out")],
    ids=["http-500", "transport-timeout"],
)
def test_messages_list_still_prints_when_the_topic_lookup_fails(
    tmp_home, monkeypatch, capsys, failure
) -> None:
    _login(tmp_home)
    page = api_client.GroupMessagePage(messages=[_group_message()], next_before=None)
    monkeypatch.setattr(
        api_client,
        "ApiClient",
        lambda **kw: _FakeRoomApi(topics=failure, page=page, **kw),
    )
    assert cli.main(["messages", "list", "--room", _GID, "--topic", _TID]) == 0
    assert "see this" in capsys.readouterr().out
