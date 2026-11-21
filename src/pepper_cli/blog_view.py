"""``pepper blog stage`` / ``pepper blog publish``: turn the server's stage and check into lines.

The CLI only fetches and renders. Whether a note can be published, what needs the owner's
review, and what the address will be are the server's answers; this module prints the fields it
sent, in the order it sent them, and holds no rule of its own. Every server string goes through
:mod:`pepper_cli.terminal_text` before it reaches the terminal.
"""

from __future__ import annotations

from typing import Any

from pepper_cli.terminal_text import sanitize_line, sanitize_text


def _strings(value: object) -> list[str]:
    return [sanitize_line(str(item)) for item in value] if isinstance(value, list) else []


def check_lines(check: dict[str, Any], *, with_address: bool = True) -> list[str]:
    """What publishing would do, one line per entry; indented lines belong to the one above.

    ``with_address=False`` leaves out the address and summary, for when the saved stage's own
    (:func:`stage_lines`) follow: the stage's are the ones a publish would use.
    """
    title = sanitize_line(str(check.get("title") or "(untitled)"))
    kind = "Republish" if check.get("republish") else "First publish"
    lines = [f"{kind}: {title}"]
    if with_address:
        lines.append(f"Address: {sanitize_line(str(check.get('slug') or ''))}")
        summary = sanitize_line(str(check.get("summary") or ""))
        limit = check.get("max_summary")
        suffix = f" (up to {limit} characters)" if isinstance(limit, int) else ""
        lines.append(f"Summary{suffix}: {summary or '(none; readers get an excerpt)'}")

    refusal = check.get("refusal")
    if refusal:
        lines.append(f"Cannot be published: {sanitize_line(str(refusal))}")

    flattened = _strings(check.get("flattened_links"))
    linked = _strings(check.get("linked_posts"))
    others = _strings(check.get("other_images"))
    copied = check.get("copied_images")
    if flattened:
        lines.append("Links turned into plain words (their notes are not published posts):")
        lines.extend(f"  {word}" for word in flattened)
    if linked:
        lines.append("Links that now point at published posts:")
        lines.extend(f"  {post}" for post in linked)
    if others:
        lines.append("Images left as they are (not a room image the blog can copy):")
        lines.extend(f"  {url}" for url in others)
    if isinstance(copied, int) and copied:
        lines.append(f"Images copied into the blog: {copied}")
    for warning in _strings(check.get("warnings")):
        lines.append(f"Note: {warning}")

    review = check.get("review")
    if isinstance(review, dict):
        if review.get("human_version_found") is False:
            lines.append(
                "No version saved by a person was found to compare with, so the whole text "
                "is unreviewed:"
            )
            lines.extend(_block(review.get("body")))
        elif review.get("diff"):
            lines.append("Changed since the last version a person saved:")
            lines.extend(_block(review.get("diff")))
    if check.get("findings"):
        lines.append("The check found things above to review before this goes public.")
    return lines


def stage_lines(stage: dict[str, Any]) -> list[str]:
    """The address a publish would use, the saved summary and the schedule, as the server sent them.

    ``address`` is the server's whole address (the date the post will carry and the saved name);
    a server too old to send it leaves the saved name alone.
    """
    raw = stage.get("check")
    check: dict[str, Any] = raw if isinstance(raw, dict) else {}
    address = (
        sanitize_line(str(stage.get("address") or stage.get("slug") or check.get("slug") or ""))
        or "\u2014"
    )
    saved = stage.get("summary")
    summary = sanitize_line(str(saved if saved is not None else check.get("summary") or ""))
    excerpt = "(none; readers get an excerpt)"
    lines = [f"Address: {address}", f"Summary: {summary or excerpt}"]
    when = stage.get("publish_at_display")
    if when:
        lines.append(f"Scheduled: {sanitize_line(str(when))}")
    else:
        lines.append("Not scheduled \u2014 publish with `pepper blog publish NOTE`")
    return lines


def _block(text: object) -> list[str]:
    """A multi-line server text, each line indented, control characters stripped."""
    return [f"  {line}" for line in sanitize_text(str(text or "")).splitlines()]


def posts_lines(payload: dict[str, Any]) -> list[str]:
    """The room's posts, one line each (title, address, date, tags), then the notes not yet posts.

    Whatever the server sent is printed: ``scheduled_for`` only when it is there.
    """
    posts = payload.get("posts")
    lines: list[str] = []
    for post in posts if isinstance(posts, list) else []:
        if not isinstance(post, dict):
            continue
        title = sanitize_line(str(post.get("title") or "(untitled)"))
        address = sanitize_line(str(post.get("address") or post.get("slug") or "")) or "\u2014"
        when = sanitize_line(str(post.get("published_at") or ""))
        tags = []
        if post.get("withdrawn"):
            tags.append("withdrawn")
        if post.get("edited_since"):
            tags.append("note edited since")
        if post.get("scheduled_for"):
            tags.append(f"scheduled for {sanitize_line(str(post['scheduled_for']))}")
        suffix = f"  [{', '.join(tags)}]" if tags else ""
        lines.append(f"{title}  {address}  {when}{suffix}")
        post_id = sanitize_line(str(post.get("id") or ""))
        if post_id:
            lines.append(f"  id {post_id}")
    unpublished = payload.get("unpublished")
    rows = [r for r in unpublished if isinstance(r, dict)] if isinstance(unpublished, list) else []
    if rows:
        lines.append("Not published yet:")
        for row in rows:
            title = sanitize_line(str(row.get("title") or "(untitled)"))
            lines.append(f"  {title}  {sanitize_line(str(row.get('note_id') or ''))}")
    return lines or ["No posts yet."]


def withdrawn_lines(result: dict[str, Any]) -> list[str]:
    """The result of a withdraw."""
    lines = ["Already withdrawn." if result.get("already") else "Withdrawn."]
    if result.get("card") == "unavailable" or result.get("copy") in (
        "unavailable",
        "links_unavailable",
    ):
        lines.append(
            "The post is down, but its comments card or public-room copy could not be brought "
            "in step just now. Withdraw again to try again."
        )
    return lines


def published_lines(result: dict[str, Any]) -> list[str]:
    """The result of a publish: the address, and whether it was a first publish."""
    verb = "Republished" if result.get("republished") else "Published"
    lines = [f"{verb}."]
    # The server names the full addresses (``public_url`` only while the blog is public);
    # ``url`` is the bare path, shown only by a server too old to send them.
    if result.get("public_url"):
        lines.append(f"Public: {sanitize_line(str(result['public_url']))}")
    if result.get("post_url"):
        lines.append(f"Members: {sanitize_line(str(result['post_url']))}")
    elif result.get("url"):
        lines.append(sanitize_line(str(result["url"])))
    # The card and the public copy are written after the post is saved; when one could not be,
    # the post still stands and the next republish tries again (the server's own words).
    if result.get("card") == "unavailable":
        lines.append("The post is published, but its comments card could not be posted just now.")
    if result.get("copy") in ("unavailable", "links_unavailable"):
        lines.append(
            "The post is published, but its copy in the public room is not fully up to date."
        )
    if result.get("card") == "unavailable" or result.get("copy") in (
        "unavailable",
        "links_unavailable",
    ):
        lines.append("Republish to try again.")
    return lines
