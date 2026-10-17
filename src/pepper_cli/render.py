"""Rendering helpers for the CLI.

Rich handles markdown → terminal output. Two consoles: ``stdout`` for
the reply itself (so ``pepper chat ... | less`` and ``pepper chat ... >
file`` work), ``stderr`` for status / error chrome (so piping the
reply doesn't capture the "Logged in as ..." messages).

``NO_COLOR`` and non-tty downgrades are handled by Rich's
auto-detection — passing nothing extra here means a CI log file gets
plain ASCII without us doing anything.

The ``render_thought`` / ``render_tool_call`` / ``render_tool_result``
helpers are used by the REPL to surface the model's interim work as
each SSE frame arrives. They print to stderr so a hypothetical
future ``pepper chat --stream MESSAGE | jq`` (out of scope today but
plausible) wouldn't capture them in the piped output.

Server text is untrusted here, for two reasons. Terminal control: an escape
sequence in a room name or an error detail could clear the screen, retitle
the window or plant a hyperlink, so every field goes through
:mod:`pepper_cli.terminal_text` — one-line fields through ``sanitize_line``
(via :func:`_s`), free text through ``sanitize_text`` — and both consoles
carry a render hook that strips the same sequences from everything they
print, as a backstop for any field a renderer forgets. Rich markup: a
``[red]``- or ``[link=…]``-shaped name must print literally, so server text
interpolated into a markup string is ``escape``'d (again :func:`_s`), and
free text is printed on its own with ``markup=False``. ``info`` and
``error`` never parse their message as markup; a caller that wants emphasis
passes a :func:`styled` ``Text`` instead.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, tzinfo
from typing import TYPE_CHECKING, Any

from rich.console import Console, ConsoleOptions, ConsoleRenderable, RenderHook, RenderResult
from rich.markdown import Markdown
from rich.markup import escape
from rich.segment import Segment
from rich.text import Text

from pepper_cli.terminal_text import sanitize_line, sanitize_text

if TYPE_CHECKING:
    from pepper_cli.api_client import (
        ArtifactListResponse,
        GroupList,
        GroupMessage,
        GroupMessagePage,
        GroupRosterMember,
        GroupTopicSummary,
        ScheduledTaskDetail,
        ScheduledTaskMessage,
        ScheduledTaskRunEntry,
        ScheduledTaskSummary,
        TodoEntry,
        TrajectoryDetail,
        TrajectoryListEntry,
    )


class _Sanitized:
    """Wrap a renderable so the text it renders carries no terminal controls.

    Works on the rendered segments, after markup has been parsed and before
    rich adds its own styling, so rich's colours survive and only control
    and escape characters inside the text itself are removed. Rich's own
    control segments (cursor movement it emits on purpose) pass through.
    """

    def __init__(self, renderable: ConsoleRenderable) -> None:
        self._renderable = renderable

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        """Render the wrapped object, cleaning each text segment."""
        for segment in console.render(self._renderable, options):
            if segment.control or not segment.text:
                yield segment
                continue
            clean = sanitize_text(segment.text)
            yield segment if clean == segment.text else Segment(clean, segment.style)


class _StripTerminalControls(RenderHook):
    """Backstop: every renderable printed on our consoles goes through :class:`_Sanitized`."""

    def process_renderables(self, renderables: list[ConsoleRenderable]) -> list[ConsoleRenderable]:
        """Wrap each renderable about to be printed."""
        return [_Sanitized(r) for r in renderables]


def _make_console(**kwargs: Any) -> Console:
    """A rich console with the terminal-control backstop installed."""
    console = Console(**kwargs)
    console.push_render_hook(_StripTerminalControls())
    return console


# Lazy-ish: build the consoles once per process. ``no_color`` is left
# to Rich's auto-detection so ``NO_COLOR=1`` and piped output both
# produce plain text without explicit config here.
_stdout = _make_console()
_stderr = _make_console(stderr=True)


def _s(value: object) -> str:
    """A one-line server field, ready to interpolate into a markup string.

    Control characters (line breaks included) become spaces and rich
    markup is escaped, so the field can neither break its line nor style
    or link anything.
    """
    return escape(sanitize_line(str(value)))


def styled(*parts: str | tuple[str, str]) -> Text:
    """Literal text with styled spans, for an :func:`info` line that wants emphasis.

    Each part is a plain string or a ``(text, style)`` pair, as for
    ``Text.assemble``. Every part's text is sanitized and none of it is
    parsed as markup, so server text can go in any part.
    """
    return Text.assemble(
        *(sanitize_text(p) if isinstance(p, str) else (sanitize_text(p[0]), p[1]) for p in parts)
    )


#: Cap on how much of a tool_result ``text`` we render inline. The
#: server's ``_sanitize_step_value`` already truncates to 2KB, but the
#: REPL transcript looks gnarly if we slap 2KB into the terminal for
#: every tool result. Keep the inline preview short; the user can pull
#: the full payload from the trajectory viewer if they need it.
_TOOL_RESULT_PREVIEW_CHARS = 200


def render_markdown(text: str) -> None:
    """Print markdown text to stdout: rendered when stdout is a TTY, raw and
    byte-exact otherwise.

    Shared by :func:`render_reply` and ``pepper notes show``. The non-tty
    branch writes ``text`` with nothing added or trimmed — ``notes show``'s
    job is to put a note's stored body on stdout byte-for-byte, so
    ``show > file.md`` followed by a later ``edit --file file.md`` round-trips
    cleanly instead of a no-op edit silently changing the stored body (and
    its etag) by one trailing newline.

    Only the TTY branch is cleaned of terminal controls: that is the one
    that reaches a terminal. Piped or redirected output belongs to the
    reader on the other end, the same as ``--json``, and stays byte-exact.
    """
    if not sys.stdout.isatty():
        sys.stdout.write(text)
        return
    _stdout.print(Markdown(sanitize_text(text)))


def render_reply(markdown_text: str) -> None:
    """Print a chat reply to stdout, rendered as markdown.

    Non-tty stdout (piped, redirected) skips the markdown
    formatting and prints raw text — chat replies usually contain
    plain text the user wants to grep / pipe, not bold and headings.
    Ensures the output ends with a newline: a reply is a line of output,
    unlike a note body, which :func:`render_markdown` prints exactly as
    stored.
    """
    render_markdown(markdown_text)
    if not sys.stdout.isatty() and not markdown_text.endswith("\n"):
        sys.stdout.write("\n")


def write_document(text: str) -> None:
    """Write a stored document (a skill's ``SKILL.md``) to stdout unrendered.

    The same split as :func:`render_markdown`: byte-exact when stdout is
    piped or redirected, so ``show > file`` round-trips into an edit, and
    cleaned of terminal controls when it is a terminal.
    """
    sys.stdout.write(text if not sys.stdout.isatty() else sanitize_text(text))


def info(message: str | Text, *, soft_wrap: bool = False) -> None:
    """Print a status line to stderr. Used for the chrome around
    commands ('Logged in as ...', 'Removed credentials at ...') so the
    primary output channel stays clean for piping.

    A ``str`` message is printed literally: it is never parsed as rich
    markup, because callers interpolate server text (an error detail, a
    name) into it. Emphasis comes from passing a :func:`styled` ``Text``.
    ``soft_wrap=True`` is for a line a person copies back as a command
    (a next-page hint): rich must not fold it into two lines.
    """
    if isinstance(message, Text):
        _stderr.print(message, soft_wrap=soft_wrap)
    else:
        _stderr.print(sanitize_text(message), markup=False, soft_wrap=soft_wrap)


def error(message: str, *, soft_wrap: bool = False) -> None:
    """Print an error line to stderr in red. The CLI returns nonzero
    *and* prints this; the two signals together let scripts both
    detect failure (exit code) and surface the cause (message).

    ``soft_wrap=True`` is for a message that carries an id/uuid a person
    would want to copy-paste (an ambiguous-name match listing, say) —
    Rich would otherwise fold it across lines at the terminal width,
    which breaks a paste of exactly the thing being offered.

    ``message`` is printed literally, never parsed as rich markup: most
    callers interpolate a server ``detail`` or name into it.
    """
    _stderr.print(
        Text("error:", style="red"), sanitize_text(message), markup=False, soft_wrap=soft_wrap
    )


def render_thought_delta(text: str, *, first: bool) -> None:
    """Append one ``model_thought`` fragment to the thought being rendered.

    A fragment is whatever the backend chunks at — a paragraph on Gemini, a
    token on OpenAI and Anthropic — so a line per fragment gives a column of
    single words on the token-level backends. Fragments are written
    without a newline instead, and :func:`end_thought` closes the line when the
    terminal ``model_thought_block`` says the thought ended. Nothing is
    buffered, so the text still appears at the backend's own pace.

    ``first`` marks the fragment that opens a thought and gets the ``·``
    bullet. Dimmed because thoughts are background context — the user's eye
    should land on the eventual reply, not the running commentary.

    ``markup=False`` matters here: a fragment is model text, and a stray
    ``[dim]``-shaped token in it must not be read as rich markup.
    """
    text = sanitize_text(text)
    if not text:
        return
    if first:
        _stderr.print("· ", style="dim", end="", markup=False, highlight=False, soft_wrap=True)
    _stderr.print(text, style="dim", end="", markup=False, highlight=False, soft_wrap=True)


def end_thought(text: str, *, streamed: str) -> None:
    """Close the thought a ``model_thought_block`` frame terminates.

    ``streamed`` is everything the fragments put on screen for this thought,
    which is usually exactly ``text`` — then this only ends the line, because
    the thought is already rendered and reprinting it would double it.

    The two cases where it is not:

    - **Nothing streamed.** A backend that only emits whole thoughts, or a
      dropped fragment. Print the block as its own line.
    - **What streamed is not this thought.** A transient provider failure
      mid-stream abandons its attempt *without* a terminating block (see
      the server's generation loop); the server now sends
      a ``retry_attempt`` frame for that (:func:`discard_thought`).
      Otherwise the block is authoritative — the same rule the web composer
      applies by rewriting the row — so print it on its own line rather than
      leaving stale text as the record. A terminal cannot un-write what it
      already showed; what it can do is end with the correct paragraph.
    """
    if not streamed:
        if text and text.strip():
            render_thought_delta(text.strip(), first=True)
            _stderr.print("")
        return
    _stderr.print("")
    if text and text.strip() != streamed.strip():
        render_thought_delta(text.strip(), first=True)
        _stderr.print("")


def discard_thought() -> None:
    """End the line a ``retry_attempt`` frame abandons.

    A terminal cannot un-write the abandoned fragments, so end their line and
    let the retry's fragments start a fresh ``·`` thought instead of fusing
    onto them.
    """
    _stderr.print("")


def render_tool_call(payload: dict[str, Any]) -> None:
    """Render one ``tool_call`` SSE frame.

    Compact one-liner: ``→ name(arg=value, ...)``. Args dict is
    formatted with ``repr``-style truncation so a giant arg blob
    (e.g. a pasted email body) doesn't blow up the terminal."""
    name = _s(payload.get("name") or "<unknown>")
    args = payload.get("args") or {}
    if isinstance(args, dict) and args:
        rendered_args = ", ".join(f"{_s(k)}={_s(_short(v))}" for k, v in args.items())
        _stderr.print(f"[cyan]→[/cyan] [bold]{name}[/bold]({rendered_args})")
    else:
        _stderr.print(f"[cyan]→[/cyan] [bold]{name}[/bold]()")


def render_tool_result(payload: dict[str, Any]) -> None:
    """Render one ``tool_result`` SSE frame.

    Status-coloured glyph + name + truncated result text. ``ok`` gets
    a green check; non-ok gets a yellow warning so a glance at the
    transcript surfaces the failures."""
    name = _s(payload.get("name") or "<unknown>")
    status = payload.get("status") or "ok"
    text = payload.get("text") or ""
    glyph = "[green]✓[/green]" if status == "ok" else "[yellow]⚠[/yellow]"
    if text:
        _stderr.print(f"{glyph} [bold]{name}[/bold] → {_s(_short(text))}")
    else:
        _stderr.print(f"{glyph} [bold]{name}[/bold] ({_s(status)})")


#: Per-event payload preview budget for ``pepper trajectories show``.
#: Part of the rendered shape below, so changing it changes what
#: parsers of that output see.
_TRAJECTORY_EVENT_PREVIEW_CHARS = 300


def render_trajectory(detail: TrajectoryDetail) -> None:
    """Pretty-print a trajectory + its events to stdout.

    The output shape is kept stable, because agents and scripts
    parse it:

        trajectory <id> — user <user_id> — channel <channel>
        conversation <key>                (when the turn carries one)
        created <ts>  closed <ts|"(open)">
        code <12-char-sha>[ (dirty)]   (when meta carries it)
        N events:
        [seq] event_type: <payload truncated>
        ...

    Stdout, not stderr — so ``pepper trajectories show <id> > out.txt``
    captures the rendered view. The ``--json`` flag bypasses this
    function entirely.
    """
    traj = detail.trajectory
    closed = traj.closed_at or "(open)"
    _stdout.print(
        f"trajectory [bold]{_s(traj.id)}[/bold] — user {_s(traj.user_id)} "
        f"— channel {_s(traj.channel)}"
    )
    # Its own line rather than appended to the header: a dated 1:1 key is
    # ~55 chars, and the point of printing it is that two trajectories'
    # keys can be compared at a glance.
    if traj.conversation_key:
        _stdout.print(f"conversation {_s(traj.conversation_key)}")
    _stdout.print(f"created {_s(traj.created_at)}  closed {_s(closed)}")
    code_sha = traj.meta.get("code_sha") if isinstance(traj.meta, dict) else None
    if isinstance(code_sha, str) and code_sha:
        dirty = " (dirty)" if traj.meta.get("code_dirty") else ""
        _stdout.print(f"code {_s(code_sha[:12])}{dirty}")
    _stdout.print(f"{len(detail.events)} events:")
    for ev in detail.events:
        payload_str = _short_json(ev.payload, _TRAJECTORY_EVENT_PREVIEW_CHARS)
        _stdout.print(f"[{ev.seq}] {_s(ev.event_type)}: {_s(payload_str)}")


def render_trajectory_list(rows: list[TrajectoryListEntry]) -> None:
    """Pretty-print a trajectory-list response to stdout.

    Newest-first. Each row: id, channel, created_at, event count,
    optional group tag, and a snippet of the first user message.
    Plain text — no Rich tables — so the output stays grep-able.
    """
    if not rows:
        _stdout.print("(no trajectories)")
        return
    for row in rows:
        tag = f" {_s(f'[group:{row.group_name}]')}" if row.group_name else ""
        preview = ""
        if row.first_user_message:
            snippet = sanitize_line(row.first_user_message).strip()
            if len(snippet) > 80:
                snippet = snippet[:80] + "…"
            preview = f" — {escape(snippet)}"
        _stdout.print(
            f"{_s(row.created_at)}  [bold]{_s(row.id)}[/bold]  "
            f"{_s(row.channel)}{tag}  ({row.event_count} events){preview}"
        )


#: Status → leading glyph for the todo list. Open is an empty box; the
#: two closed states get distinct marks so a ``--status all`` listing
#: reads at a glance.
_TODO_MARKERS = {"open": "☐", "done": "✓", "dismissed": "✗"}


def render_todo_list(rows: list[TodoEntry]) -> None:
    """Pretty-print a todo list to stdout.

    One line per todo: status glyph, title, optional localized due date
    and priority, then a dim id (the handle for ``todos done <id>``).
    Plain text — no Rich tables — so the output stays grep-able, matching
    ``render_trajectory_list``. Due dates are localized to the machine's
    zone (the CLI runs on the user's own machine).
    """
    if not rows:
        _stdout.print("(no todos)")
        return
    for row in rows:
        marker = _TODO_MARKERS.get(row.status, "•")
        line = f"{marker} {_s(row.title)}"
        if row.due_date:
            line += f"  due {_s(_format_due(row.due_date, row.due_is_date_only))}"
        if row.priority:
            line += f"  ({_s(row.priority)})"
        line += f"  [dim]{_s(row.id)}[/dim]"
        _stdout.print(line)


_IMPUTED = {"followed": "followed", "email": "suggested - from email"}


def _calendar_event_line(ev: dict[str, Any]) -> str:
    """One agenda line from an event's server-preformatted fields."""
    if ev.get("all_day"):
        when = "all day"
    else:
        when = f"{ev.get('start_label') or ''}-{ev.get('end_label') or ''}".strip("-")
    title = _s(ev.get("summary") or "(no title)")
    line = f"{'>' if ev.get('is_next') else ' '} {_s(when):<16} {title}"
    if ev.get("location"):
        line += f"  @ {_s(ev['location'])}"
    conf = ev.get("conference")
    if isinstance(conf, dict) and conf.get("url"):
        line += f"  {_s('[' + (conf.get('name') or 'call') + ']')}"
    if ev.get("my_response") in ("declined", "tentative"):
        line += f"  ({ev['my_response']})"
    if ev.get("kind") in _IMPUTED:
        line += f"  [dim]({_IMPUTED[ev['kind']]})[/dim]"
    return f"[bold]{line}[/bold]" if ev.get("is_next") else line


def _sort_key(all_day: bool, iso: str | None) -> tuple[bool, datetime]:
    return (not all_day, datetime.fromisoformat(iso) if iso else datetime.min.replace(tzinfo=UTC))


def _day_rows(day: dict[str, Any], mode: str) -> list[tuple[tuple[bool, datetime], str]]:
    """A day's lines for one view, as (sort key, markup) pairs.

    ``calendar`` is what is on the user's calendars; ``schedule`` adds timed
    todo deadlines and planned meals; ``planning`` adds what Pepper imputes
    (followed events, email suggestions, meal windows), marked as such.
    """
    rows = []
    for ev in day.get("events") or []:
        if ev.get("kind") in _IMPUTED and mode != "planning":
            continue
        rows.append((_sort_key(bool(ev.get("all_day")), ev.get("start")), _calendar_event_line(ev)))
    if mode == "schedule":
        for todo in day.get("deadlines") or []:
            due = _s(f"due {todo.get('time') or ''}")
            line = f"  {due:<16} {_s(todo.get('title') or '')}"
            rows.append((_sort_key(False, todo.get("sort_key")), line))
        for meal in day.get("meals") or []:
            if meal.get("name"):
                slot = _s(str(meal.get("slot") or "meal"))
                rows.append((_sort_key(False, None), f"  {slot:<16} {_s(meal['name'])}"))
    if mode == "planning":
        for win in day.get("meal_windows") or []:
            when = f"{win.get('start_label') or ''}-{win.get('end_label') or ''}"
            dish = f" - {_s(win['dish'])}" if win.get("dish") else ""
            name = _s(win.get("label") or "Meal")
            line = f"  {_s(when):<16} {name} [dim](meal window){dish}[/dim]"
            rows.append((_sort_key(False, win.get("start")), line))
    for leg in day.get("travel") or []:
        facts = leg.get("facts") or {}
        if facts.get("ok"):
            slack = facts.get("spare_minutes")
            tight = f", {-slack} min late" if isinstance(slack, int) and slack < 0 else ""
            drive = _s(f"drive {facts['minutes']} min")
            line = (
                f"  [dim]{drive:<16} to {_s(leg.get('to') or '')}, "
                f"arrive {_s(facts.get('arrive_at') or '')}{tight}[/dim]"
            )
            rows.append((_sort_key(False, leg.get("after")), line))
    return sorted(rows, key=lambda r: r[0])


def _proposed_rows(plan: dict[str, Any]) -> list[tuple[tuple[bool, datetime], str]]:
    """Pepper's proposed todo blocks from the active schedule plan.

    Blocks already held on the calendar are real events in the range payload, so
    only the unheld ones are added here.
    """
    chosen = next(
        (p for p in plan.get("plans") or [] if plan.get("active") in p["philosophies"]), None
    )
    rows = []
    for b in (chosen or {}).get("blocks") or []:
        if b.get("type") != "todo" or b.get("done") or b.get("on_calendar"):
            continue
        when = _s(f"{b['start_label']}-{b['end_label']}")
        why = f" - {_s(b['reason'])}" if b.get("reason") else ""
        line = f"  {when:<16} {_s(b['title'])}  [dim](proposed todo block{why})[/dim]"
        rows.append((_sort_key(False, b["start"]), line))
    return rows


def _conditions_line(env: dict[str, Any], date: str) -> str | None:
    """One dim line of weather and daylight for ``date`` from the environment payload."""
    info = (env.get("days") or {}).get(date) or {}
    wx, solar = info.get("weather") or {}, info.get("solar") or {}
    parts = []
    if wx:
        parts.append(f"{wx.get('label')}, {wx.get('low')}-{wx.get('high')}F")
        if wx.get("precip"):
            parts.append(f"{wx['precip']['kind']} {wx['precip']['inches']}in")
        if wx.get("humidity"):
            parts.append(f"humidity {wx['humidity']['low']}-{wx['humidity']['high']}%")
        if wx.get("severe"):
            parts.append("severe weather")
    parts += [solar[k] for k in ("sunrise_label", "sunset_label") if solar.get(k)]
    return f"  [dim]{_s(' | '.join(parts))}[/dim]" if parts else None


def render_calendar(
    payload: dict[str, Any], *, mode: str = "calendar", cards: list[dict[str, Any]] | None = None
) -> None:
    """Print a calendar range payload as a per-day agenda.

    Uses the server's preformatted labels. Every ``window`` day prints,
    quiet ones included; ``days`` entries outside it don't. Multi-day
    ``spanning`` items come first. ``mode`` picks the layer (see
    ``_day_rows``); Planning's detector ``cards`` print after the days.
    Plain text, grep-able, like the other list renderers.
    """
    # ``window`` names every requested date; ``days`` only those with activity. A connected
    # calendar prints the whole window, so a quiet day still gets its heading.
    window_rows = payload.get("window") or []
    window = {w["date"] for w in window_rows}
    by_date = {d.get("date"): d for d in payload.get("days") or []}
    connected = payload.get("state") in (None, "connected")
    days = [{**w, **by_date.get(w["date"], {})} for w in window_rows if connected] or [
        d for d in by_date.values() if not window or d.get("date") in window
    ]
    # Followed and email suggestions can span days too: same layer rule as day events.
    spanning = [
        ev
        for ev in payload.get("spanning") or []
        if ev.get("kind") not in _IMPUTED or mode == "planning"
    ]

    def out(text: str = "") -> None:
        # Piped output is read by agents and scripts: never hard-wrap it at 80 columns.
        _stdout.print(text, soft_wrap=not _stdout.is_terminal)

    if spanning:
        out("[bold]Multi-day[/bold]")
        for ev in spanning:
            tag = " (ongoing)" if ev.get("ongoing") else ""
            if ev.get("kind") in _IMPUTED:
                tag += f"  [dim]({_IMPUTED[ev['kind']]})[/dim]"
            out(f"  {_s(ev.get('range_label') or '')}  {_s(ev.get('summary') or '')}{tag}")
        out()
    for day in days:
        label, date_label = day.get("label") or "", day.get("date_label") or ""
        extra = f" [dim]{_s(date_label)}[/dim]" if date_label != label else ""
        out(f"[bold]{_s(label)}[/bold]{extra}")
        env = payload.get("environment")
        if env and (conditions := _conditions_line(env, day.get("date") or "")):
            out(conditions)
        if env and day.get("date") == (window and min(window)):
            out_bands = [b.get("label") for b in env.get("precipitation") or []]
            if out_bands:
                out(f"  [dim]{_s('precipitation: ' + '; '.join(out_bands))}[/dim]")
        rows = _day_rows(day, mode)
        plan = payload.get("schedule_plan")
        if mode == "schedule" and plan and plan.get("date") == day.get("date"):
            rows = sorted(rows + _proposed_rows(plan), key=lambda r: r[0])
        for _, line in rows:
            out(line)
        if not rows:
            out("  [dim](nothing scheduled)[/dim]")
    plan = payload.get("schedule_plan")
    if mode == "schedule" and plan and plan.get("date") not in {d.get("date") for d in days}:
        # No calendar days (not connected) but todos still get a plan.
        proposed = _proposed_rows(plan)
        if proposed:
            out(f"[bold]{_s(plan['date'])}[/bold]")
            for _, line in sorted(proposed, key=lambda r: r[0]):
                out(line)
    if not days and payload.get("state") not in (None, "connected"):
        out("[dim](calendar not connected)[/dim]")
    if cards:
        out("\n[bold]Suggestions[/bold] [dim](not on your calendar)[/dim]")
        for card in cards:
            why = f" - {_s(card['rationale'])}" if card.get("rationale") else ""
            kind = _s(f"[{card.get('kind')}]")
            out(f"  {kind} {_s(card.get('title') or '')}{why}")
    if payload.get("truncated"):
        out("[dim](more events than shown; try fewer days)[/dim]")


#: Human labels for the destination names the server stores. Mirrors
#: ``pepper.agent.scheduled_tasks.destinations.LABELS`` — a name the CLI
#: doesn't know falls through verbatim rather than being dropped, so a
#: destination added server-side still renders honestly here.
_DESTINATION_LABELS = {
    "main_chat": "main chat",
    "resend_email": "email",
    "twilio_sms": "SMS",
    "whatsapp": "WhatsApp",
    "client_api": "app",
    "http": "task thread only",
}


def _describe_destinations(destinations: list[str] | None) -> str:
    """One-line summary of where a task announces its results.

    The three states are rendered as three different sentences on
    purpose: ``None`` (inherit) and ``[]`` (silent) look the same in a
    naive renderer and mean opposite things to the user — one follows
    their notification default, the other deliberately tells nobody.
    """
    if destinations is None:
        return "your default"
    if not destinations:
        return "nobody (silent)"
    return ", ".join(_s(_DESTINATION_LABELS.get(d, d)) for d in destinations)


def render_scheduled_task_list(rows: list[ScheduledTaskSummary]) -> None:
    """Pretty-print a scheduled-task listing to stdout.

    One line per task: state glyph, name, schedule, next fire (localized),
    delivery, then a dim id. Plain text — no Rich tables — so the output
    stays grep-able, matching the trajectory and todo listings.
    """
    if not rows:
        _stdout.print("(no scheduled tasks)")
        return
    for row in rows:
        glyph = "[green]●[/green]" if row.active else "[dim]○[/dim]"
        schedule = "one-off" if row.one_shot else f"cron {_s(row.cron)}"
        line = f"{glyph} {_s(row.name)}  {schedule}"
        # A paused row's next_run_at is whatever it held when it stopped —
        # printing it as "next" would be a lie, so say what it is instead.
        line += f"  {'next' if row.active else 'was due'} {_s(_format_stamp(row.next_run_at))}"
        # A room's row carries the server's own sentence: its ``None`` is the
        # room's default, which "your default" would misdescribe.
        # (``getattr``: this module imports the wire types for checking only.)
        delivery = getattr(row, "delivery", None)
        if delivery is not None:
            line += f"  → {_s(delivery)}"
            last_run_status = getattr(row, "last_run_status", None)
            if last_run_status:
                line += f"  last run {_s(last_run_status)}"
        else:
            line += f"  → {_describe_destinations(row.destinations)}"
        line += f"  [dim]{_s(row.id)}[/dim]"
        _stdout.print(line)


def render_scheduled_task(detail: ScheduledTaskDetail) -> None:
    """Pretty-print one task's configuration and its most recent fire."""
    state = "active" if detail.active else "paused"
    _stdout.print(f"[bold]{_s(detail.name)}[/bold] ({state})  [dim]{_s(detail.id)}[/dim]")
    if detail.description:
        # Free text, on a line of its own: kept multi-line, never parsed as markup.
        _stdout.print(sanitize_text(detail.description), markup=False)
    schedule = "one-off" if detail.one_shot else f"cron {_s(detail.cron)}"
    if detail.timezone:
        schedule += f" ({_s(detail.timezone)})"
    _stdout.print(f"schedule {schedule}")
    stamp = _s(_format_stamp(detail.next_run_at))
    _stdout.print(f"{'next' if detail.active else 'was due'} {stamp}")
    if detail.group_name:
        _stdout.print(f"room {_s(detail.group_name)}")
    if detail.group_delivery is not None:
        # A room task's ``None`` destinations is the room's default, which only the
        # server's own sentence describes ("your default" would be wrong).
        _stdout.print(f"delivered to {_s(detail.group_delivery)}")
    else:
        _stdout.print(f"delivered to {_describe_destinations(detail.destinations)}")
    if detail.latest_run is None:
        _stdout.print("last run (never fired)")
        return
    _stdout.print("last run:")
    render_scheduled_task_runs([detail.latest_run])


def render_scheduled_task_runs(rows: list[ScheduledTaskRunEntry]) -> None:
    """Pretty-print a task's fire history to stdout, newest first."""
    if not rows:
        _stdout.print("(no runs yet)")
        return
    for row in rows:
        line = f"{_s(_format_stamp(row.fired_at))}  {_s(row.status)}"
        if row.delivered_channels:
            line += f"  → {_describe_destinations(row.delivered_channels)}"
        if row.error:
            line += f"  [red]{_s(_short(row.error))}[/red]"
        elif row.reply_preview:
            line += f"  {_s(_short(row.reply_preview))}"
        if row.trajectory_id:
            line += f"  [dim]{_s(row.trajectory_id)}[/dim]"
        _stdout.print(line)


def render_scheduled_task_thread(rows: list[ScheduledTaskMessage]) -> None:
    """Pretty-print a task's own conversation thread, oldest first."""
    if not rows:
        _stdout.print("(no messages in this task's thread)")
        return
    for row in rows:
        _stdout.print(
            f"[dim]{_s(_format_stamp(row.created_at))}[/dim] [bold]{_s(row.role)}[/bold]:"
        )
        _stdout.print(sanitize_text(row.text), markup=False)


#: Server-recognized ``kind`` values (mirrors
#: ``pepper.features.groups.contract`` / ``CreateGroupRequest``'s docstring)
#: mapped to the label a person recognizes: a named, persistent room is a
#: "channel"; the wire value ``sealed`` is an email or text thread, which the
#: apps list under "Other conversations". The wire value is unchanged.
_KIND_LABELS = {"open": "channel", "sealed": "email/text thread"}


def render_group_list(gl: GroupList) -> None:
    """Pretty-print a rooms listing to stdout.

    One line per room: name, member count, the server-rendered relative
    activity label, an archived tag, then a dim id. Plain text — no Rich
    tables — matching every other listing in this CLI.

    ``kind`` and unread counts are deliberately absent: ``GET
    /api/v1/groups`` doesn't carry either (only ``GET
    /api/v1/groups/{id}`` — ``groups show`` — has ``kind``), and fetching
    it per room here would turn one list call into N+1. Left as a
    follow-up rather than guessed at.

    ``degraded`` means "could not ask", not "nothing new" — printed as a
    note so a client reading this list doesn't mistake a stale answer for
    a complete one.
    """
    if gl.degraded:
        _stdout.print("[yellow](groups service degraded — this list may be incomplete)[/yellow]")
    if not gl.groups:
        _stdout.print("(no rooms)")
        return
    for g in gl.groups:
        tag = " [dim](archived)[/dim]" if g.archived else ""
        # ``display_name`` is member-settable (a room rename) and interpolated
        # alongside our own ``[dim]``-style chrome, so it's escaped —
        # otherwise a room named e.g. "[bold red]Trip[/]" would inject
        # formatting, or a stray unmatched tag could raise. Everything else
        # on this line (count, activity label, id) is server-computed, not
        # free text.
        # soft_wrap: the id at the end of the line is the one thing here a
        # person is likely to copy-paste, and Rich's default wrapping would
        # otherwise fold it across two lines at the terminal width.
        _stdout.print(
            f"{_s(g.display_name)}{tag}  {g.member_count} members  "
            f"{_s(g.sub)}  [dim]{_s(g.group_id)}[/dim]",
            soft_wrap=True,
        )


def render_group_topics(rows: list[GroupTopicSummary]) -> None:
    """Pretty-print a room's topic list to stdout, busiest first (the order
    the server already answers them in)."""
    if not rows:
        _stdout.print("(no topics)")
        return
    for t in rows:
        tag = " [dim](archived)[/dim]" if t.archived_at is not None else ""
        # soft_wrap: same reasoning as render_group_list — don't fold the id.
        _stdout.print(
            f"{_s(t.name)}{tag}  active {_s(_format_stamp(t.last_activity_at))}  "
            f"[dim]{_s(t.topic_id)}[/dim]",
            soft_wrap=True,
        )


def render_group_roster(rows: list[GroupRosterMember]) -> None:
    """Pretty-print a room's ``@``-mention roster to stdout, one member per
    line with the person id a ``--mention`` takes."""
    if not rows:
        _stdout.print("(no members)")
        return
    for m in rows:
        tag = " [dim](Pepper)[/dim]" if m.is_pepper else " [dim](agent)[/dim]" if m.is_agent else ""
        # soft_wrap: the id is what a person copies — same as the lists above.
        _stdout.print(f"{_s(m.display_name)}{tag}  [dim]{_s(m.person_id)}[/dim]", soft_wrap=True)


def render_group_detail(detail: dict[str, Any]) -> None:
    """Pretty-print one room's info to stdout: kind, members + roles,
    topics, installed apps.

    ``detail`` is the raw ``GET /api/v1/groups/{id}`` JSON (a dict on
    purpose — see ``ApiClient.get_group``'s docstring): thirty-odd nested
    fields nothing else in this package reads would be a second
    description to drift from, so this reads it defensively with
    ``.get()`` rather than assuming every key is present.

    Every member-settable string (room name, member display names, topic
    names) is ``rich.markup.escape``'d before it's interpolated alongside
    this function's own ``[bold]``/``[dim]`` chrome — a display name or a
    room rename is exactly as attacker-controlled as a message body, and
    unescaped it could inject formatting or abort the render on a stray
    unmatched tag.
    """
    kind = detail.get("kind")
    kind_label = _s(_KIND_LABELS.get(kind, kind or "unknown"))
    _stdout.print(
        f"[bold]{_s(detail.get('display_name') or '(unnamed)')}[/bold]  ({kind_label})  "
        f"[dim]{_s(detail.get('group_id', ''))}[/dim]"
    )
    members = detail.get("members") or []
    _stdout.print(f"members ({len(members)}):")
    for m in members:
        you = " [dim](you)[/dim]" if m.get("is_you") else ""
        role = _s(m.get("role") or "member")
        agent = ""
        if m.get("is_agent"):
            sponsor = m.get("sponsor_display_name")
            run_by = f", run by {_s(sponsor)}" if sponsor else ""
            agent = f" [dim](agent{run_by})[/dim]"
        _stdout.print(f"  {_s(m.get('display_name') or 'Someone')}{you}{agent}  — {role}")
    topics = detail.get("topics") or []
    _stdout.print(f"topics ({len(topics)}):")
    for t in topics:
        tag = " [dim](archived)[/dim]" if t.get("archived") else ""
        _stdout.print(f"  {_s(t.get('name') or '')}{tag}")
    apps = detail.get("apps") or {}
    if apps.get("unavailable"):
        _stdout.print("apps: [dim](unavailable — couldn't ask)[/dim]")
    else:
        installed = apps.get("installed") or []
        if installed:
            _stdout.print("apps:")
            for a in installed:
                _stdout.print(f"  {_s(a.get('title') or a.get('app_key') or '')}")
        else:
            _stdout.print("apps: (none installed)")


def render_note_list(result: ArtifactListResponse) -> None:
    """Pretty-print a ``pepper notes list``/``search`` page to stdout.

    One line per note: name, labels (if any), ``updated`` timestamp
    (localized to the machine's own zone via :func:`_format_stamp` — same
    convention as the todo and scheduled-task listings), then a dim id.
    Plain text — no Rich tables — matching every other listing here.

    A truncated page says so: the server's ``truncated`` flag means there
    are more rows than fit in this page, and silently showing 50 of some
    larger total without a word would look like the whole answer.
    """
    if not result.items:
        _stdout.print("(no notes)")
        return
    for note in result.items:
        # The whole bracketed literal is escaped, not just each label: an
        # unescaped ``[recipe]`` is itself a Rich tag and silently vanishes,
        # and a label like ``[/dim]`` would close our own markup early.
        labels = "[" + ", ".join(note.labels) + "]"
        tag = f"  [dim]{_s(labels)}[/dim]" if note.labels else ""
        # soft_wrap: same reasoning as render_group_list — don't fold the id.
        _stdout.print(
            f"{_s(note.name)}{tag}  updated {_s(_format_stamp(note.updated_at))}  "
            f"[dim]{_s(note.id)}[/dim]",
            soft_wrap=True,
        )
    if result.truncated:
        _stdout.print("[dim](more notes than shown — narrow with --label or --room)[/dim]")


def group_message_tags(m: GroupMessage) -> tuple[str, str]:
    """The plain ``(via X)`` and ``(agent, for X)`` tags of a room message; ``""`` for each
    that does not apply. Shared by this module and the terminal UI so both say it the same
    way. The strings carry participant text: escape or sanitize before styling them."""
    via = f"(via {m.via_display_name})" if m.via_display_name else ""
    if not m.is_agent:
        return via, ""
    agent = f"(agent, for {m.for_display_name})" if m.for_display_name else "(agent)"
    return via, agent


def render_group_messages(page: GroupMessagePage) -> None:
    """Pretty-print one page of a topic's messages to stdout, oldest first.

    Timestamps use ``created_at_display`` verbatim — server-preformatted in
    the user's own timezone and date format (``GroupMessage``'s docstring:
    "show them; never re-derive them"), which is what makes this render the
    repo's "always render times in the user's local timezone" rule without
    this client owning any zone logic of its own.

    Every participant-controlled string (display name, "via" name,
    attachment filename) is ``rich.markup.escape``'d before it's
    interpolated alongside this function's own ``[dim]``/``[bold]`` chrome.
    The message body gets a stronger treatment: it's printed with
    ``markup=False`` on its own, unadorned call rather than escaped and
    merged into a chrome string, since it's the one field here with no
    formatting of ours around it to preserve.

    A day header (``day_label`` — the same server-preformatted string the
    web room fragment's own day divider prints) is inserted wherever
    ``day_key`` changes, so a transcript spanning more than one day says
    which day each run of messages is from instead of only a bare
    ``created_at_display`` clock time. A message with an empty ``day_key``
    or ``day_label`` — the server's broken-message placeholder, when its
    ``created_at`` couldn't be parsed — neither prints a header nor resets
    the current day, so it sits inside the surrounding day's run rather
    than opening a blank one (the same guard as the web room's
    ``group_room.js``).
    """
    if not page.messages:
        _stdout.print("(no messages)")
    last_day_key: str | None = None
    for m in page.messages:
        if m.day_key and m.day_label and m.day_key != last_day_key:
            if last_day_key is not None:
                _stdout.print()
            _stdout.print(f"[bold]{_s(m.day_label)}[/bold]")
            last_day_key = m.day_key
        display_name = _s(m.display_name)
        # "*agent* · for *requester*" — on a deleted
        # post too, as the web's badge is: who wrote it doesn't change.
        via_tag, agent_tag = group_message_tags(m)
        agent_tag = f" [dim]{_s(agent_tag)}[/dim]" if agent_tag else ""
        if m.is_deleted:
            _stdout.print(
                f"[dim]{_s(m.created_at_display)}[/dim] [bold]{display_name}[/bold]{agent_tag}: "
                "[dim](message deleted)[/dim]"
            )
            continue
        via = f" [dim]{_s(via_tag)}[/dim]" if via_tag else ""
        via += agent_tag
        edited = " [dim](edited)[/dim]" if m.is_edited else ""
        _stdout.print(
            f"[dim]{_s(m.created_at_display)}[/dim] [bold]{display_name}[/bold]{via}{edited}:"
        )
        if m.is_broken:
            _stdout.print("[dim](message could not be rendered)[/dim]")
        else:
            # Rich's markup parser would otherwise interpret any
            # ``[...]``-shaped text a participant typed into the message —
            # misrendering it at best, aborting the whole ``groups read``
            # on a malformed/unmatched tag at worst. The body is untrusted
            # in a way nothing else printed here is, so it gets its own
            # plain, unparsed print rather than being escaped into a
            # chrome string like the fields above.
            _stdout.print(sanitize_text(m.raw_body), markup=False, highlight=False)
        if m.attachments:
            names = ", ".join(
                _s(str(a.get("filename") or a.get("id") or "attachment")) for a in m.attachments
            )
            _stdout.print(f"  [dim]📎 {names}[/dim]")
        if m.reactions:
            chips = " ".join(_s(f"{r.get('emoji', '')}{r.get('count', '')}") for r in m.reactions)
            _stdout.print(f"  {chips}")
    if page.next_before:
        # soft_wrap: the cursor is meant to be copy-pasted into the next
        # --before, and Rich's default wrapping would otherwise fold it
        # across two lines at the terminal width.
        _stdout.print(f"[dim](more — pass --before {_s(page.next_before)})[/dim]", soft_wrap=True)


def _local_tz() -> tzinfo | None:
    """The zone stamps are rendered in; ``None`` means the machine's own.

    ``astimezone(None)`` is exactly the no-argument ``astimezone()``, so this
    changes nothing at runtime. It exists as the one place the local zone is
    learned, so tests can pin a ``zoneinfo.ZoneInfo`` here instead of setting
    the process ``TZ`` (``time.tzset`` does not exist on Windows).
    """
    return None


def local_clock(moment: datetime, *, hour24: bool = False) -> str:
    """``moment`` as a short clock time (``9:41 AM`` or ``09:41``) in the zone stamps render in.

    Spelled by hand, not with ``%-I``: that directive does not exist on Windows.
    """
    local = moment.astimezone(_local_tz())
    if hour24:
        return f"{local.hour:02d}:{local.minute:02d}"
    hour = local.hour % 12 or 12
    return f"{hour}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"


def _format_stamp(iso: str) -> str:
    """Render a UTC ISO-8601 timestamp in the machine's local zone, labelled.

    The CLI runs on the user's own machine, so its zone is the right one
    per the repo's "always render times in the user's local timezone"
    rule — the server deliberately hands out raw UTC and leaves this to
    the client.

    The zone label is not decoration. A cron task carries its own pinned
    timezone, which ``show`` prints beside the schedule, and the machine's
    zone is frequently not that one — an unlabelled ``next 14:00`` under a
    ``(America/New_York)`` pin reads as 14:00 New York and is off by the
    offset between them. Naming the zone the stamp is actually in is what
    makes the two lines consistent instead of contradictory, and it costs
    nothing on the common case where they agree.

    Falls back to the raw string when it can't be parsed: better a
    verbatim timestamp than a crashed listing.
    """
    import datetime as dt

    try:
        parsed = dt.datetime.fromisoformat(iso).astimezone(_local_tz())
    except ValueError:
        return iso
    rendered = parsed.strftime("%Y-%m-%d %H:%M")
    # %Z is empty on a platform that can't name the local zone; fall back
    # to the numeric offset so the stamp is never silently unanchored.
    label = parsed.strftime("%Z") or parsed.strftime("%z")
    return f"{rendered} {label}" if label else rendered


def _format_due(iso: str, date_only: bool | None = None, tz: tzinfo | None = None) -> str:
    """Render a UTC ISO-8601 due date for display.

    When ``date_only`` is ``True`` the server stored midnight UTC on a
    *named day*, not an instant — show that day's ``YYYY-MM-DD`` straight
    off the UTC value with no zone conversion, exactly as the web
    applet's ``fmtDue`` does, or a client east of UTC renders a clock
    that was never there and a client west of UTC shows the wrong day.
    Otherwise localize to ``tz``, or the machine's own zone when it is
    ``None``: ``YYYY-MM-DD`` for a deadline that happens to land on local
    midnight, ``YYYY-MM-DD HH:MM`` otherwise. Falls back to the raw string if it can't be parsed —
    better a verbatim timestamp than a crashed listing.
    """
    import datetime as dt

    try:
        parsed_utc = dt.datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if date_only:
        return parsed_utc.strftime("%Y-%m-%d")
    parsed = parsed_utc.astimezone(tz if tz is not None else _local_tz())
    if (parsed.hour, parsed.minute) == (0, 0):
        return parsed.strftime("%Y-%m-%d")
    return parsed.strftime("%Y-%m-%d %H:%M")


def _short_json(value: Any, budget: int) -> str:
    """JSON-serialize ``value`` and truncate to ``budget`` chars.

    The equivalent of jq's ``payload | tostring | .[0:budget]``.
    ``default=str`` rather than raising on
    a non-serializable value: a payload dict from the server is JSON
    by definition, but defensiveness here costs nothing and keeps
    the renderer from dying mid-event if a future field slips
    through as something exotic.
    """
    try:
        rendered = json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = str(value)
    if len(rendered) > budget:
        return rendered[:budget] + "…"
    return rendered


def _short(value: Any) -> str:
    """Format a value for inline display, truncating long strings.

    Strings get a ``…`` suffix past the budget; other types pass
    through ``repr`` (so ``{}`` / ``True`` / numbers all render
    sensibly) and then the same truncation rule applies."""
    s = value if isinstance(value, str) else repr(value)
    if len(s) > _TOOL_RESULT_PREVIEW_CHARS:
        return s[:_TOOL_RESULT_PREVIEW_CHARS] + "…"
    return s


def _countdown(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}m" if hours and mins else f"{hours}h" if hours else f"{mins}m"


def _event_when(row: dict[str, Any]) -> str:
    return f"{row.get('start_label') or ''}-{row.get('end_label') or ''}".strip("-")


def _started(row: dict[str, Any], now: datetime | None) -> bool:
    try:
        return now is not None and datetime.fromisoformat(row["start"]) <= now
    except (KeyError, TypeError, ValueError):
        return False


def _todo_due_label(item: dict[str, Any], now: datetime | None, zone: tzinfo | None) -> str:
    """The server's due field as text, tagged "overdue" once it is behind ``now``.

    A timed due is shown in ``zone``, the account's IANA zone, so it reads on the same clock as
    the event labels and keeps the right offset across a DST change. With no account zone
    (``zone`` is ``None``) it is shown in UTC, labelled, and never tagged: "today" is the
    account's, and the machine's zone is not a stand-in for it. Presentation only: the tag never
    moves or hides a todo."""
    raw = item.get("due")
    if not raw:
        return ""
    date_only = item.get("due_is_date_only")
    if zone is None:
        return f"due {_format_due(raw, date_only, UTC)}" + ("" if date_only else " UTC")
    text = f"due {_format_due(raw, date_only, zone)}"
    if now is not None:
        try:
            due = datetime.fromisoformat(raw)
            local = now.astimezone(zone)
            late = due.date() < local.date() if date_only else due <= local
        except (TypeError, ValueError):
            late = False
        if late:
            text += " (overdue)"
    return text


def _result_zone(name: Any) -> tzinfo | None:
    """The account's zone named in a ``pepper next`` result, or ``None`` when absent or unknown."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    if not isinstance(name, str) or not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def render_next(result: dict[str, Any]) -> None:
    """Print a ``pepper next`` result: Now, today's calendar, and the todos in server order."""

    def out(text: str = "") -> None:
        _stdout.print(text, soft_wrap=not _stdout.is_terminal)

    now, nxt = result.get("now"), result.get("next_event")
    try:
        clock: datetime | None = datetime.fromisoformat(result["generated_at"])
    except (KeyError, ValueError):
        clock = None
    zone = _result_zone(result.get("timezone"))
    out("[bold]Now[/bold]")
    if now:
        left = _countdown(now["minutes_left"])
        out(f"  {_s(now.get('title') or '')}  [dim]{_s(_event_when(now))}, {left} left[/dim]")
        if now.get("detail"):
            out(f"  [dim]@ {_s(now['detail'])}[/dim]")
    elif result.get("note"):
        # The calendar was not read: say so here, where "(no event right now)" would read as an
        # answer about a calendar nobody looked at.
        out(f"  [dim]{_s(result['note'])}[/dim]")
    else:
        out("  [dim](no event right now)[/dim]")
    if nxt:
        out(
            f"  [dim]next: {_s(nxt['title'])} at {_s(nxt['start_label'] or '')}, "
            f"in {_countdown(nxt['in_minutes'])}[/dim]"
        )
    elif now:
        out("  [dim]nothing else on the calendar today[/dim]")
    if result["calendar"]:
        out("\n[bold]Today's calendar[/bold]")
    for row in result["calendar"]:
        meta = f"  [dim]@ {_s(row['detail'])}[/dim]" if row.get("detail") else ""
        # Now shows one live event; another one running at the same time is marked here, so it
        # does not read as still ahead.
        if _started(row, clock):
            meta = f"  [dim](in progress)[/dim]{meta}"
        out(f"  {_s(_event_when(row)):<16} {_s(row.get('title') or '')}{meta}")
    todos = result["todos"]
    out(f"\n[bold]Todos[/bold] [dim]({todos['count']})[/dim]")
    for n, item in enumerate(todos["items"], 1):
        line = f"{n:>2}. {_s(item.get('title') or '(untitled)')}"
        labels = [_todo_due_label(item, clock, zone)]
        if item.get("priority"):
            labels.append(f"priority {item['priority']}")
        if item.get("source") and item["source"] != "user":
            labels.append(f"from {item['source']}")
        labels = [x for x in labels if x]
        if labels:
            line += f"  [dim]{_s(', '.join(labels))}[/dim]"
        out(line)
    if not todos["items"]:
        out("  [dim](nothing open)[/dim]")
    if todos["count"] > len(todos["items"]):
        out(f"  [dim]... {todos['count'] - len(todos['items'])} more (--all)[/dim]")


# ---------------------------------------------------------------------------
# Public helpers for the modules that draw their own views (``account_render``).


def out_line(text: str = "") -> None:
    """One line of rich-markup text to stdout, never folded (ids and links stay pasteable)."""
    _stdout.print(text, soft_wrap=True)


def out_text(text: str) -> None:
    """Server text to stdout as it is: no markup, no highlighting."""
    _stdout.print(sanitize_text(text), markup=False, highlight=False)


def local_stamp(iso: object) -> str:
    """A UTC ISO-8601 timestamp in this machine's zone, labelled (``_format_stamp``)."""
    return _format_stamp(str(iso)) if iso else ""


def one_line(value: object) -> str:
    """A server field made safe for one line of markup (``_s``)."""
    return _s(value)
