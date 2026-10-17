"""Server text never reaches the terminal as control sequences or as rich markup.

Every printing path that carries server-provided text is fed one hostile
string — a clear-screen CSI, an OSC 8 hyperlink, an OSC 0 window title, a C1
CSI, a bare ESC, DEL, a bidi override, a zero-width space, a line break, and
rich ``[red]`` / ``[link=…]`` markup — and the captured output must hold none
of the control characters and show the markup literally.
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self

import httpx
import pytest
from rich.console import Console

from pepper_cli import api_client, cli, credentials, render
from pepper_cli.secret import SecretStr
from pepper_cli.terminal_text import make_visible, sanitize_line, sanitize_text

MARKUP = "[red]x[/red] [link=https://evil.example]y[/link]"
HOSTILE = (
    "A\x1b[2J\x1b[H"  # CSI clear screen + home
    "B\x1b]8;;https://evil.example/\x1b\\C\x1b]8;;\x1b\\"  # OSC 8 hyperlink
    "D\x1b]0;pwned\x07"  # OSC 0 window title
    "E\x9b2J"  # C1 CSI
    "F\x1bG"  # bare ESC
    "H\x7f"  # DEL
    "I\u202eJ\u200bK"  # bidi override, zero-width space
    "\r\nINJECTED " + MARKUP  # a line break that would forge a new output line
)

# Anything that must never come out: ESC, every C1 control, DEL, and the
# invisible format characters (zero-width, bidi marks/embeddings/overrides/
# isolates, BOM).
_FORBIDDEN = re.compile(r"[\x1b\x7f-\x9f\u061c\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")
_ALSO_FORBIDDEN_C0 = re.compile(r"[\x00-\x08\x0b-\x1f]")


def _assert_clean(out: str, *, one_line: bool) -> None:
    assert not _FORBIDDEN.search(out), repr(out)
    assert not _ALSO_FORBIDDEN_C0.search(out), repr(out)
    # The OSC title payload went with its sequence (a JSON-escaped event
    # payload may still show it, inertly, as ``\u001b]0;pwned``).
    assert not re.search(r"(?<!\\u001b\]0;)pwned", out), repr(out)
    assert "[red]x[/red]" in out, repr(out)  # markup printed literally...
    assert "[link=https://evil.example]y[/link]" in out, repr(out)
    if one_line:
        # ...and a one-line field cannot start a line of its own.
        assert not any(line.startswith("INJECTED") for line in out.splitlines()), repr(out)


# ---------------------------------------------------------------------------
# the helpers


def test_sanitize_text_keeps_newlines_and_tabs_and_drops_the_rest() -> None:
    assert sanitize_text("a\tb\nc\r\nd\re\x07f\x1b[31mg\u202eh") == "a\tb\nc\nd\nefgh"


def test_sanitize_line_turns_every_control_into_a_space() -> None:
    assert sanitize_line("a\tb\nc\r\nd\x07e\x1b[31mf") == "a b c d ef"


@pytest.mark.parametrize("fn", [sanitize_line, sanitize_text])
@pytest.mark.parametrize(
    "clean",
    [
        "",
        "plain",
        "  two  spaces  ",
        "019a2b3c-0000-7000-8000-000000000001",
        "2026-10-13 09:00 PDT",
        "café — naïve 日本語 🎉",
        "[red]markup is not a control[/red]",
    ],
)
def test_helpers_return_clean_text_unchanged(fn: Callable[[str], str], clean: str) -> None:
    assert fn(clean) == clean


@pytest.mark.parametrize("fn", [sanitize_line, sanitize_text])
def test_helpers_are_idempotent(fn: Callable[[str], str]) -> None:
    once = fn(HOSTILE)
    assert fn(once) == once


def test_an_escape_split_across_two_streamed_fragments_is_harmless() -> None:
    # A thought arrives in fragments; a sequence can straddle two of them.
    # The trailing bare ESC must go on its own, leaving only printable litter.
    assert sanitize_text("before\x1b") == "before"
    assert sanitize_text("[2Jafter") == "[2Jafter"


# ---------------------------------------------------------------------------
# every render path, captured


class _FakeTty(io.StringIO):
    def isatty(self) -> bool:
        return True


def _plain_console(buf: io.StringIO) -> Console:
    return Console(file=buf, force_terminal=False, width=1000)


def _hooked_console(buf: io.StringIO) -> Console:
    return render._make_console(file=buf, force_terminal=False, width=1000)


@pytest.fixture(params=["per-field", "with-backstop"])
def captured(request, monkeypatch) -> Callable[[], str]:
    """Point both render consoles at one buffer.

    ``per-field`` uses plain consoles, so the fields' own sanitizing is what
    is tested; ``with-backstop`` uses the shipped consoles with the render
    hook installed. Both must come out clean.
    """
    buf = io.StringIO()
    make = _plain_console if request.param == "per-field" else _hooked_console
    console = make(buf)
    monkeypatch.setattr(render, "_stdout", console)
    monkeypatch.setattr(render, "_stderr", console)
    tty = _FakeTty()
    monkeypatch.setattr(render, "sys", SimpleNamespace(stdout=tty))
    return lambda: buf.getvalue() + tty.getvalue()


H = HOSTILE


def _todo() -> api_client.TodoEntry:
    return api_client.TodoEntry(
        id=H,
        title=H,
        details=None,
        status="open",
        source="manual",
        priority=H,
        due_date=None,
        due_is_date_only=None,
        created_at="2026-10-13T00:00:00Z",
        done_at=None,
        source_ref=None,
    )


def _task_fields() -> dict[str, Any]:
    return {
        "id": H,
        "name": H,
        "description": H,
        "cron": H,
        "timezone": H,
        "destinations": [H],
        "active": True,
        "one_shot": False,
        "last_run_at": None,
        "next_run_at": H,
    }


def _run() -> api_client.ScheduledTaskRunEntry:
    return api_client.ScheduledTaskRunEntry(
        id=H,
        fired_at=H,
        finished_at=None,
        status=H,
        error=H,
        delivered_channels=[H],
        trajectory_id=H,
        reply_preview=None,
    )


def _message(**over: Any) -> api_client.GroupMessage:
    fields: dict[str, Any] = {
        "id": "m1",
        "is_pepper": False,
        "is_you": False,
        "is_deleted": False,
        "is_broken": False,
        "run_start": True,
        "display_name": H,
        "initial": "A",
        "avatar_tint": "#000",
        "raw_body": H,
        "rendered_html": "",
        "created_at": "2026-10-13T00:00:00Z",
        "created_at_display": H,
        "day_key": "2026-10-13",
        "day_kicker": "",
        "day_heading": "",
        "day_label": H,
        "quote_excerpt": "",
        "via_display_name": H,
        "is_edited": True,
        "reactions": [{"emoji": H, "count": 2}],
        "attachments": [{"filename": H}],
        "is_agent": True,
        "for_display_name": H,
    }
    fields.update(over)
    return api_client.GroupMessage(**fields)


# (name, call, one_line): ``one_line`` paths must also not let the text break
# its line; free-text paths may keep the line break.
_RENDER_PATHS: list[tuple[str, Callable[[], None], bool]] = [
    ("info", lambda: render.info(H), False),
    ("error", lambda: render.error(H), False),
    ("error soft_wrap", lambda: render.error(H, soft_wrap=True), False),
    ("styled info", lambda: render.info(render.styled(("note:", "yellow"), " ", H)), False),
    ("reply (tty)", lambda: render.render_reply(H), False),
    ("markdown (tty)", lambda: render.render_markdown(H), False),
    ("document (tty)", lambda: render.write_document(H), False),
    ("thought delta", lambda: render.render_thought_delta(H, first=True), False),
    ("thought block", lambda: render.end_thought(H, streamed=""), False),
    ("tool call", lambda: render.render_tool_call({"name": H, "args": {H: H}}), True),
    ("tool result", lambda: render.render_tool_result({"name": H, "text": H}), True),
    ("tool status", lambda: render.render_tool_result({"name": H, "status": H}), True),
    (
        "trajectory",
        lambda: render.render_trajectory(
            api_client.TrajectoryDetail(
                trajectory=api_client.TrajectoryRef(
                    id=H,
                    user_id=H,
                    channel=H,
                    conversation_key=H,
                    created_at=H,
                    closed_at=H,
                    meta={"code_sha": H},
                ),
                events=[
                    api_client.TrajectoryEvent(
                        id="e1", seq=1, event_type=H, payload={"t": H}, created_at=H
                    )
                ],
            )
        ),
        True,
    ),
    (
        "trajectory list",
        lambda: render.render_trajectory_list(
            [
                api_client.TrajectoryListEntry(
                    id=H,
                    channel=H,
                    created_at=H,
                    closed_at=None,
                    meta={},
                    event_count=1,
                    first_user_message=H,
                    group_id="g",
                    group_name=H,
                )
            ]
        ),
        True,
    ),
    ("todo list", lambda: render.render_todo_list([_todo()]), True),
    (
        "task list",
        lambda: render.render_scheduled_task_list(
            [api_client.ScheduledTaskSummary(**_task_fields())]
        ),
        True,
    ),
    (
        "room task list",
        lambda: render.render_scheduled_task_list(
            [api_client.RoomScheduledTaskSummary(**_task_fields(), delivery=H, last_run_status=H)]
        ),
        True,
    ),
    (
        "task detail",
        lambda: render.render_scheduled_task(
            api_client.ScheduledTaskDetail(**_task_fields(), latest_run=_run())
        ),
        False,  # the description is free text on its own line
    ),
    ("task runs", lambda: render.render_scheduled_task_runs([_run()]), True),
    (
        "task runs preview",
        lambda: render.render_scheduled_task_runs(
            [
                api_client.ScheduledTaskRunEntry(
                    id="r",
                    fired_at="x",
                    finished_at=None,
                    status="ok",
                    error=None,
                    delivered_channels=None,
                    trajectory_id=None,
                    reply_preview=H,
                )
            ]
        ),
        True,
    ),
    (
        "task thread",
        lambda: render.render_scheduled_task_thread(
            [
                api_client.ScheduledTaskMessage(
                    id="m", role=H, text=H, channel="c", trajectory_id=None, created_at=H
                )
            ]
        ),
        False,
    ),
    (
        "group list",
        lambda: render.render_group_list(
            api_client.GroupList(
                enabled=True,
                degraded=False,
                total=1,
                archived_total=0,
                groups=[
                    api_client.GroupSummary(
                        group_id=H,
                        display_name=H,
                        color="#000",
                        sub=H,
                        member_count=2,
                        archived=False,
                    )
                ],
            )
        ),
        True,
    ),
    (
        "group topics",
        lambda: render.render_group_topics(
            [api_client.GroupTopicSummary(topic_id=H, name=H, last_activity_at=H)]
        ),
        True,
    ),
    (
        "group roster",
        lambda: render.render_group_roster(
            [api_client.GroupRosterMember(person_id=H, display_name=H)]
        ),
        True,
    ),
    (
        "group detail",
        lambda: render.render_group_detail(
            {
                "display_name": H,
                "kind": H,
                "group_id": H,
                "members": [
                    {"display_name": H, "role": H, "is_agent": True, "sponsor_display_name": H}
                ],
                "topics": [{"name": H}],
                "apps": {"installed": [{"title": H}]},
            }
        ),
        True,
    ),
    (
        "note list",
        lambda: render.render_note_list(
            api_client.ArtifactListResponse(
                truncated=False,
                items=[
                    api_client.ArtifactListItem(
                        id=H, name=H, kind="inline_text", updated_at=H, etag="e", labels=[H]
                    )
                ],
            )
        ),
        True,
    ),
    (
        "group messages",
        lambda: render.render_group_messages(
            api_client.GroupMessagePage(messages=[_message()], next_before=H)
        ),
        False,  # the message body is free text on its own line
    ),
    (
        "deleted group message",
        lambda: render.render_group_messages(
            api_client.GroupMessagePage(messages=[_message(is_deleted=True)])
        ),
        True,
    ),
    (
        "calendar",
        lambda: render.render_calendar(
            {
                "window": [{"date": "2026-10-13", "label": H, "date_label": H}],
                "days": [
                    {
                        "date": "2026-10-13",
                        "events": [
                            {
                                "summary": H,
                                "start_label": H,
                                "end_label": H,
                                "location": H,
                                "conference": {"url": "u", "name": H},
                            }
                        ],
                        "deadlines": [{"title": H, "time": H}],
                        "meals": [{"name": H, "slot": H}],
                    }
                ],
            },
            mode="schedule",
        ),
        True,
    ),
    (
        "next",
        lambda: render.render_next(
            {
                "note": H,
                "now": {
                    "title": H,
                    "start_label": H,
                    "end_label": H,
                    "minutes_left": 5,
                    "detail": H,
                },
                "next_commitment": {"title": H, "start_label": H, "in_minutes": 5},
                "due_today": [{"title": H}],
                "calendar": [
                    {"title": H, "start_label": H, "end_label": H, "detail": H},
                    {"kind": "deadline", "title": H, "start_label": H, "passed": True},
                    {"kind": "deadline", "title": H, "start_label": H, "source_time": "remind"},
                ],
                "todos": {
                    "count": 12,
                    "items": [{"title": H, "reasons": [H], "source": H}],
                },
            }
        ),
        True,
    ),
]


@pytest.mark.parametrize(
    ("call", "one_line"),
    [pytest.param(call, one_line, id=name) for name, call, one_line in _RENDER_PATHS],
)
def test_render_path_prints_no_controls_and_literal_markup(
    captured, call: Callable[[], None], one_line: bool
) -> None:
    call()
    _assert_clean(captured(), one_line=one_line)


def test_backstop_strips_controls_from_anything_printed(monkeypatch) -> None:
    buf = io.StringIO()
    monkeypatch.setattr(render, "_stdout", _hooked_console(buf))
    render._stdout.print(HOSTILE, markup=False)
    assert not _FORBIDDEN.search(buf.getvalue())


def test_backstop_keeps_rich_styling(monkeypatch) -> None:
    buf = io.StringIO()
    console = render._make_console(file=buf, force_terminal=True, color_system="standard")
    monkeypatch.setattr(render, "_stderr", console)
    render.error("boom")
    out = buf.getvalue()
    assert "\x1b[31m" in out  # rich's own red survives the hook
    assert "boom" in out


def test_piped_markdown_and_document_stay_byte_exact(monkeypatch) -> None:
    piped = io.StringIO()
    monkeypatch.setattr(render, "sys", SimpleNamespace(stdout=piped))
    render.render_markdown(HOSTILE)
    render.write_document(HOSTILE)
    assert piped.getvalue() == HOSTILE + HOSTILE


# ---------------------------------------------------------------------------
# the ApiError boundary and the CLI's own error paths


def _error_response(detail: str, status: int = 500) -> httpx.Response:
    request = httpx.Request("GET", "http://example.com/api/v1/whoami")
    return httpx.Response(status, json={"detail": detail}, request=request)


def test_api_error_detail_is_cleaned_where_it_is_built() -> None:
    resp = _error_response(HOSTILE)
    with pytest.raises(api_client.ApiError) as info:
        api_client._raise_for_error(resp)
    exc = info.value
    assert exc.detail == sanitize_text(HOSTILE)
    assert not _FORBIDDEN.search(str(exc))
    assert exc.body == resp.text  # the raw body is kept for parsing


def test_report_api_error_prints_no_controls(capsys) -> None:
    exc = api_client.ApiError(status=500, body="{}", detail=HOSTILE)
    assert cli._report_api_error(exc) == 1
    _assert_clean(capsys.readouterr().err, one_line=False)


def test_exc_interpolation_site_prints_no_controls(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    def refuse(_server_url: str, _device_name: str) -> None:
        raise api_client.ApiError(status=422, body="{}", detail=HOSTILE)

    monkeypatch.setattr(api_client, "device_start", refuse)
    assert cli.main(["login", "--server", "http://example.com"]) == 1
    err = capsys.readouterr().err
    assert "Server rejected the device-login request: HTTP 422:" in err
    _assert_clean(err, one_line=False)


# ---------------------------------------------------------------------------
# lines the CLI writes without rich


class _FakeApi:
    raw: dict[str, Any] = {}

    def __init__(self, *, server_url: str, token: SecretStr) -> None:
        del server_url, token

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def whoami(self) -> api_client.Whoami:
        return api_client.Whoami(user_id=H, email=H, display_name=H, timezone=H)

    def list_skills(self) -> list[api_client.SkillSummary]:
        return [
            api_client.SkillSummary(name=H, description=H),
            api_client.SkillSummary(name=H, description=H, builtin=True),
        ]

    def trajectory_get_raw(self, _trajectory_id: str) -> dict[str, Any]:
        return self.raw


@pytest.fixture
def logged_in(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    credentials.save(server_url="https://pepper.test", token=SecretStr("pep_pat_test"))
    monkeypatch.setattr(api_client, "ApiClient", _FakeApi)


def _assert_plain_lines_clean(out: str) -> None:
    assert not _FORBIDDEN.search(out), repr(out)
    assert not _ALSO_FORBIDDEN_C0.search(out), repr(out)
    assert not any(line.startswith("INJECTED") for line in out.splitlines()), repr(out)
    assert MARKUP in out


@pytest.mark.usefixtures("logged_in")
@pytest.mark.parametrize("argv", [["whoami"], ["skills", "list"]])
def test_plain_stdout_lines_are_cleaned(argv: list[str], capsys) -> None:
    assert cli.main(argv) == 0
    _assert_plain_lines_clean(capsys.readouterr().out)


def test_room_skill_and_watch_lines_are_cleaned() -> None:
    row = api_client.RoomSkill(
        id=H,
        name=H,
        description=H,
        state="active",
        granted_by=H,
        owner_is_you=False,
        can_revoke=True,
        ungrantable_toolsets=[],
        toolsets=[H],
    )
    _assert_plain_lines_clean(cli._room_skill_line(row) + "\n")
    line = cli._watch_event_line(
        {"id": 1, "kind": H, "group_id": H, "person_id": H, "payload": {"topic_id": H}},
        as_json=False,
        cursor=1,
    )
    _assert_plain_lines_clean(line + "\n")


@pytest.mark.usefixtures("logged_in")
def test_json_output_is_byte_exact_with_what_the_server_sent(capsys) -> None:
    raw = {"trajectory": {"id": H, "channel": H}, "events": [{"payload": {"text": H}}]}
    _FakeApi.raw = raw
    trajectory_id = "019a2b3c-0000-7000-8000-000000000001"
    assert cli.main(["trajectories", "show", trajectory_id, "--json"]) == 0
    assert capsys.readouterr().out == json.dumps(raw, indent=2) + "\n"


def test_watch_json_line_is_left_as_the_server_sent_it() -> None:
    event = {"id": 1, "kind": H}
    line = cli._watch_event_line(event, as_json=True, cursor=1)
    assert json.loads(line) == {**event, "cursor": 1}


# ---------------------------------------------------------------------------
# the note-conflict diff: differences must stay visible, controls must not run


def test_make_visible_spells_out_controls_and_keeps_newlines_and_tabs() -> None:
    assert make_visible("a\tb\nc\x1b[2Jd\re\u202ef") == "a\tb\nc\\x1b[2Jd\\re\\u202ef"
    assert make_visible("plain [red]x[/red] caf\u00e9") == "plain [red]x[/red] caf\u00e9"


def test_note_diff_shows_a_change_that_is_only_an_escape_sequence(capsys) -> None:
    cli._print_note_diff("hello\nworld\n", "hello\x1b[2J\nworld\r\n")
    err = capsys.readouterr().err
    assert not _FORBIDDEN.search(err), repr(err)
    assert "\r" not in err
    assert "-hello\n" in err
    assert "+hello\\x1b[2J\n" in err
    assert "+world\\r\n" in err


def test_note_diff_of_hostile_bodies_prints_no_controls(capsys) -> None:
    cli._print_note_diff(HOSTILE, HOSTILE + "\nmore")
    err = capsys.readouterr().err
    assert not _FORBIDDEN.search(err), repr(err)
    assert not _ALSO_FORBIDDEN_C0.search(err), repr(err)
