"""Terminal window-title management for the interactive REPL and the TUI.

One small context manager rather than escape codes scattered through
:mod:`pepper_cli.cli`: :func:`window_title` pushes the terminal's current
title onto xterm's title stack (``CSI 22;0 t``), sets a new one (OSC 0, which
most terminals treat as both the window and tab title), and pops the stack
back (``CSI 23;0 t``) on the way out — clean exit, ``/quit``, Ctrl-D, Ctrl-C,
or an uncaught exception. Using the stack means this code never has to read
back "what was the title before" itself; the terminal remembers it.

Restoring is best-effort. A terminal that doesn't implement the (private,
xterm-specific) push/pop sequences just ignores them, and if it still honours
OSC 0 the Pepper title stays after exit until something else (usually the
shell prompt) sets a new one. That's cosmetic, never a hard failure, so this is
safe to wrap around the REPL unconditionally rather than trying to detect
terminal capability first. For the same reason a failed title write (a closed
or broken stdout) is swallowed rather than raised: the title must never
disrupt the REPL or mask the exception it is unwinding with.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Generator

#: The base window/tab title. The pepper emoji is the closest a terminal program can
#: get to an app icon: a terminal tab shows text, and no standard escape sets an image.
APP_TITLE = "\N{HOT PEPPER}\N{VARIATION SELECTOR-16} Pepper"

#: xterm "push current title onto the stack" (``Ps = 22``, ``Pt = 0`` for
#: both the icon and window title).
_PUSH_TITLE = "\x1b[22;0t"
#: xterm "pop the stack back onto the current title" counterpart.
_POP_TITLE = "\x1b[23;0t"


def _stdout_is_tty() -> bool:
    """Broken out of :func:`window_title` so tests can force either path
    without fighting pytest's own stdout capture (which isn't a TTY)."""
    return sys.stdout.isatty()


@contextlib.contextmanager
def window_title(title: str) -> Generator[None]:
    """Set the terminal's window title to ``title`` for the life of the
    ``with`` block, restoring whatever it was before on the way out.

    A no-op when stdout isn't a TTY: piped or redirected output must never
    carry escape sequences into the stream.
    """
    if not _stdout_is_tty():
        yield
        return
    try:
        _write(_PUSH_TITLE + f"\x1b]0;{title}\x07")
        yield
    finally:
        _write(_POP_TITLE)


def _write(sequence: str) -> None:
    """Write and flush one escape sequence, swallowing the errors a closed or
    broken stdout raises (``OSError`` for a broken pipe or EIO, ``ValueError``
    for a closed file). The title is cosmetic: failing to set it must not stop
    the REPL, and failing to restore it must not replace the exception the
    ``with`` block is already propagating."""
    with contextlib.suppress(OSError, ValueError):
        sys.stdout.write(sequence)
        sys.stdout.flush()
