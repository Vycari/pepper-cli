"""``pepper blog check`` / ``pepper blog publish``: turn the server's check into lines of text.

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


def check_lines(check: dict[str, Any]) -> list[str]:
    """What publishing would do, one line per entry; indented lines belong to the one above."""
    title = sanitize_line(str(check.get("title") or "(untitled)"))
    kind = "Republish" if check.get("republish") else "First publish"
    lines = [f"{kind}: {title}", f"Address: {sanitize_line(str(check.get('slug') or ''))}"]
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


def _block(text: object) -> list[str]:
    """A multi-line server text, each line indented, control characters stripped."""
    return [f"  {line}" for line in sanitize_text(str(text or "")).splitlines()]


def published_lines(result: dict[str, Any]) -> list[str]:
    """The result of a publish: the address, and whether it was a first publish."""
    verb = "Republished" if result.get("republished") else "Published"
    lines = [f"{verb}."]
    if result.get("url"):
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
