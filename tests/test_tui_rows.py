"""Rendering of messages for the TUI thread pane."""

from __future__ import annotations

import io

from rich.console import Console, RenderableType

from pepper_cli import api_client, render
from pepper_cli.tui import rows
from tests import tui_support as ts


def _text(renderable: RenderableType, width: int = 60) -> str:
    console = Console(width=width, file=io.StringIO(), record=True, force_terminal=False)
    console.print(renderable)
    return console.export_text()


def _m(**over: object) -> api_client.GroupMessage:
    return api_client.group_message_from_dict(ts.message("m1", "2026-09-16T17:30:00+00:00", **over))


def test_name_line_only_on_a_run_start_with_the_time_right_aligned() -> None:
    first = _text(rows.message_renderable(_m(raw_body="one")))
    assert first.splitlines()[0].startswith("Ada")
    assert first.splitlines()[0].rstrip().endswith("5:30 PM")
    follow = _text(rows.message_renderable(_m(raw_body="two", run_start=False)))
    assert "Ada" not in follow
    assert follow.strip() == "two"


def test_bodies_are_never_markup_and_controls_are_stripped() -> None:
    body = "[bold red]loud[/] \x1b]0;pwn\x07 [link=http://x]y[/link]"
    out = _text(rows.message_renderable(_m(raw_body=body)))
    assert "[bold red]loud[/]" in out
    assert "\x1b" not in out
    assert "[link=http://x]" in out


def test_tombstone_and_broken_rows_are_a_dim_sentence() -> None:
    assert "Message deleted" in _text(
        rows.message_renderable(_m(is_deleted=True, raw_body="secret"))
    )
    assert "secret" not in _text(rows.message_renderable(_m(is_deleted=True, raw_body="secret")))
    assert "Couldn't show this message" in _text(rows.message_renderable(_m(is_broken=True)))


def test_tags_edited_quote_mentions_attachments_and_reactions() -> None:
    m = _m(
        raw_body="hi @Bob",
        via_display_name="Pepper",
        is_edited=True,
        quote_excerpt="hi @Bob",
        quote={"id": "p1", "author": "Ada", "excerpt": "earlier words", "deleted": False},
        mention_names=["Bob"],
        mentions=[ts.ME],
        attachments=[{"filename": "plan.pdf", "size_bytes": 2048}, {"id": "x", "size": "?"}],
        reactions=[{"emoji": "👍", "count": 2}, {"emoji": "", "count": ""}],
    )
    out = _text(rows.message_renderable(m, you_id=ts.ME))
    assert "(via Pepper)" in out
    assert "(edited)" in out
    assert "┃ Ada: earlier words" in out
    assert "┃ hi" not in out  # its own quote_excerpt is not a quote
    assert "@Bob" in out
    assert "📎 plan.pdf (2.0 KB)" in out
    assert "📎 x" in out
    assert "👍 2" in out
    assert out.splitlines()[0].rstrip().endswith("5:30 PM")
    assert " @" in out.splitlines()[0]  # the mention marker beside the name


def test_agent_tag_and_unsafe_colour() -> None:
    out = _text(
        rows.message_renderable(_m(is_agent=True, for_display_name="Ada", avatar_tint="red; x"))
    )
    assert "(agent, for Ada)" in out
    assert rows.safe_color("#A1b2C3") == "#A1b2C3"
    assert rows.safe_color("t1") is None
    assert rows.safe_color(None) is None


def test_human_size() -> None:
    assert rows._human_size(5) == "5 B"
    assert rows._human_size(3 * 1024 * 1024) == "3.0 MB"
    assert rows._human_size(-1) == ""
    assert rows._human_size(True) == ""
    assert rows._human_size("big") == ""


def test_day_dividers_appear_on_a_new_day_and_skip_placeholders() -> None:
    first = _m(day_key="2026-09-16", day_label="Today")
    again = api_client.group_message_from_dict(
        ts.message("m2", "2026-09-16T18:00:00+00:00", day_key="2026-09-16", day_label="Today")
    )
    tomorrow = api_client.group_message_from_dict(
        ts.message("m3", "2026-09-17T08:00:00+00:00", day_key="2026-09-17", day_label="Tomorrow")
    )
    placeholder = api_client.group_message_from_dict(
        ts.message("m4", "2026-09-17T09:00:00+00:00", day_key="", day_label="", is_broken=True)
    )
    out = _text(rows.thread_renderable([first, again, tomorrow, placeholder], you_id=None))
    assert out.count("── Today ──") == 1
    assert out.count("── Tomorrow ──") == 1
    assert out.count("──") == 4
    assert "(no messages)" in _text(rows.thread_renderable([], you_id=None))


def test_dm_rows_name_the_speaker_on_change_and_localize_time(machine_zone) -> None:
    machine_zone("America/Los_Angeles")
    history = [
        api_client.ChatHistoryEntry("1", "user", "hi\nthere", "terminal", "2026-09-16T17:30:00Z"),
        api_client.ChatHistoryEntry("2", "user", "again", "terminal", "2026-09-16T17:31:00Z"),
        api_client.ChatHistoryEntry(
            "3",
            "assistant",
            "hello",
            "terminal",
            "2026-09-16T17:31:30Z",
            attachments=[{"filename": "a.txt", "size_bytes": 3}],
            reactions=[{"emoji": "❤", "count": 1}],
        ),
    ]
    out = _text(rows.dm_renderable(history))
    assert "── Today ──" in out
    assert out.count("You") == 1
    assert "10:30 AM" in out
    assert "Pepper" in out
    assert "📎 a.txt (3 B)" in out
    assert "❤ 1" in out
    assert "10:30" in _text(rows.dm_renderable(history, hour24=True))
    assert "(no messages yet today)" in _text(rows.dm_renderable([]))


def test_local_clock_formats_without_platform_specific_directives(machine_zone) -> None:
    from datetime import UTC, datetime

    machine_zone("Asia/Kolkata")
    moment = datetime(2026, 1, 1, 0, 5, tzinfo=UTC)
    assert render.local_clock(moment) == "5:35 AM"
    assert render.local_clock(datetime(2026, 1, 1, 12, 0, tzinfo=UTC), hour24=True) == "17:30"


def test_own_quote_excerpt_is_not_rendered_as_a_quote() -> None:
    # The server fills quote_excerpt with the message's own text on every row.
    out = _text(rows.message_renderable(_m(raw_body="same words", quote_excerpt="same words")))
    assert "┃" not in out
    assert out.count("same words") == 1


def test_quote_edge_cases() -> None:
    deleted = _m(quote={"id": "p", "author": "Ada", "excerpt": "gone", "deleted": True})
    assert "┃ (deleted message)" in _text(rows.message_renderable(deleted))
    assert "gone" not in _text(rows.message_renderable(deleted))
    no_author = _text(rows.message_renderable(_m(quote={"excerpt": "words"})))
    assert "┃ words" in no_author
    for junk in ({}, {"author": "", "excerpt": ""}, "not a dict"):
        assert "┃" not in _text(rows.message_renderable(_m(quote=junk)))
