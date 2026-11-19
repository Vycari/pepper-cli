"""``note_images``: rewriting a draft's local picture references to uploaded links."""

from __future__ import annotations

from pepper_cli import note_images

_U = note_images.Uploaded(filename="chart.svg", artifact_id="abc")


def _rw(body: str, *uploads: note_images.Uploaded) -> note_images.Rewrite:
    return note_images.rewrite(body, list(uploads or (_U,)))


def test_an_obsidian_embed_becomes_a_note_image() -> None:
    assert _rw("See ![[chart.svg]] here.").body == "See ![](/artifacts/abc/raw) here."


def test_an_embed_label_becomes_alt_but_a_size_is_dropped() -> None:
    assert _rw("![[chart.svg|Q3 revenue]]").body == "![Q3 revenue](/artifacts/abc/raw)"
    assert _rw("![[chart.svg|300]]").body == "![](/artifacts/abc/raw)"
    assert _rw("![[chart.svg|300x200]]").body == "![](/artifacts/abc/raw)"


def test_an_embed_matches_by_file_name_in_any_folder_and_case() -> None:
    assert _rw("![[attachments/Chart.SVG]]").replaced == 1


def test_a_markdown_image_keeps_its_alt_and_title() -> None:
    body = '![Revenue](./img/chart.svg "Q3")'
    assert _rw(body).body == '![Revenue](/artifacts/abc/raw "Q3")'


def test_an_angle_bracketed_target_with_spaces_and_an_encoded_one_match() -> None:
    u = note_images.Uploaded(filename="my chart.svg", artifact_id="abc")
    assert _rw("![](<img/my chart.svg>)", u).replaced == 1
    assert _rw("![](img/my%20chart.svg)", u).replaced == 1


def test_urls_site_paths_and_other_files_are_left_alone() -> None:
    body = (
        "![a](https://example.com/chart.svg) ![b](/artifacts/x/raw) "
        "![[other.png]] ![[Meeting notes]] [chart.svg](chart.svg)"
    )
    done = _rw(body)
    assert done.body == body
    assert done.replaced == 0
    assert done.unreferenced == (_U,)


def test_alt_text_from_an_embed_is_escaped() -> None:
    assert _rw("![[chart.svg|a [b c]]").body == "![a \\[b c](/artifacts/abc/raw)"


def test_every_reference_is_rewritten_and_none_left_unreferenced() -> None:
    done = _rw("![[chart.svg]]\n\n![x](chart.svg)")
    assert done.replaced == 2
    assert done.unreferenced == ()


def test_append_unreferenced_puts_each_on_its_own_line() -> None:
    assert note_images.append_unreferenced("Text\n", (_U,)) == "Text\n\n![](/artifacts/abc/raw)\n"
    assert note_images.append_unreferenced("", (_U,)) == "![](/artifacts/abc/raw)\n"
    assert note_images.append_unreferenced("Text", ()) == "Text"


def test_references_inside_code_are_examples_and_left_alone() -> None:
    uploads = [note_images.Uploaded("chart.svg", "abc")]
    body = "Write `![x](chart.svg)` like so.\n\n```md\n![[chart.svg]]\n```\n"
    done = note_images.rewrite(body, uploads)
    assert done.body == body
    assert done.replaced == 0
    assert done.unreferenced == tuple(uploads)


def test_escaped_alt_and_parenthesised_target_are_matched() -> None:
    uploads = [note_images.Uploaded("chart(1).svg", "abc")]
    done = note_images.rewrite("![a\\]b](img/chart(1).svg)", uploads)
    assert done.body == "![a\\]b](/artifacts/abc/raw)"


def test_a_stray_backtick_does_not_hide_a_later_paragraph() -> None:
    uploads = [note_images.Uploaded("chart.svg", "abc")]
    body = "Use a lone ` here.\n\n![[chart.svg|Revenue]]\n\nSee `x`.\n"
    done = note_images.rewrite(body, uploads)
    assert done.replaced == 1
    assert "/artifacts/abc/raw" in done.body
    assert "![[chart.svg" not in done.body
