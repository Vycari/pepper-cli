"""``pepper blog``: draw a room blog's posts, a note's check, and what publishing did.

The CLI only fetches and renders. What a check finds, whether a note may be published, and
the address a post gets are the server's; this module prints what came back.
"""

from __future__ import annotations

from typing import Any

from pepper_cli.api_client import BlogPublished, BlogWithdrawn
from pepper_cli.render import local_stamp as _stamp
from pepper_cli.render import one_line as _s
from pepper_cli.render import out_line as _line
from pepper_cli.render import out_text


def posts(body: dict[str, Any]) -> None:
    """The blog's posts, newest first, then the room's notes not yet published (owners only)."""
    rows = [p for p in body.get("posts") or [] if isinstance(p, dict)]
    _line("[bold]Posts[/bold]")
    if not rows:
        _line("  [dim](none)[/dim]")
    for post in rows:
        state = "  [dim]withdrawn[/dim]" if post.get("withdrawn") else ""
        edited = "  [dim]edited since[/dim]" if post.get("edited_since") else ""
        _line(f"  {_s(post.get('title'))}  [dim]{_s(_stamp(post.get('published_at')))}[/dim]")
        _line(f"    [dim]{_s(post.get('id'))}[/dim]{state}{edited}")
        if post.get("url"):
            out_text(f"    {post['url']}", soft_wrap=True)
    unpublished = [n for n in body.get("unpublished") or [] if isinstance(n, dict)]
    if unpublished:
        _line()
        _line("[bold]Not yet published[/bold]")
        for note in unpublished:
            _line(f"  {_s(note.get('title'))}  [dim]{_s(note.get('note_id'))}[/dim]")
    if not body.get("may_publish"):
        _line()
        _line("[dim]You can't publish here: only a room owner can, once blogs are on.[/dim]")


def _list(title: str, values: list[Any]) -> None:
    if values:
        _line(f"[bold]{title}[/bold]")
        for value in values:
            _line(f"  {_s(value)}")


def address(body: dict[str, Any], slug: str | None) -> str:
    """The address a publish will use: the check's, or with ``--slug``, the check's date in
    front of the name asked for. The server makes the address; this only previews it, and the
    server refuses a name a republish cannot take."""
    proposed = str(body.get("slug") or "")
    if not slug:
        return proposed
    day, sep, _name = proposed.rpartition("/")
    return f"{day}{sep}{slug}"


def check(body: dict[str, Any], *, address: str | None = None, summary: str | None = None) -> None:
    """What publishing would do: the address, anything to read first, and the review.

    ``address`` and ``summary`` are what ``publish`` was asked to send, shown so the owner
    confirms what goes out rather than the check's defaults."""
    _line(f"[bold]{_s(body.get('title'))}[/bold]")
    verb = "Republishes at" if body.get("republish") else "Publishes at"
    _line(f"{verb} [bold]{_s(address or body.get('slug'))}[/bold]")
    if summary is not None:
        _line(
            "Summary: " + (_s(summary) if summary else "[dim](none; readers get an excerpt)[/dim]")
        )
    if body.get("refusal"):
        _line(f"[red]Can't publish:[/red] {_s(body['refusal'])}")
    _list("Warnings", list(body.get("warnings") or []))
    _list("Links that become plain words", list(body.get("flattened_links") or []))
    _list("Links to other posts", list(body.get("linked_posts") or []))
    _list("Images left as they are", list(body.get("other_images") or []))
    if body.get("copied_images"):
        _line(f"Images copied into the post: {_s(body['copied_images'])}")
    raw = body.get("review")
    review: dict[str, Any] = raw if isinstance(raw, dict) else {}
    if review.get("diff"):
        _line()
        _line("[bold]Changes Pepper made since a person last wrote it[/bold]")
        out_text(str(review["diff"]))
    elif not review.get("human_version_found") and review.get("body"):
        _line()
        _line("[bold]Pepper wrote this text; read it before it goes out[/bold]")
        out_text(str(review["body"]))


def _card(card: str) -> str:
    return {
        "posted": "A card was posted in the room's comments.",
        "edited": "The post's card in the room was updated.",
        "none": "No card was posted in the room.",
    }.get(card, "")


def published(result: BlogPublished) -> None:
    verb = "Republished" if result.republished else "Published"
    _line(f"[bold]{verb}[/bold] {_s(result.slug)}")
    out_text(result.url, soft_wrap=True)
    if note := _card(result.card):
        _line(f"[dim]{note}[/dim]")


def withdrawn(result: BlogWithdrawn) -> None:
    if result.already:
        _line("That post was already withdrawn.")
    else:
        _line("Withdrawn. Publishing the note again puts it back at the same address.")
