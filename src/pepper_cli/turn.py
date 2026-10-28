"""One streamed Pepper turn, dispatched by event kind: shared by the REPL and the TUI.

``POST /api/v1/chat/send`` streams SSE frames (:class:`~pepper_cli.api_client.StreamEvent`).
Which kinds exist, what each means, how thought fragments add up to one thought, and that an
unknown kind is skipped rather than fatal are decided once, here. A front end subclasses
:class:`TurnSink` and overrides only what it draws: the REPL prints to the terminal, the
full-screen UI updates a status line. Neither one re-implements the dispatch, so a kind the
server adds later is ignored the same way by both (``planning/terminal-ui.md`` §6.5).

Nothing here prints. Reporting belongs to the sink, because the TUI owns the alternate screen
and must never write to stdout or stderr.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import httpx

from pepper_cli.api_client import ApiError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from pepper_cli.api_client import ApiClient, StreamEvent
    from pepper_cli.provider.fs import FilesystemProvider

#: How a turn's stream finished: a terminal ``reply`` frame, a terminal ``error`` frame,
#: the stream closing without either, or the caller asking it to stop.
type TurnOutcome = Literal["reply", "error", "ended", "stopped"]

#: What a tool call for a tool this process never declared is answered with.
NO_LOCAL_TOOLS = "No local tools are shared with this session."


class TurnSink:
    """Receives a turn's events. Every method is a no-op here; override what you draw.

    The thought methods carry ``streamed``: the fragment text already handed to
    :meth:`thought_delta` for the thought still open, so a sink can tell whether what it
    has shown matches the assembled block, or what a retry abandoned.
    """

    def frame(self, event: StreamEvent) -> None:
        """Every frame, known kind or not, before its kind is dispatched."""

    def thought_delta(self, text: str, *, first: bool) -> None:
        """A non-empty fragment of the thought that is open (``first``: none came before)."""

    def thought_block(self, text: str, *, streamed: str) -> None:
        """The assembled thought; it closes the open one."""

    def retry(self, *, streamed: str) -> None:
        """The attempt was abandoned and is being re-run; ``streamed`` is what it left."""

    def tool_call(self, payload: dict[str, Any]) -> None:
        """A server-side tool started."""

    def tool_result(self, payload: dict[str, Any]) -> None:
        """A server-side tool returned."""

    def client_tools(self, payload: dict[str, Any]) -> None:
        """Which declared client tools the server accepted, and why it dropped the rest."""

    def client_tool_call(self, payload: dict[str, Any]) -> None:
        """The server asks this process to run a tool. Answer it before returning: the turn
        waits for the answer, and the next frame is read only after this returns."""

    def reply(self, payload: dict[str, Any]) -> None:
        """Terminal success (``payload`` mirrors ``ChatReply``)."""

    def error(self, payload: dict[str, Any]) -> None:
        """Terminal failure (``{"kind", "message"}``)."""


def dispatch(
    events: Iterable[StreamEvent],
    sink: TurnSink,
    *,
    should_stop: Callable[[], bool] | None = None,
) -> TurnOutcome:
    """Feed each frame to ``sink`` by kind until a terminal frame or the end of the stream.

    ``should_stop`` is asked before each frame is dispatched; when it says so the loop stops
    reading (closing the stream is the caller's ``with``/generator cleanup). Unknown kinds are
    skipped: an older client against a newer server sees novel frames without failing, which
    is also what makes the client-tool frames safe to add.
    """
    # Thoughts arrive as fragments of any size followed by one terminal
    # ``model_thought_block``. ``streamed`` is what the fragments have shown for the thought
    # still open; ``retry_attempt`` resets it, so it only holds the attempt a block closes.
    streamed = ""
    for event in events:
        if should_stop is not None and should_stop():
            return "stopped"
        sink.frame(event)
        kind = event.kind
        if kind == "model_thought":
            text = event.payload.get("text") or ""
            if text:
                sink.thought_delta(text, first=not streamed)
                streamed += text
        elif kind == "model_thought_block":
            sink.thought_block(event.payload.get("text") or "", streamed=streamed)
            streamed = ""
        elif kind == "retry_attempt":
            sink.retry(streamed=streamed)
            streamed = ""
        elif kind == "tool_call":
            sink.tool_call(event.payload)
        elif kind == "tool_result":
            sink.tool_result(event.payload)
        elif kind == "client_tools":
            sink.client_tools(event.payload)
        elif kind == "client_tool_call":
            sink.client_tool_call(event.payload)
        elif kind == "reply":
            sink.reply(event.payload)
            return "reply"
        elif kind == "error":
            sink.error(event.payload)
            return "error"
    return "ended"


def run_turn(
    api: ApiClient,
    message: str,
    sink: TurnSink,
    *,
    provider: FilesystemProvider | None = None,
    should_stop: Callable[[], bool] | None = None,
    subject: str | None = None,
    client_context: dict[str, Any] | None = None,
) -> TurnOutcome:
    """Send ``message`` as one streamed turn and dispatch its frames to ``sink``.

    ``subject`` and ``client_context`` name the app on screen (the TUI's own Todos or Notes
    pane); see :meth:`ApiClient.chat_send_stream`.

    ``provider`` is the ``--root`` filesystem toolset, if any: its manifest rides on the
    request. ``None`` sends no manifest, so the request body is what it was before client
    tools existed. Transport and HTTP errors propagate; nothing here ever resends the turn.
    """
    manifest = provider.manifest() if provider is not None else None
    # Only the TUI's own panes name an app; every other caller sends what it always did.
    on_screen: dict[str, Any] = {}
    if subject is not None:
        on_screen["subject"] = subject
    if client_context:
        on_screen["client_context"] = client_context
    stream = api.chat_send_stream(message, client_tools=manifest, **on_screen)
    try:
        return dispatch(stream, sink, should_stop=should_stop)
    finally:
        # Close the generator now (and the HTTP stream with it) rather than at garbage
        # collection, so a stopped or failed turn lets go of its connection at once.
        close = getattr(stream, "close", None)
        if close is not None:
            close()


@dataclass(frozen=True)
class ToolAnswer:
    """What :func:`answer_client_tool_call` did with one ``client_tool_call`` frame."""

    name: str
    arguments: dict[str, Any]
    text: str
    is_error: bool
    #: Why the answer could not be posted back, or ``None`` when it was.
    post_error: ApiError | httpx.RequestError | None = None


def answer_client_tool_call(
    api: ApiClient, payload: dict[str, Any], *, provider: FilesystemProvider | None
) -> ToolAnswer | None:
    """Run one client tool here and post its result; ``None`` for a frame with no call id.

    Answers even without a provider: the server is waiting on that ``call_id``, and saying
    "nothing is shared" at once is better than letting the call burn the turn's budget.

    A failure to *post* is returned, not raised. The result route's 404 means the call is
    already gone (timed out, or the turn moved on) and there is nothing to recover; raising
    would abort a stream whose reply is still coming. The caller decides how to report it.
    """
    call_id = str(payload.get("call_id") or "")
    name = str(payload.get("name") or "")
    if not call_id:
        return None
    raw_arguments = payload.get("arguments")
    arguments = raw_arguments if isinstance(raw_arguments, dict) else {}
    if provider is None:
        text, is_error = NO_LOCAL_TOOLS, True
    else:
        text, is_error = provider.call(name, arguments)
    post_error: ApiError | httpx.RequestError | None = None
    try:
        api.post_tool_result(
            call_id=call_id,
            content=[{"type": "text", "text": text}],
            is_error=is_error,
        )
    except (ApiError, httpx.RequestError) as exc:
        post_error = exc
    return ToolAnswer(
        name=name, arguments=arguments, text=text, is_error=is_error, post_error=post_error
    )
