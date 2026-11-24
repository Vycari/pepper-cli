"""Conversation cards in the TUI: nesting, the fold, the bump and the face."""

from __future__ import annotations

import io

from rich.console import Console, Group

from pepper_cli import api_client
from pepper_cli.tui import cards, rows
from tests import tui_support as ts


def _m(mid: str, minute: int, body: str = "hello", **over: object) -> api_client.GroupMessage:
    at = f"2026-09-16T17:{minute:02d}:00+00:00"
    return api_client.group_message_from_dict(ts.message(mid, at, body, **over))


def _text(items: list, width: int = 60) -> str:
    console = Console(width=width, file=io.StringIO(), record=True, force_terminal=False)
    console.print(Group(*(r for _, r in items)))
    return console.export_text()


def _ids(stream: cards.Stream) -> list[str]:
    return [e.id for e in stream.entries]


def test_a_reply_and_a_reply_to_it_nest_under_their_card() -> None:
    root = _m("a", 0, "# Cabin\nBooked it")
    reply = _m("b", 1, "dog?", parent_message_id="a")
    deeper = _m("c", 2, "yes", parent_message_id="b", display_name="Pepper", is_pepper=True)
    loose = _m("d", 3, "elsewhere")
    stream = cards.build_stream([root, reply, deeper, loose], takes_cards=True)
    card = stream.card("a")
    assert card is not None
    assert [r.message.id for r in card.replies] == ["b", "c"]
    assert [r.to_card for r in card.replies] == [True, False]
    assert stream.owner["c"] == "a"
    assert _ids(stream) == ["a", "d"]
    assert isinstance(stream.entries[1], cards.Card)  # every top-level message is a card


def test_a_bridged_topic_stays_flat_but_note_comments_still_nest() -> None:
    root = _m("a", 0)
    reply = _m("b", 1, parent_message_id="a")
    stream = cards.build_stream([root, reply], takes_cards=False)
    assert all(not isinstance(e, cards.Card) for e in stream.entries)
    comment = _m("n", 0, "nice line", anchor={"exact": "the passage"})
    on_it = _m("o", 1, parent_message_id="n")
    stream = cards.build_stream([comment, on_it], takes_cards=False)
    assert _ids(stream) == ["n"]


def test_activity_lines_mirrors_and_blocked_stubs_are_never_cards() -> None:
    rows_in = [
        _m("a", 0, is_activity=True),
        _m("b", 1, is_bridged=True),
        _m("c", 2, is_blocked_collapsed=True),
        _m("d", 3, is_broken=True),
    ]
    stream = cards.build_stream(rows_in, takes_cards=True)
    assert all(not isinstance(e, cards.Card) for e in stream.entries)


def test_a_deleted_reply_is_not_drawn_but_its_replies_stay_in_the_card() -> None:
    root = _m("a", 0)
    gone = _m("b", 1, parent_message_id="a", is_deleted=True)
    under = _m("c", 2, parent_message_id="b")
    card = cards.build_stream([root, gone, under], takes_cards=True).card("a")
    assert card is not None
    assert [r.message.id for r in card.replies] == ["c"]


def test_a_deleted_root_is_a_card_while_it_has_a_live_reply() -> None:
    root = _m("a", 0, is_deleted=True)
    reply = _m("b", 1, parent_message_id="a")
    assert cards.build_stream([root, reply], takes_cards=True).card("a") is not None
    tomb = cards.build_stream([root], takes_cards=True)
    assert tomb.card("a") is None and _ids(tomb) == ["a"]  # a plain tombstone
    gone = _m("b", 1, parent_message_id="a", is_deleted=True)
    assert cards.build_stream([root, gone], takes_cards=True).entries == []


def test_a_reply_from_someone_else_bumps_its_card_but_your_own_does_not() -> None:
    old = _m("a", 0)
    mid = _m("b", 1)
    theirs = _m("c", 2, parent_message_id="a")
    assert _ids(cards.build_stream([old, mid, theirs], takes_cards=True)) == ["b", "a"]
    mine = _m("c", 2, parent_message_id="a", is_you=True)
    assert _ids(cards.build_stream([old, mid, mine], takes_cards=True)) == ["a", "b"]


def test_a_held_card_keeps_its_place_until_let_go() -> None:
    old, mid = _m("a", 0), _m("b", 1)
    theirs = _m("c", 2, parent_message_id="a")
    held = cards.build_stream([old, mid, theirs], takes_cards=True, holds={"a": "a"})
    assert _ids(held) == ["a", "b"]
    holds = cards.settle_holds({}, {"a"}, {"a": "a"})
    assert holds == {"a": "a"}
    assert cards.settle_holds(holds, set(), {"a": "c"}) == {}
    # A hold whose placement is no longer one of the card's messages falls back to the rule.
    assert _ids(cards.build_stream([old, mid, theirs], takes_cards=True, holds={"a": "zz"})) == [
        "b",
        "a",
    ]


def test_hold_ids_are_the_selected_card_and_the_one_a_reply_goes_into() -> None:
    stream = cards.build_stream(
        [_m("a", 0), _m("b", 1, parent_message_id="a"), _m("c", 2)], takes_cards=True
    )
    assert cards.hold_ids(stream, selected="b", replying_to="c") == {"a", "c"}
    assert cards.hold_ids(stream, selected=None, replying_to=None) == set()


def test_the_fold_shows_the_latest_three() -> None:
    replies = [_m(f"r{i}", i + 1, f"reply {i}", parent_message_id="a") for i in range(5)]
    card = cards.build_stream([_m("a", 0), *replies], takes_cards=True).card("a")
    assert card is not None
    hidden, shown = card.shown(expanded=False)
    assert hidden == 2
    assert [r.message.id for r in shown] == ["r2", "r3", "r4"]
    assert card.shown(expanded=True)[0] == 0


def test_the_face_uses_the_server_title_and_drops_the_line_it_took() -> None:
    heading = _m(
        "a", 0, "# Launch plan\nShip it Friday", title="Launch plan", title_source="heading"
    )
    assert cards.face(heading) == cards.Face("Launch plan", "Ship it Friday")
    line = _m("b", 0, "Lunch?\nNoon works", title="Lunch?", title_source="first_line")
    assert cards.face(line) == cards.Face("Lunch?", "Noon works")
    long_line = "word " * 40
    cut = _m("c", 0, long_line, title="word word…", title_source="first_line")
    assert cards.face(cut).body == long_line.strip()


def test_the_face_falls_back_to_the_text_from_an_older_server() -> None:
    assert cards.face(_m("a", 0, "## Trip\nDetails")) == cards.Face("Trip", "Details")
    assert cards.face(_m("b", 0, "Short line\nmore")) == cards.Face("Short line", "more")
    long_line = "x" * 150
    face = cards.face(_m("c", 0, long_line))
    assert face.title.endswith("…") and len(face.title) == cards.TITLE_LIMIT
    assert face.body == long_line
    assert cards.split_title("") == ("Untitled", False)


def test_chips_leave_out_removed_links_and_count_the_rest() -> None:
    links = [{"type": "Note", "label": f"n{i}", "href": "/x"} for i in range(6)]
    links.append({"type": "Source", "label": "gone", "removed": True})
    shown, more = cards.chips(_m("a", 0, links=links))
    assert len(shown) == cards.CHIPS_ON_CARD and more == 2


def test_a_card_draws_its_title_body_thread_and_foot() -> None:
    root = _m(
        "a",
        0,
        "# Cabin\nBooked it",
        title="Cabin",
        title_source="heading",
        links=[{"type": "Note", "label": "Trip plan", "href": "/n"}],
    )
    reply = _m("b", 1, "Can we bring the dog?", parent_message_id="a", display_name="Sam")
    stream = cards.build_stream([root, reply], takes_cards=True)
    items = rows.thread_items([root, reply], you_id=None, stream=stream)
    out = _text(items)
    assert "╭─ Cabin" in out
    assert "Booked it" in out
    assert "# Cabin" not in out
    assert "Note · Trip plan" in out
    assert "Sam" in out and "Can we bring the dog?" in out
    assert "1 reply · r reply" in out
    keys = [k for k, _ in items if k]
    assert keys == ["msg:a", "msg:b"]


def test_a_folded_card_has_a_selectable_fold_row() -> None:
    replies = [_m(f"r{i}", i + 1, f"reply {i}", parent_message_id="a") for i in range(5)]
    messages = [_m("a", 0), *replies]
    stream = cards.build_stream(messages, takes_cards=True)
    folded = rows.thread_items(messages, you_id=None, stream=stream)
    assert "fold:a" in [k for k, _ in folded]
    assert "2 earlier replies" in _text(folded)
    assert "reply 0" not in _text(folded)
    opened = rows.thread_items(messages, you_id=None, stream=stream, expanded={"a"})
    assert "reply 0" in _text(opened)


def test_a_reply_to_the_card_draws_no_quote_but_a_reply_to_a_reply_does() -> None:
    root = _m("a", 0, "Root")
    direct = _m("b", 1, "direct", parent_message_id="a", quote={"author": "Ada", "excerpt": "Root"})
    deeper = _m(
        "c", 2, "deeper", parent_message_id="b", quote={"author": "Ada", "excerpt": "direct"}
    )
    messages = [root, direct, deeper]
    out = _text(
        rows.thread_items(
            messages, you_id=None, stream=cards.build_stream(messages, takes_cards=True)
        )
    )
    assert "┃ Ada: Root" not in out
    assert "┃ Ada: direct" in out


def test_a_pending_reply_is_drawn_in_its_card() -> None:
    from pepper_cli.tui import state

    root, other = _m("a", 0, "Root"), _m("z", 1, "Later")
    conv = state.Conversation("g", "t")
    pending = state.PendingSend(key="k", conversation=conv, body="on my way", parent_id="a")
    stream = cards.build_stream([root, other], takes_cards=True)
    items = rows.thread_items([root, other], you_id=None, pending=[pending], stream=stream)
    keys = [k for k, _ in items]
    assert keys.index("pending:k") < keys.index("msg:z")


def test_a_title_too_wide_for_the_rule_leaves_the_whole_line_in_the_card() -> None:
    line = "We should decide on the dates for the cabin trip before the end of the month"
    root = _m("a", 0, f"{line}\nthoughts?")
    items = rows.thread_items(
        [root], you_id=None, stream=cards.build_stream([root], takes_cards=True)
    )
    assert "end of the month" not in _text(items, width=120).split("╮")[1]
    narrow = _text(items, width=40)
    # The rule cuts the title, so the face carries the line in full (wrapped).
    assert "cabin trip" in narrow and "month" in narrow and "thoughts?" in narrow


def test_a_card_face_that_mentions_you_is_highlighted() -> None:
    root = _m("a", 0, "Plans\nask @Me about it", mentions=[ts.ME_PERSON], mention_names=["Me"])
    items = rows.thread_items(
        [root], you_id=ts.ME_PERSON, stream=cards.build_stream([root], takes_cards=True)
    )
    console = Console(width=60, file=io.StringIO(), record=True, force_terminal=True)
    console.print(Group(*(r for _, r in items)))
    body_line = next(
        line for line in console.export_text(styles=True).splitlines() if "ask" in line
    )
    assert "\x1b[1;33m" in body_line  # the viewer's accent
