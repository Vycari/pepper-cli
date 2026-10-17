"""P3 rendering: markdown bodies, link previews, app cards and reaction chips."""

from __future__ import annotations

import io

from rich.console import Console, RenderableType
from rich.markdown import Markdown
from rich.text import Text

from pepper_cli import api_client
from pepper_cli.tui import rows
from tests import tui_support as ts

CARD = {
    "kind": "card",
    "app_key": "notes",
    "slug": "n",
    "title": "Trip notes",
    "principal": "p",
    "url": "/apps/n",
    "summary": {"lines": ["3 open items", "next: book flights"]},
}


def _text(renderable: RenderableType, width: int = 60) -> str:
    # ``legacy_windows=False``: on a Windows runner with no console Rich detects a legacy
    # console and swaps rounded boxes for square ones. Textual never draws through a legacy
    # console, so the TUI always gets the rounded box this checks for.
    console = Console(
        width=width,
        file=io.StringIO(),
        record=True,
        force_terminal=False,
        legacy_windows=False,
    )
    console.print(renderable)
    return console.export_text()


def _m(**over: object) -> api_client.GroupMessage:
    return api_client.group_message_from_dict(ts.message("m1", "2026-09-16T17:30:00+00:00", **over))


def test_markdown_syntax_renders_and_plain_text_keeps_its_line_breaks() -> None:
    assert rows.looks_like_markdown("some **bold** words")
    assert rows.looks_like_markdown("# Title")
    assert rows.looks_like_markdown("- one\n- two")
    assert rows.looks_like_markdown("see [docs](https://a.example/x)")
    assert not rows.looks_like_markdown("just a sentence, 2 * 3 = 6")
    plain = _text(rows.message_renderable(_m(raw_body="line one\nline two")))
    assert "line one\n" in plain
    assert "line two" in plain
    rendered = _text(rows.message_renderable(_m(raw_body="a **bold** word")))
    assert "a bold word" in rendered
    assert "**" not in rendered


def test_markdown_shows_a_link_as_text_and_url_never_as_a_terminal_hyperlink() -> None:
    body = "open [the plan](https://a.example/plan) now"
    group = rows.message_renderable(_m(raw_body=body, run_start=False))
    markdown = next(r for r in group.renderables if hasattr(r, "renderable"))
    assert isinstance(markdown.renderable, Markdown)
    assert markdown.renderable.hyperlinks is False
    out = _text(group)
    assert "the plan (https://a.example/plan)" in out


def test_markdown_bodies_are_never_rich_markup_and_controls_are_stripped() -> None:
    out = _text(rows.message_renderable(_m(raw_body="**[bold red]x[/]** \x1b]0;pwn\x07 `y`")))
    assert "[bold red]x[/]" in out
    assert "\x1b" not in out
    assert "\x07" not in out


def test_an_edited_markdown_body_gets_its_marker_on_its_own_line() -> None:
    out = _text(rows.message_renderable(_m(raw_body="a **b**", is_edited=True, run_start=False)))
    assert "(edited)" in out
    assert out.strip().splitlines()[-1].strip() == "(edited)"


def test_link_preview_is_one_line_with_title_and_domain() -> None:
    preview = {"url": "https://www.example.com/a/b?q=1", "title": "A fine article"}
    out = _text(rows.message_renderable(_m(raw_body="hi", link_previews=[preview])))
    assert "↗ A fine article — example.com" in out
    long = {"url": "https://example.com/", "title": "t" * 200}
    lines = _text(rows.message_renderable(_m(raw_body="hi", link_previews=[long]))).splitlines()
    assert sum("↗" in line for line in lines) == 1  # truncated, not wrapped


def test_link_preview_without_a_title_uses_its_url_and_a_covered_one_is_hidden() -> None:
    bare = {"url": "https://example.com/x", "title": ""}
    out = _text(rows.message_renderable(_m(raw_body="hi", link_previews=[bare])))
    assert "↗ https://example.com/x" in out
    covered = {"url": "https://example.com/y", "title": "Covered", "covered_by_card": True}
    assert "↗" not in _text(rows.message_renderable(_m(raw_body="hi", link_previews=[covered])))


def test_preview_text_is_sanitized_and_never_markup() -> None:
    nasty = {"url": "https://example.com/", "title": "[bold]hi[/bold]\x1b[31m"}
    out = _text(rows.message_renderable(_m(raw_body="hi", link_previews=[nasty])))
    assert "[bold]hi[/bold]" in out
    assert "\x1b" not in out


def test_a_preview_with_a_malformed_url_still_draws() -> None:
    bad = {"url": "https://[", "title": "Broken link"}
    out = _text(rows.message_renderable(_m(raw_body="hi", link_previews=[bad])))
    assert "Broken link" in out


def test_app_card_is_a_box_with_title_summary_and_the_open_hint() -> None:
    out = _text(rows.message_renderable(_m(raw_body="see", cards=[CARD])))
    lines = out.splitlines()
    assert any(line.strip().startswith("╭") for line in lines)
    assert any(line.strip().startswith("╰") for line in lines)
    for needle in ("Trip notes", "3 open items", "next: book flights", "open in web (o)"):
        assert needle in out


def test_card_text_is_plain_even_when_it_looks_like_markup() -> None:
    card = {**CARD, "title": "[red]T[/red]", "summary": {"lines": ["\x1b[2Jline"]}}
    out = _text(rows.message_renderable(_m(raw_body="see", cards=[card])))
    assert "[red]T[/red]" in out
    assert "\x1b" not in out


def test_reaction_chips_mark_only_your_own_in_bold() -> None:
    chips = [
        {"emoji": "👍", "count": 2, "mine": True},
        {"emoji": "❤️", "count": 1, "mine": False},
    ]
    line = rows._reaction_line(chips)
    assert isinstance(line, Text)
    assert line.plain.strip() == "👍 2  ❤️ 1"
    bold = [s for s in line.spans if "bold" in str(s.style)]
    assert [line.plain[s.start : s.end] for s in bold] == ["👍 2"]
    assert rows._reaction_line([]) is None
