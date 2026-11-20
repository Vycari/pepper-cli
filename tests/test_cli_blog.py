"""``pepper blog stage`` / ``unschedule`` / ``unstage`` / ``publish`` against a faked ``ApiClient``.

Same approach as ``test_cli_notes.py``: ``cli.main([...])`` with ``api_client.ApiClient``
replaced, so no network. The server's own rules (owner only, the blog rollout, what a finding is)
are the server's tests; here the CLI is held to fetching, rendering, confirming and sending.
"""

from __future__ import annotations

import json

import pytest

from pepper_cli import api_client, cli, credentials
from pepper_cli.secret import SecretStr

_GID = "00000000-0000-0000-0000-0000000000a1"
_NID = "00000000-0000-0000-0000-0000000000d1"
_SOURCE = "a" * 64 + "." + "b" * 64 + "." + "c" * 64


def _login(tmp_home) -> None:
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))


def _check(**over) -> dict:
    base = {
        "note_id": _NID,
        "title": "Hello World",
        "slug": "2026/11/19/hello-world",
        "republish": False,
        "summary": "",
        "max_summary": 280,
        "source": _SOURCE,
        "findings": False,
        "refusal": None,
        "flattened_links": [],
        "linked_posts": [],
        "other_images": [],
        "copied_images": 0,
        "unreviewed_images": 0,
        "warnings": [],
        "review": {"human_version_found": True, "diff": "", "body": ""},
    }
    base.update(over)
    return base


def _stage(**over) -> dict:
    base = {
        "slug": None,
        "summary": None,
        "publish_at": None,
        "publish_at_display": None,
        "timezone": "America/Los_Angeles",
        "updated_at": None,
        "staged_by": None,
    }
    base.update(over)
    return base


_SCHEDULED = _stage(
    slug="my-post",
    summary="A greeting.",
    publish_at="2026-11-21T17:00:00+00:00",
    publish_at_display="Sat, Nov 21, 9:00 AM \u00b7 America/Los_Angeles",
)

_PUBLISHED = {
    "post_id": "00000000-0000-0000-0000-0000000000f1",
    "slug": "2026/11/19/hello-world",
    "url": f"/blog/{_GID}/2026/11/19/hello-world",
    "republished": False,
    "card": "posted",
}


class _FakeBlogApi:
    instances: list[_FakeBlogApi] = []

    def __init__(self, *, server_url, token, **canned) -> None:
        self._canned = canned
        self.calls: list[tuple] = []
        _FakeBlogApi.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def _result(self, key):
        value = self._canned[key]
        if isinstance(value, Exception):
            raise value
        return value

    def list_groups(self, *, archived=False):
        return api_client.GroupList(
            enabled=True, degraded=False, total=0, archived_total=0, groups=[]
        )

    def list_artifacts(self, *, principal=None, kind=None, name=None, name_prefix=None, **_):
        self.calls.append(("list_artifacts", principal, name, name_prefix))
        return self._result("notes")

    def get_blog_stage(self, group_id, note_id):
        self.calls.append(("get_stage", group_id, note_id))
        check = self._result("check")
        return {**self._result("stage"), "check": check}

    def update_blog_stage(self, group_id, note_id, fields):
        self.calls.append(("update_stage", group_id, note_id, fields))
        return self._result("updated")

    def delete_blog_stage(self, group_id, note_id):
        self.calls.append(("delete_stage", group_id, note_id))
        self._result("deleted")

    def publish_blog_note(self, group_id, note_id, **kwargs):
        self.calls.append(("publish", group_id, note_id, kwargs))
        return self._result("publish")

    def sent(self, name: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == name]


@pytest.fixture
def patch_api(monkeypatch):
    _FakeBlogApi.instances.clear()

    def install(**canned):
        canned.setdefault("check", _check())
        canned.setdefault("publish", _PUBLISHED)
        canned.setdefault("stage", _stage())
        canned.setdefault("updated", _SCHEDULED)
        canned.setdefault("deleted", None)
        monkeypatch.setattr(api_client, "ApiClient", lambda **kw: _FakeBlogApi(**canned, **kw))

    return install


def _api() -> _FakeBlogApi:
    return _FakeBlogApi.instances[-1]


def _tty(monkeypatch, answer: str | None) -> list[str]:
    """A terminal answering ``answer`` (``None``: no terminal); the prompts asked are returned."""
    asked: list[str] = []
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: answer is not None)

    def prompt(text: str) -> str:
        asked.append(text)
        return answer or ""

    monkeypatch.setattr(cli, "_prompt", prompt)
    return asked


def _error_lines(stderr: str) -> int:
    """How many error lines were printed: the check shown before a failed publish is not one."""
    return sum(1 for line in stderr.splitlines() if line.startswith("error:"))


def _err(status: int, detail: str | None = "nope", body: str = "") -> api_client.ApiError:
    return api_client.ApiError(status=status, body=body, detail=detail)


# ---------------------------------------------------------------------------
# stage (no flags): the check and the saved stage


def test_stage_show_renders_what_publishing_would_do(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(
        check=_check(
            summary="A greeting.",
            flattened_links=["Nowhere"],
            other_images=["https://example.com/a.png"],
            copied_images=2,
            warnings=["1 image has suggested descriptions nobody has reviewed."],
            findings=True,
            review={"human_version_found": True, "diff": "-old line\n+new line", "body": ""},
        )
    )
    rc = cli.main(["blog", "stage", "--room", _GID, _NID])
    assert rc == 0
    out = capsys.readouterr().out
    assert "First publish: Hello World" in out
    assert "Address: 2026/11/19/hello-world" in out
    assert "Summary (up to 280 characters): A greeting." in out
    assert "Nowhere" in out
    assert "https://example.com/a.png" in out
    assert "Images copied into the blog: 2" in out
    assert "suggested descriptions nobody has reviewed" in out
    assert "-old line" in out and "+new line" in out
    assert "review before this goes public" in out
    assert _api().calls == [("get_stage", _GID, _NID)]


def test_stage_show_without_a_human_version_shows_the_whole_body_to_review(
    tmp_home, patch_api, capsys
) -> None:
    _login(tmp_home)
    patch_api(
        check=_check(
            findings=True,
            review={"human_version_found": False, "diff": "", "body": "The whole draft.\nLine 2."},
        )
    )
    assert cli.main(["blog", "stage", "--room", _GID, _NID]) == 0
    out = capsys.readouterr().out
    assert "whole text is unreviewed" in out
    assert "The whole draft." in out and "Line 2." in out


def test_stage_show_says_republish_and_a_refusal(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(check=_check(republish=True, refusal="Only a text note can be published."))
    assert cli.main(["blog", "stage", "--room", _GID, _NID]) == 0
    out = capsys.readouterr().out
    assert "Republish: Hello World" in out
    assert "Cannot be published: Only a text note can be published." in out


def test_stage_show_strips_terminal_escapes_from_server_text(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(check=_check(title="Evil\x1b[31m title\nsecond line"))
    assert cli.main(["blog", "stage", "--room", _GID, _NID]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "second line" in out.splitlines()[0]


def test_stage_show_json_passes_the_server_answer_through(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    check = {**_check(), "added_later": {"x": 1}}
    server = {**_stage(), "check": check}
    patch_api(check=check)
    assert cli.main(["blog", "stage", "--room", _GID, _NID, "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == server
    assert captured.err == ""


def test_stage_show_resolves_the_note_by_name_in_the_room(tmp_home, patch_api) -> None:
    _login(tmp_home)
    item = api_client.ArtifactListItem(
        id=_NID,
        name="Hello World",
        kind="inline_text",
        updated_at="2026-09-16T17:30:00+00:00",
        etag="e",
        labels=[],
        summary=None,
    )
    patch_api(notes=api_client.ArtifactListResponse(truncated=False, items=[item]))
    assert cli.main(["blog", "stage", "--room", _GID, "Hello World"]) == 0
    assert _api().calls[0] == ("list_artifacts", _GID, "Hello World", None)
    assert _api().sent("get_stage") == [("get_stage", _GID, _NID)]


def test_stage_show_unknown_note_name_is_exit_2(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(notes=api_client.ArtifactListResponse(truncated=False, items=[]))
    assert cli.main(["blog", "stage", "--room", _GID, "nope"]) == 2
    assert "no note named 'nope'" in capsys.readouterr().err
    assert not _api().sent("get_stage")


def test_stage_show_requires_a_room() -> None:
    with pytest.raises(SystemExit) as ei:
        cli.main(["blog", "stage", _NID])
    assert ei.value.code == 2


def test_stage_show_empty_prints_check_then_no_stage(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api()
    assert cli.main(["blog", "stage", "--room", _GID, _NID]) == 0
    out = capsys.readouterr().out
    assert out.index("First publish: Hello World") < out.index("Staged:")
    assert "Staged: \u2014" in out
    assert "Not scheduled \u2014 publish with `pepper blog publish NOTE`" in out
    assert [c[0] for c in _api().calls] == ["get_stage"]


def test_stage_show_set_prints_slug_summary_and_schedule(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(stage=_SCHEDULED)
    assert cli.main(["blog", "stage", "--room", _GID, _NID]) == 0
    out = capsys.readouterr().out
    assert "Staged: my-post" in out
    assert "Summary: A greeting." in out
    assert "Scheduled: Sat, Nov 21, 9:00 AM \u00b7 America/Los_Angeles" in out
    assert "Not scheduled" not in out


# ---------------------------------------------------------------------------
# stage (flags): PATCH only what was given


def test_stage_patches_only_the_given_fields(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api()
    assert cli.main(["blog", "stage", "--room", _GID, _NID, "--slug", "my-post"]) == 0
    assert _api().sent("update_stage")[0][3] == {"slug": "my-post"}
    captured = capsys.readouterr()
    assert "Staged: my-post" in captured.out
    assert "First publish: Hello World" in captured.err
    assert cli.main(["blog", "stage", "--room", _GID, _NID, "--summary", ""]) == 0
    assert _api().sent("update_stage")[0][3] == {"summary": ""}


def test_stage_at_is_sent_verbatim_with_acknowledge_after_yes(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(check=_check(findings=True, flattened_links=["Nowhere"]))
    rc = cli.main(["blog", "stage", "--room", _GID, _NID, "--at", "tomorrow 9am", "--yes"])
    assert rc == 0
    assert _api().sent("update_stage")[0][3] == {
        "publish_at": "tomorrow 9am",
        "acknowledge": True,
    }
    captured = capsys.readouterr()
    assert "Nowhere" in captured.err
    assert "Scheduled: Sat, Nov 21" in captured.out


def test_stage_at_asks_and_a_yes_saves(tmp_home, patch_api, monkeypatch) -> None:
    _login(tmp_home)
    patch_api()
    asked = _tty(monkeypatch, "y")
    assert cli.main(["blog", "stage", "--room", _GID, _NID, "--at", "2026-11-21 09:00"]) == 0
    assert len(asked) == 1
    assert _api().sent("update_stage")[0][3]["acknowledge"] is True
    assert _api().sent("update_stage")[0][3]["publish_at"] == "2026-11-21 09:00"


def test_stage_at_declined_saves_nothing(tmp_home, patch_api, monkeypatch, capsys) -> None:
    _login(tmp_home)
    patch_api()
    _tty(monkeypatch, "n")
    assert cli.main(["blog", "stage", "--room", _GID, _NID, "--at", "2026-11-21 09:00"]) == 0
    assert "Aborted." in capsys.readouterr().err
    assert not _api().sent("update_stage")


def test_stage_at_refused_check_exits_1_without_saving(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(check=_check(refusal="Only a text note can be published."))
    rc = cli.main(["blog", "stage", "--room", _GID, _NID, "--at", "2026-11-21 09:00", "--yes"])
    assert rc == 1
    assert "Only a text note can be published." in capsys.readouterr().err
    assert not _api().sent("update_stage")


def test_stage_at_json_without_yes_refuses(tmp_home, patch_api, monkeypatch, capsys) -> None:
    _login(tmp_home)
    patch_api()
    _tty(monkeypatch, "y")
    rc = cli.main(["blog", "stage", "--room", _GID, _NID, "--at", "2026-11-21 09:00", "--json"])
    assert rc == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "usage"
    assert not _api().sent("update_stage")


def test_stage_at_422_message_surfaces(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(updated=_err(422, "That time is in the past."))
    rc = cli.main(["blog", "stage", "--room", _GID, _NID, "--at", "2001-01-01", "--yes"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "422: That time is in the past." in err
    assert _error_lines(err) == 1


def test_stage_server_409_refusal_is_one_line(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(updated=_err(409, "Only a text note can be published."))
    rc = cli.main(["blog", "stage", "--room", _GID, _NID, "--at", "2026-11-21 09:00", "--yes"])
    assert rc == 1
    assert _error_lines(capsys.readouterr().err) == 1


def test_stage_flags_json_prints_the_stage(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api()
    assert cli.main(["blog", "stage", "--room", _GID, _NID, "--slug", "my-post", "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == _SCHEDULED
    assert captured.err == ""


def test_unschedule_clears_the_time_and_prints_the_stage(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(updated=_stage(slug="my-post"))
    assert cli.main(["blog", "unschedule", "--room", _GID, _NID]) == 0
    assert _api().sent("update_stage")[0][3] == {"publish_at": None}
    assert "Not scheduled" in capsys.readouterr().out


def test_unstage_deletes_the_stage(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api()
    assert cli.main(["blog", "unstage", "--room", _GID, _NID]) == 0
    assert _api().sent("delete_stage") == [("delete_stage", _GID, _NID)]
    assert "Stage removed." in capsys.readouterr().out


def test_removed_check_command_is_gone() -> None:
    with pytest.raises(SystemExit) as ei:
        cli.main(["blog", "check", "--room", _GID, _NID])
    assert ei.value.code == 2


# ---------------------------------------------------------------------------
# publish


def test_publish_yes_checks_then_publishes_with_the_source(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api()
    rc = cli.main(["blog", "publish", "--room", _GID, _NID, "--yes"])
    assert rc == 0
    captured = capsys.readouterr()
    # The check is shown on stderr, the result on stdout.
    assert "First publish: Hello World" in captured.err
    assert "Published." in captured.out
    assert f"/blog/{_GID}/2026/11/19/hello-world" in captured.out
    api = _api()
    assert [c[0] for c in api.calls] == ["get_stage", "publish"]
    assert api.sent("publish")[0][3] == {
        "source": _SOURCE,
        "acknowledge": True,
        "slug": None,
        "summary": None,
    }


def test_publish_without_slug_or_summary_leaves_them_out_for_the_saved_stage(
    tmp_home, patch_api
) -> None:
    _login(tmp_home)
    patch_api(stage=_SCHEDULED)
    assert cli.main(["blog", "publish", "--room", _GID, _NID, "--yes"]) == 0
    sent = _api().sent("publish")[0][3]
    assert sent["slug"] is None and sent["summary"] is None  # the client omits None from the body


def test_publish_passes_slug_and_summary_through(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api()
    rc = cli.main(
        [
            "blog", "publish", "--room", _GID, _NID, "--yes",
            "--slug", "my-post", "--summary", "A short one.",
        ]
    )  # fmt: skip
    assert rc == 0
    sent = _api().sent("publish")[0][3]
    assert sent["slug"] == "my-post"
    assert sent["summary"] == "A short one."


def test_publish_an_empty_summary_is_sent_not_dropped(tmp_home, patch_api) -> None:
    _login(tmp_home)
    patch_api()
    assert cli.main(["blog", "publish", "--room", _GID, _NID, "--yes", "--summary", ""]) == 0
    assert _api().sent("publish")[0][3]["summary"] == ""


def test_publish_confirm_yes_publishes_and_acknowledges(tmp_home, patch_api, monkeypatch, capsys):
    _login(tmp_home)
    patch_api(check=_check(findings=True, flattened_links=["Nowhere"]))
    asked = _tty(monkeypatch, "y")
    assert cli.main(["blog", "publish", "--room", _GID, _NID]) == 0
    assert len(asked) == 1
    assert "Publish 'Hello World'" in asked[0]
    assert "Nowhere" in capsys.readouterr().err
    assert _api().sent("publish")[0][3]["acknowledge"] is True


def test_publish_confirm_no_publishes_nothing(tmp_home, patch_api, monkeypatch, capsys) -> None:
    _login(tmp_home)
    patch_api(check=_check(findings=True, flattened_links=["Nowhere"]))
    _tty(monkeypatch, "n")
    rc = cli.main(["blog", "publish", "--room", _GID, _NID])
    assert rc == 0
    assert "Aborted." in capsys.readouterr().err
    assert not _api().sent("publish")


def test_publish_a_republish_asks_to_republish(tmp_home, patch_api, monkeypatch) -> None:
    _login(tmp_home)
    patch_api(check=_check(republish=True))
    asked = _tty(monkeypatch, "y")
    assert cli.main(["blog", "publish", "--room", _GID, _NID]) == 0
    assert asked[0].startswith("Republish 'Hello World'")


def test_publish_without_a_terminal_needs_yes_and_sends_nothing(
    tmp_home, patch_api, monkeypatch, capsys
) -> None:
    _login(tmp_home)
    patch_api(check=_check(findings=True))
    _tty(monkeypatch, None)
    assert cli.main(["blog", "publish", "--room", _GID, _NID]) == 2
    assert "--yes" in capsys.readouterr().err
    assert not _api().sent("publish")


def test_publish_json_never_prompts_and_needs_yes(tmp_home, patch_api, monkeypatch, capsys) -> None:
    _login(tmp_home)
    patch_api()
    _tty(monkeypatch, "y")  # a terminal is there, but --json never asks
    assert cli.main(["blog", "publish", "--room", _GID, _NID, "--json"]) == 2
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["code"] == "usage"
    assert not _api().sent("publish")


def test_publish_json_yes_prints_only_the_server_answer(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(publish={**_PUBLISHED, "copy": "written"})
    assert cli.main(["blog", "publish", "--room", _GID, _NID, "--yes", "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {**_PUBLISHED, "copy": "written"}
    assert captured.err == ""


def test_publish_stops_on_a_refused_check_without_asking(
    tmp_home, patch_api, monkeypatch, capsys
) -> None:
    _login(tmp_home)
    patch_api(check=_check(refusal="Only a text note can be published."))
    asked = _tty(monkeypatch, "y")
    assert cli.main(["blog", "publish", "--room", _GID, _NID, "--yes"]) == 1
    err = capsys.readouterr().err
    assert "Only a text note can be published." in err
    assert asked == []
    assert not _api().sent("publish")


def test_publish_notes_a_card_or_copy_that_could_not_be_written(
    tmp_home, patch_api, capsys
) -> None:
    _login(tmp_home)
    patch_api(publish={**_PUBLISHED, "card": "unavailable", "copy": "unavailable"})
    assert cli.main(["blog", "publish", "--room", _GID, _NID, "--yes"]) == 0
    out = capsys.readouterr().out
    assert "comments card could not be posted" in out
    assert "public room" in out
    assert "Republish to try again." in out


def test_publish_says_republished(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(publish={**_PUBLISHED, "republished": True})
    assert cli.main(["blog", "publish", "--room", _GID, _NID, "--yes"]) == 0
    assert "Republished." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# errors: one line each, from the check or the publish


@pytest.mark.parametrize("step", ["check", "publish"])
def test_401_says_blog_needs_a_persons_token(tmp_home, patch_api, capsys, step) -> None:
    _login(tmp_home)
    patch_api(**{step: _err(401, "invalid bearer token")})
    argv = ["blog", "stage" if step == "check" else "publish", "--room", _GID, _NID]
    if step == "publish":
        argv.append("--yes")
    assert cli.main(argv) == 1
    err = capsys.readouterr().err
    assert "401" in err and "your own token" in err
    assert _error_lines(err) == 1


@pytest.mark.parametrize(
    ("status", "detail", "code", "expect"),
    [
        (403, "Only an owner of this room can publish.", 1, "403: Only an owner"),
        (404, "Publishing is not available yet.", 2, "Publishing is not available yet."),
        (409, "The note changed since the check.", 1, "409: The note changed"),
    ],
)  # fmt: skip
def test_stage_show_and_publish_errors_are_one_line(
    tmp_home, patch_api, capsys, status, detail, code, expect
) -> None:
    _login(tmp_home)
    for step, argv in (
        ("check", ["blog", "stage", "--room", _GID, _NID]),
        ("publish", ["blog", "publish", "--room", _GID, _NID, "--yes"]),
    ):
        patch_api(**{"check": _err(status, detail)} if step == "check" else {})
        if step == "publish":
            patch_api(publish=_err(status, detail))
        assert cli.main(argv) == code
        err = capsys.readouterr().err
        assert expect in err
        assert _error_lines(err) == 1


def test_404_without_a_detail_uses_the_uniform_message(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(check=_err(404, None))
    assert cli.main(["blog", "stage", "--room", _GID, _NID]) == 2
    assert "no such room or note, or you are not a member" in capsys.readouterr().err


def test_422_surfaces_the_first_validation_message(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    body = json.dumps({"detail": [{"loc": ["body", "source"], "msg": "Field required"}]})
    patch_api(publish=_err(422, None, body=body))
    assert cli.main(["blog", "publish", "--room", _GID, _NID, "--yes"]) == 1
    err = capsys.readouterr().err
    assert "Server returned 422: Field required (body.source)" in err
    assert _error_lines(err) == 1


def test_422_with_an_unreadable_body_is_still_one_line(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(check=_err(422, None, body="<html>"))
    assert cli.main(["blog", "stage", "--room", _GID, _NID]) == 1
    assert capsys.readouterr().err.strip() == "error: Server returned 422."


def test_a_summary_the_server_refuses_is_its_message(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(publish=_err(400, "A summary is at most 280 characters."))
    rc = cli.main(["blog", "publish", "--room", _GID, _NID, "--yes", "--summary", "x" * 400])
    assert rc == 1
    assert "A summary is at most 280 characters." in capsys.readouterr().err


def test_errors_are_json_on_stderr_under_json(tmp_home, patch_api, capsys) -> None:
    _login(tmp_home)
    patch_api(check=_err(403, "Only an owner of this room can publish or withdraw its posts."))
    assert cli.main(["blog", "stage", "--room", _GID, _NID, "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)["error"]
    assert error["code"] == "forbidden" and error["status"] == 403


def test_publish_prints_the_full_addresses_the_server_names() -> None:
    from pepper_cli.blog_view import published_lines

    lines = published_lines(
        {
            **_PUBLISHED,
            "post_url": f"https://pepper.vycari.ai/blog/{_GID}/2026/11/19/hello-world",
            "public_url": "https://blog.vycari.ai/2026/11/19/hello-world",
        }
    )
    assert "Public: https://blog.vycari.ai/2026/11/19/hello-world" in lines
    assert f"Members: https://pepper.vycari.ai/blog/{_GID}/2026/11/19/hello-world" in lines
    assert f"/blog/{_GID}/2026/11/19/hello-world" not in lines


def test_publish_names_only_the_members_page_while_the_blog_is_not_public() -> None:
    from pepper_cli.blog_view import published_lines

    members = f"https://pepper.vycari.ai/blog/{_GID}/2026/11/19/hello-world"
    lines = published_lines({**_PUBLISHED, "post_url": members, "public_url": None})
    assert f"Members: {members}" in lines
    assert not any(line.startswith("Public:") for line in lines)
