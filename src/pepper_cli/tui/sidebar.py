"""The Messages tree: Pepper first, rooms by recency, topics nested under the open room."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual.binding import Binding, BindingType
from textual.message import Message
from textual.widgets import Tree

from pepper_cli.terminal_text import sanitize_line
from pepper_cli.tui.rows import safe_color
from pepper_cli.tui.state import SidebarRow

if TYPE_CHECKING:
    from textual.widgets.tree import TreeNode

DOT = "●"
#: In place of the dot on a row whose unread activity mentions you (§15 C).
MENTION = "@"


def row_label(row: SidebarRow, *, current: bool) -> Text:
    """One line of the tree. Server text is only ever a ``Text`` segment, never markup."""
    label = Text()
    if row.kind == "room":
        label.append("■ ", style=safe_color(row.color) or "")
    name = sanitize_line(row.label)
    if row.kind == "app":
        label.append("▌ ", style="bold")  # the open room app: its row exists only while open
    if row.kind in ("more", "archived", "newtopic", "inactive"):
        label.append(name, style="dim italic")
    else:
        label.append(name, style="bold reverse" if current else "bold" if row.unread else "")
    if row.mention:
        label.append(f" {MENTION}", style="bold")
    elif row.unread:
        label.append(f" {DOT}", style="bold")
    return label


class Sidebar(Tree[SidebarRow]):
    """A flat-row-driven tree: the app owns the rows, this only draws them."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("right", "expand_room", "Expand", show=False),
        Binding("left", "collapse_room", "Collapse", show=False),
    ]

    class RoomToggled(Message):
        """The user asked to expand or collapse a room by hand."""

        def __init__(self, room_id: str, *, expanded: bool) -> None:
            super().__init__()
            self.room_id = room_id
            self.expanded = expanded

    def __init__(self) -> None:
        super().__init__("Messages", id="sidebar")
        self.show_root = False
        self.auto_expand = False
        self.guide_depth = 2
        self._signature: tuple[object, ...] = ()
        self._by_key: dict[str, TreeNode[SidebarRow]] = {}
        #: A row the app asked for by key; a redraw still waiting to restore the old cursor
        #: yields to it.
        self._wanted: str | None = None

    @property
    def highlighted_row(self) -> SidebarRow | None:
        node = self.cursor_node
        return node.data if node is not None else None

    def set_rows(self, rows: list[SidebarRow], *, current_key: str) -> None:
        """Redraw only when something visible changed; keep the cursor on the same row."""
        signature = (tuple(rows), current_key)
        if signature == self._signature:
            return
        self._signature = signature
        keep = self.highlighted_row.key if self.highlighted_row else None
        self.clear()
        self.root.expand()
        self._by_key = {}
        parent: TreeNode[SidebarRow] = self.root
        for row in rows:
            label = row_label(row, current=row.key == current_key)
            if row.kind == "room":
                node = self.root.add(label, data=row, expand=row.expanded)
                parent = node
            elif row.kind in ("pepper", "inactive"):
                node = self.root.add_leaf(label, data=row)
            else:
                node = parent.add_leaf(label, data=row)
            self._by_key[row.key] = node
        self.call_after_refresh(self._restore_cursor, keep)

    def _restore_cursor(self, key: str | None) -> None:
        key = self._wanted or key
        node = self._by_key.get(key) if key else None
        if node is None:
            node = next(iter(self._by_key.values()), None)
        if node is not None:
            self.move_cursor(node)

    def select_key(self, key: str) -> None:
        """Move the cursor to a row by key (used by next-unread)."""
        self._wanted = key
        self.call_after_refresh(self._forget_wanted)
        if (node := self._by_key.get(key)) is not None:
            self.move_cursor(node)

    def _forget_wanted(self) -> None:
        self._wanted = None

    def action_expand_room(self) -> None:
        row = self.highlighted_row
        if row is not None and row.kind == "room" and row.room_id and not row.expanded:
            self.post_message(self.RoomToggled(row.room_id, expanded=True))

    def action_collapse_room(self) -> None:
        row = self.highlighted_row
        if row is None or row.room_id is None:
            return
        if row.kind == "room":
            if row.expanded:
                self.post_message(self.RoomToggled(row.room_id, expanded=False))
        elif (node := self._by_key.get(f"room:{row.room_id}")) is not None:
            self.move_cursor(node)
