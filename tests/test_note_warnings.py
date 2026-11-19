"""The text half of #318: which picture references in a note readers won't see."""

from __future__ import annotations

from pepper_cli import note_warnings

_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"


def _kinds(body: str) -> list[str]:
    return [w.kind for w in note_warnings.scan(body).warnings]


def test_obsidian_embeds_are_flagged_in_order() -> None:
    found = note_warnings.scan("![[a.png]] then ![[notes/b.svg|300]]")
    assert [w.reference for w in found.warnings] == ["![[a.png]]", "![[notes/b.svg|300]]"]
    assert {w.kind for w in found.warnings} == {"obsidian_embed"}


def test_a_wikilink_to_a_picture_file_is_flagged_but_a_note_link_is_not() -> None:
    assert _kinds("see [[chart.PNG]] and [[Packing list]] and [[Trip#Day 1]]") == ["obsidian_embed"]


def test_a_local_markdown_image_is_flagged() -> None:
    assert _kinds("![x](attachments/chart.png) ![y](<my chart.png>)") == [
        "local_file",
        "local_file",
    ]


def test_urls_and_site_paths_are_not_flagged() -> None:
    body = "![](https://example.com/a.png) ![](/static/a.png) ![](data:image/png;base64,AA)"
    assert _kinds(body) == []


def test_picture_links_are_returned_for_the_room_check_once_each() -> None:
    upper = _ID.upper()
    body = (
        f'![](/artifacts/{_ID}/raw) ![t](/artifacts/{upper}/raw "title") '
        f"![](https://pepper.example/artifacts/{_ID}/raw)"
    )
    found = note_warnings.scan(body)
    assert found.warnings == ()
    assert found.picture_ids == (_ID,)


def test_code_is_left_out() -> None:
    body = "```md\n![[a.png]]\n![](x.png)\n```\n\nInline `![[b.png]]` too.\n\n~~~\n[[c.png]]\n~~~\n"
    assert note_warnings.scan(body) == note_warnings.Scan(warnings=(), picture_ids=())


def test_an_unclosed_fence_runs_to_the_end() -> None:
    assert _kinds("text\n```\n![[a.png]]\n") == []


def test_room_warning_names_the_room_and_the_fix() -> None:
    warning = note_warnings.not_in_room(_ID, "Trip planning")
    assert warning.reference == f"/artifacts/{_ID}/raw"
    assert "isn't saved in Trip planning" in warning.message
    assert "--room Trip planning" in warning.message
