# Terminal UI: a room's Todos and Notes, reached through the room

**Status:** Proposed. The maintainer has decided the item in §12. The lettered questions in §13 are open.
**Author:** drafted with Claude Code for the maintainer
**Last updated:** 2026-10-28
**Tracked by:** #157
**Related:** `planning/terminal-ui.md` (the TUI this extends; its §2 non-goal "A TUI tab for them
can be a later doc" is this doc); pepper `planning/apps-in-groups-web.md` (how the web opens a
room's copy of an app), pepper `planning/groups-phase-d.md` D.3 (app state owned by the group
principal). Server paths below are relative to `src/pepper/` in Vycari/pepper, read on
`origin/main` 2026-10-28.

## 1. Problem

`pepper tui` shows a room's conversation but nothing else the room holds. A room's shared todo
list and its notes are real server-side, and the one-shot commands already reach them (`pepper
todos list --room`, `pepper notes list --room`). Someone working in the TUI still has to leave it
and run those commands to see what the room has to do, or to read the packing list. The web
offers both from inside the room. The terminal should too.

## 2. Goals and non-goals

**Goals**

- Open a room's **Todos** and **Notes** from inside that room, the way the web does: from the
  room's launcher, or by typing `/todo` or `/notes` in the room's composer. Never as top-level tabs.
- Show what the web shows, in the web's order. For Todos: open items due-first, with closed ones
  folded under "Closed (N)". For Notes: the room's notes, most recently edited first, each one
  opening to its full text.
- Act on them in place, phased: tick, dismiss, reopen and add todos (P2); create, edit and delete
  notes, with `$EDITOR` and the same conflict handling `pepper notes edit` has (P3).
- No new server routes are required. Everything uses `/api/v1` routes `PepperClient` already calls,
  plus one existing route it doesn't wrap yet (checklist toggle, P3).

**Non-goals**

- The room's other apps (meals, shopping, recipes, calendar, …). The launcher lists them, and
  picking one opens it in the browser. Native panes for them can each be a later doc.
- The Pepper composer that floats over a room's app on the web, and the per-app in-app conversation
  that goes with it (pepper `apps-in-groups-web.md` §6–§7). This is a later doc.
- Installing or uninstalling apps. Room admin stays out of the TUI (`terminal-ui.md` §2).
- Notes' personal places (Voice, Journal, Add notes). They are the viewer's own and a room's copy
  doesn't have them (§3).
- Due-date phrase recognition ("tomorrow at 5pm") and the date/reminder pickers. P2 takes an
  explicit date (§13 G).
- Live push. The person stream carries no todo or note events (§6.4).

## 3. What the web does, mapped to the terminal

| Web behaviour | Source | Terminal |
|---|---|---|
| A room's apps open from **the room's composer launcher** (the waffle dock). Its tiles are the room's **installed** apps in install order, and only the installed ones. Unknown and retired keys drop out | `web/composer.py` `group_room_tiles`, `agent/apps/registry.py` `room_shelf`, `templates/applets/_drawer.html` | `a` opens a "Apps in <Room>" `PickerScreen` built from `GET /api/v1/groups/{gid}/apps/launcher` (`PepperClient.group_app_launcher`, same `room_shelf`). Same order, and only installed apps |
| Every app is also `/{slug}` in the composer's `/` menu: `/todo`, `/notes` | `web/composer.py` `app_slash`, `SlashCommand` | `/todo` and `/notes` typed alone in a room's composer open the pane. The TUI's slash handling today is just `/quit` (`tui/app.py`), so these two join it |
| A tile opens **the room's copy**, never the viewer's own (`?principal=<group id>`, `pepper-group-principal` meta) | `templates/group_room.html`, `static/applet.js` | Every read and write passes the room: `group_id=` for todos, `principal=` for notes |
| The app opens over the conversation. The room's topbar band (room name, members) stays above it, because the room's copy and yours look the same | `group_room.html` `data-shell-stage-top`, `_group_header.html` | The app pane replaces the thread pane. The header reads `Family › Todos` |
| While the room's app is open, a highlighted row naming it is nested **first** inside the room's expanded rail entry, before the topics. It disappears when the app closes. Clicking it returns to the app's front door | `static/shell.js` `paintRoomAppRow`, `roomAppState` | A `kind="app"` sidebar row, first among the open room's children, carrying the `▌` current marker. It's only present while the pane is open. `Enter` on it goes back to the list (from an open note, or after scrolling) |
| A room's app never lights the viewer's own Notes nav row or its sub-items | `_sidebar.html` (Notes block), `shell.js` `roomNotesOpen` | No personal row exists in the TUI to light. See §13 A for whether one should |
| While a room's app is open, **the groups composer bar is hidden**: the topic isn't visible, so it would be unclear where a post would land | pepper `planning/apps-in-groups-web.md` decision log 2026-09-15, phase 7 | The composer box is hidden while the pane is open, and the room's draft is kept. Closing the pane brings it back |
| **Todos page**: a title and an "N open" count, an add row, the open list sorted by due date (undated last), then created; closed items (done and dismissed) folded under "Closed (N)", newest first | `web/apps/defaults/todo.html` `sortItems`, `render` | Same sections, order and fold. The sort and fold are pure functions in `state.py`. The web's `config.sort = "created"` alternative isn't on `/api/v1`, so the TUI always sorts due-first |
| A todo row: tick (done), title, due chip, source link ("from <note>", an email, …), × (dismiss). Undo reopens | `todo.html` `row`, `sourceFor`; `static/notes_todos.js` | `space`/`x` done, `d` dismiss, `u` reopen (on a closed row). The due date and the source label are shown as dim text; `o` opens a source URL |
| A room's list is the room's `app:todo:data` document. The page offers the same controls on a room's list as on your own (maintainer decision 2026-09-15) | `todo.html` header comment; `features/todos/app.py` | Same. `GET /api/v1/groups/{gid}/todos` reads that same document (`api/groups.py` `room_todos` → `_room_todos_owner`) |
| The web todo page doesn't draw asks (`assignee`, `requested_by`) | `todo.html` (no ask rendering) | `TodoEntry` carries both. Showing them would be a departure (§13 D) |
| **Notes board**: the owner's `inline_text` artifacts, most recently **edited** first, up to 200, each card with a ~400-char preview and its checklist items | `agent/artifacts/notes/applet.py` `read_notes` (`order_by="updated_at"`, `LIST_LIMIT`, `PREVIEW_CHARS`) | `list_artifacts(principal=gid, kind="inline_text", limit=200)`. That route orders by `created_at` and carries no preview, only `summary` (§6.3), so the TUI re-sorts by `updated_at` and shows name, edited date and summary |
| A room's Notes has only the library and its notes. Voice, Journal and Add notes are the viewer's own | `static/notes_places.js` (`PERSONAL` gate) | List and viewer only |
| A note opens **rendered** (server-sanitized markdown). Edit is something you ask for, in a plain textarea holding exactly what was typed. A save sends `If-Match`, and a 412 is a conflict | `web/apps/defaults/notes.html` header comment | The viewer renders the body as markdown through the TUI's existing body renderer. `e` suspends the TUI into `$EDITOR` holding the raw body, and the save sends `If-Match` (§6.6) |
| Ticking a checklist box on the board rewrites that one line under a row lock | `notes.html`, `api/artifacts.py` `toggle_checklist` (`POST /api/v1/artifacts/{id}/toggle`, by line text, 409 when it no longer matches) | P3, `space` on a checklist line in the viewer (§13 H). Needs a new `PepperClient.toggle_checklist` |
| Delete is the Notes app's `delete` action (owner-scoped) | `notes.html` (`POST /apps/notes/actions/delete`) | `D` with a `ConfirmScreen`, via `PepperClient.delete_artifact(principal=gid)` |
| Imported notes the original owns are read-only ("Open original" in place of Edit) | `agent/artifacts/notes/applet.py` `READ_ONLY_SOURCES` | The edit refusal is shown verbatim. The list shows no extra marker in P3, because the list route doesn't say which notes are imported |

**Three surprises.**

1. **Todos and Notes are installed apps on a room, not built-in places.** The web offers them only
   in rooms that have them installed (`room_shelf`), and so does this doc (§13 C asks whether the
   TUI should go further). `pepper todos --room` works on any room, installed or not, so the
   gate is a UI rule, not an API one.
2. **The person stream has no todo or note frames.** `GET /api/v1/groups/events/stream` is the groups
   service's roster feed (`gateway/channels/groups_service/person_feed.py`, `agent_feed.py`:
   `message.*`, `member.*`, `read_state.advanced`), plus the `chat.message` frame for the 1:1 thread.
   Todos and artifacts live in pepper's own tables and emit nothing on it. The web's todo page
   doesn't live-update either: it re-reads on actions and on a short next-action watch.
3. **The notes list route and the board disagree on order.** `GET /api/v1/artifacts` defaults to
   `created_at` (`agent/artifacts/service.py` `list_artifacts`), while the board asks for
   `updated_at`. Up to 200 notes, a client re-sort by `updated_at` is exact. Past 200
   (`truncated`), the top 200 by creation isn't the top 200 by edit (§11, item 1).

## 4. Layout

The open room is Family. Its Todos app is open:

```
┌ Messages ──────────────────┐┌ Family › Todos ───────────────────────────────────────────┐
│ Pepper                     ││ Todos  3 open                                             │
│ Family                     ││                                                           │
│ ▌ Todos                    ││ ○ Book the dog sitter                          due Oct 30 │
│   General                  ││ ○ Pack the cooler                              due Nov 1  │
│   Trip                   ● ││ ○ Return the library books          from "Errands" note   │
│   Packing                  ││                                                           │
│   + New topic              ││ ▸ Closed (2)                                              │
│ Book club                  ││                                                           │
│ Work                     ● ││                                                           │
│                            ││                                                           │
│ 4 people · active 2m ago   ││                                                           │
└────────────────────────────┘└───────────────────────────────────────────────────────────┘
 space done  d dismiss  n add  a apps  Esc back to topic  ^Q quit
```

Notes, then one note open:

```
┌ Family › Notes ───────────────────────────────┐   ┌ Family › Notes › Packing list ───────────┐
│ Notes  12                                     │   │ Packing list            edited Yesterday │
│                                               │   │                                          │
│ Packing list                      Yesterday   │   │ Bring layers, it gets cold at night.     │
│   Bring layers, it gets cold at night.        │   │                                          │
│ Cabin wifi                           Oct 21   │   │ ☐ Sleeping bags                          │
│   Network "lakeside", password on the fridge. │   │ ☑ Headlamps                              │
│ Recipes to try                       Oct 14   │   │ ☐ Dog food                               │
│                                               │   │                                          │
└───────────────────────────────────────────────┘   └──────────────────────────────────────────┘
 Enter open  c new  e edit  D delete               Esc list  e edit  space tick (P3)  o link
```

- The pane takes the thread pane's place (`#right`'s `ThreadView`). The composer box is hidden,
  as on the web, while the pane is open.
- The sidebar's `▌ Todos` row is the web's room-app rail row. It disappears when the pane closes
  (`Esc`, opening a topic, or switching rooms).
- The second line of a notes-list row is the server's `summary`, clipped to one line. It's empty
  when the note has none. The web's preview is the opening of the body, which the list route
  doesn't send (§11, item 2).

## 5. Keybindings

New keys apply only where noted. `terminal-ui.md` §5's keys are unchanged. The thread's own `r`,
`e`, `d`, `o`, `+` bindings sit on `ThreadView`, which is hidden while the pane is open, so they
don't collide.

| Key | Where | Action | Web analogue |
|---|---|---|---|
| `a` | outside the composer, scoped as `i` is, with a room open or highlighted | Open the room's launcher: a picker of its installed apps. Todos and Notes open the pane, and anything else opens `/groups/{gid}?open=<slug>` in the browser | The waffle |
| `/todo`, `/notes` + `Enter` | room composer | Open that pane (only when installed; otherwise a notice) | `/{slug}` |
| `Esc` | pane | From an open note: back to the list. From a list: close the pane and go back to the topic | The stage's ✕ |
| `Enter` | sidebar app row | Back to the pane's list | Click the rail row |
| `↑`/`↓`, `PgUp`/`PgDn` | pane | Move the selection | — |
| `Enter` | todo row | Show its details (title, details, due date, source, asks) in an `InfoScreen` | — |
| `space` or `x` | open todo | Mark done (P2) | Tick |
| `d` | open todo | Dismiss (P2) | × |
| `u` | closed todo | Reopen (P2) | Undo |
| `n` | Todos | Add a todo: `PromptScreen` for the title, then an optional due date (P2) | Add row |
| `Enter` | "Closed (N)" | Fold or unfold | `<details>` summary |
| `Enter` | note row | Open the note | Card click |
| `c` | Notes | New note in `$EDITOR`: the first line is the title (P3) | New note |
| `e` | note row or open note | Edit in `$EDITOR` (P3) | Edit |
| `D` | note row or open note | Delete, with a confirmation (P3) | Delete |
| `space` | checklist line in an open note | Tick or untick (P3, §13 H) | Checkbox |
| `o` | todo or open note | Open the todo's source link, or the first link in the note | Click |
| `r` | pane | Refresh now | — |

`D` is uppercase so that `d` on a todo (dismiss, which `u` undoes) can't be misread as deleting a
note, which can't be undone from the TUI.

## 6. Data flow

Every call runs on the TUI's existing worker path: `App._start(kind, work, deliver)` →
`run_worker(thread=True)` → `_post` back to the UI thread, with a `_Loop` per kind for busy and
generation checks (`tui/app.py`). Fetchers go in `tui/poller.py` beside `fetch_room_info`. Pure
shaping (sort, fold, selection, row keys) goes in `tui/state.py`, so the coverage ratchet holds.

### 6.1 The room's shelf

`a`, `/todo` and `/notes` need to know whether the room has the app. `group_app_launcher(gid)`
returns `(tiles, installed_unavailable)`. It's read when first needed and cached per room for the
session, and the cache is dropped when room info (`i`) is re-read. If the read is
`installed_unavailable`, the TUI says "Apps unavailable right now" and doesn't treat the room as
having no apps. This mirrors the reason the server sends the flag (`features/groups/contract.py`,
#3147).

### 6.2 Todos (P1 read)

`list_todos(status="open", group_id=gid)` and `list_todos(status="done", …)` plus `"dismissed"`.
The server also accepts `status=all` (`api/groups.py` `room_todos`), but `PepperClient.list_todos`
documents only the three values. P1 widens its docstring and any `Literal` to include `all`, so
the pane makes one call instead of three. That's a client change only. `state.todo_sections(entries)`
returns `(open_sorted, closed_sorted)`: open by due date (undated last), then `created_at`, then
server order; closed by `done_at` (else `created_at`), newest first. Both match `todo.html`. A
`due_is_date_only` due date shows as a bare day with no zone conversion (`TodoEntry` docstring).

### 6.3 Notes (P1 read)

`list_artifacts(principal=gid, kind="inline_text", limit=200)`, re-sorted by `updated_at`
descending in `state.notes_order`. If `truncated`, a dim last row says "Showing 200 notes. `pepper
notes list --room` searches the rest", rather than implying the list is complete. Opening a note
calls `get_artifact_detail(id)` and keeps its `etag` for P3. A detail whose `kind` isn't
`inline_text` is refused, as `pepper notes` does.

### 6.4 Freshness

With no event frames (§3, surprise 2), the pane re-reads:

- when it opens, and when it's re-entered from the sidebar row;
- after every write it makes (the write's response is applied at once, then the list is re-read);
- on `r`;
- on a slow poll while it's open and the terminal is focused: 30s, paused while blurred or
  suspended, using the same `_blurred` signal as the topic poll. Nothing is polled while the pane is
  closed.

While the pane is open, the room's topic polls and the person stream keep running, so unread dots
still move. An optional server addition for push is §11, item 3.

### 6.5 Todo writes (P2)

`complete_todo`, `dismiss_todo`, `reopen_todo` and `create_todo`, each with `group_id=gid`. All
four are owner-scoped single-document mutations under the document's row lock on the server
(`todo.html` header comment), so there's no rev and no retry loop. The row shows a pending mark
until the response lands, and the returned `TodoEntry` replaces the row. A failure restores the row
and shows the server's message through the existing `_action_failed`. Add takes a title (max 500,
as the web field does) and an optional due date parsed by the same rule as `todos create --due`
(`cli.py` `_parse_due`: `YYYY-MM-DD` or `YYYY-MM-DD HH:MM`, local zone). `_parse_due` moves to a
shared module so the TUI doesn't import `cli.py`.

### 6.6 Note writes (P3)

- **Edit.** Read the detail fresh, then `with self.app.suspend():` run the editor on the raw body
  (`$VISUAL`/`$EDITOR`, `vi` fallback). On return, an unchanged body (up to one trailing newline,
  `_texts_match`) sends nothing. Otherwise `edit_artifact(id, body=…, principal=gid,
  if_match=etag)`.
- **412.** `ArtifactConflictError` carries the server's current body and etag. The TUI saves the
  user's text exactly as `pepper notes edit` does (`_save_conflict_copy`, `./<slug>.<etag>.mine.md`)
  and offers the same three choices in a `PickerScreen`: re-open the editor on their own text against
  the new etag, overwrite with the new etag, or quit (the saved copy is kept). The diff the CLI prints
  goes into an `InfoScreen`.
- **New.** `c` opens the editor on an empty template. The first non-blank line is the title and the
  rest is the body. `create_artifact(name, body, principal=gid)`. An empty result aborts.
- **Delete.** `ConfirmScreen("Delete <name>?")`, then `delete_artifact(id, principal=gid)`.
- **Checklist toggle** (§13 H). `POST /api/v1/artifacts/{id}/toggle?principal=gid` names the line by
  its text, and a 409 shows the server's sentence verbatim and re-reads.

The editor helper is `cli.py` `_open_in_editor`. It reports errors through `render.error` and names
its temp file `pepper-skill-`. P3 moves it to a shared module that returns an error string instead
of printing (the TUI shows it as a notice), and takes a temp-file prefix. The CLI keeps its
behaviour. Suspending needs a real terminal: under Pilot, or when `App.suspend` raises
`SuspendNotSupported`, the action shows a notice pointing at `pepper notes edit` instead.

## 7. Rendering

- All server text goes through `sanitize_line` (titles, names, due labels, source labels,
  `summary`) or `sanitize_text` (note bodies, todo details) into `Text`, never into Rich markup. This
  is the same invariant as `terminal-ui.md` §7. A note body renders through the same markdown path
  the thread uses for message bodies, so a note can't style itself beyond what a message can.
- Dates use the TUI's existing local-time helpers. The web formats in the Pepper timezone on the
  server; the TUI uses the machine's zone, as `terminal-ui.md` §7 already does for messages.
- Checklist lines (`- [ ]`, `- [x]`, outside code fences) render as `☐`/`☑`. The rule for what
  counts as a checklist line is the server's (`agent/artifacts/notes/checklist.py`), so a small
  `state.checklist_lines` mirrors it, with fixture tests copied from the server's own cases.
- Nothing shows a count except "N open" on Todos and the "Closed (N)" fold, which are the web's own.
  The sidebar app row never carries a dot.

## 8. Package layout

- `tui/apps.py` (new): `AppPane` (a `ContentSwitcher` of `TodoList`, `NoteList`, `NoteView`) and its
  bindings. Widget glue only.
- `tui/state.py`: `todo_sections`, `notes_order`, `checklist_lines`, `AppPaneState` (which room,
  which app, which note, selection, fold), and the sidebar's `kind="app"` row in `_room_children`.
- `tui/poller.py`: `fetch_room_shelf`, `fetch_room_todos`, `fetch_room_notes`, `fetch_note`.
- `tui/app.py`: the `a` binding, `/todo` and `/notes` in `_composer_key`/send, pane open and close,
  composer hide, and the 30s loop in `_LOOPS`.
- `api_client.py`: `list_todos` accepts `"all"` (P1); `toggle_checklist` (P3, `# MIRROR:
  src/pepper/api/artifacts.py::ArtifactToggleResponse`; already in the vendored OpenAPI snapshot).
- Shared helpers moved out of `cli.py`: `_parse_due` (P2), `_open_in_editor`, `_texts_match`,
  `_save_conflict_copy` (P3).
- `README.md` and the bundled `SKILL.md` get a line per phase.

## 9. Testing

- **`state.py`** unit tests carry the coverage: the due-first sort with undated, date-only and timed
  due dates and ties; the closed fold order; the `updated_at` re-sort; checklist parsing against the
  server's cases (fenced code, nested, `[X]`); the app row appearing first, only for the open room,
  and only while the pane is open.
- **Pollers** with pytest-httpx: the shelf (installed, none installed, `installed_unavailable`),
  room todos, room notes with `truncated`, a 403/404 on a room the caller left.
- **Pilot** (`App.run_test`, fake server, as in `tests/test_tui_parity.py`): `a` → picker → Todos
  opens and the header says `Room › Todos`; `/notes` in the composer opens Notes; the composer is
  hidden while the pane is open and the draft survives; `Esc` walks note → list → topic; an
  uninstalled room shows a notice. P2: done, dismiss, reopen and add round-trip, and a failed write
  rolls back. P3: edit with `suspend` patched to run a fake editor, and a 412 with each of the three
  choices; delete confirm and cancel.
- **Snapshots** for the two layouts in §4 once P1 settles, as `terminal-ui.md` §11 does.
- **Windows**: nothing new. Pilot is headless. `$EDITOR` suspend is checked by hand in Windows
  Terminal before P3 merges.

## 10. Ordering and failure posture

- A pane read that fails shows the error in the pane, with `r` to retry. It never closes the pane,
  and the last good list stays up, marked stale.
- A room the caller has left (403/404) closes the pane with a notice. The existing rail refresh
  removes the room.
- Switching rooms closes the pane. The pane belongs to one room, as a room's applet does on the web.
- A worker result for a room or note that's no longer open is dropped by the existing generation
  check.

## 11. Server changes

**None needed for P1–P3.** All routes exist: `GET /api/v1/groups/{gid}/apps/launcher`, `GET/POST
/api/v1/groups/{gid}/todos` and `…/{id}/done|dismiss|reopen`, `GET /api/v1/artifacts`, `GET
…/{id}/detail`, `POST …/new`, `POST …/{id}/edit` (If-Match/412), `POST …/{id}/toggle`, `POST
/api/v1/apps/notes/actions/delete`.

Optional, all additive under `/api/v1`. Each is filed as a pepper issue only if §13 says so:

1. `order_by=updated_at` on `GET /api/v1/artifacts`. Makes the list exact past 200 notes (§13 F).
2. A `preview` field on list rows, the board's ~400-char opening, so a list row can show the
   opening of the note as the board does.
3. `todo.changed` / `artifact.changed` frames on the person stream, carrying ids only, as the
   stream's other frames do. Lets the pane drop its poll (§13 E).

## 12. Decisions

| | Decision |
|---|---|
| 1 | **2026-10-28, Allen.** Model it as the web does. A room's notes and todos are reached **through the room** (its launcher, its `/` commands, its nested rail row), not as top-level tabs. Mirror the web's entry points, ordering and what it shows. Departures are listed in §3, each with its reason. |

## 13. Questions

**A. Should the Pepper row get the same treatment for the person's own Todos and Notes?**

On the web, your own Notes is a top-level Primary nav row (with Voice, Journal and Add notes
sub-items) that opens inside `/chat`. Your own Todos is reached only from `/chat`'s launcher
(`/todo`), and your Todos page also lists each of your rooms' open items in a section per room
(`todo.html`, `GET /apps/todo/rooms`; `/api/v1/todos/rooms` is the PAT twin). There is no personal
To-dos nav row (`_sidebar.html`).

- (a) No. Your own todos and notes stay on `pepper todos` / `pepper notes`.
- (b) The Pepper DM gets the same launcher: `a`, `/todo` and `/notes` with the Pepper row open
  open *your* copy (`list_todos()` with no group, `list_artifacts()` with no principal), with the
  app row nested under the Pepper row while open. Your Todos also lists your rooms' open items
  in sections, as the web does.
- (c) As in (b), plus a top-level "Notes" row to match the web's Primary nav.

**Recommendation: (b), in the same phases.** It reuses the pane unchanged, follows the "DM is
just another row" decision (`terminal-ui.md` §14.1), and avoids adding top-level rows, which this
doc's decision steers away from. Room sections on your own Todos come in P2 (they need
`/todos/rooms`, which `PepperClient` doesn't wrap yet).

**B. Is the web's entry point enough, or should Todos and Notes always show under the room?**

On the web the app row appears only *after* the app is opened from the launcher or `/`. A terminal
has no visible waffle.

- (a) Parity: `a`, `/todo` and `/notes`, with the row shown only while open. The footer shows
  `a apps` whenever a room is open, so it can be found.
- (b) Always show "Todos" and "Notes" rows under the open room's topics (when installed).

**Recommendation: (a).** The decision in §12 is parity. The footer hint covers discoverability.
(b) is cheap to add later if (a) proves hard to find.

**C. What about rooms that don't have Todos or Notes installed?**

The API serves a room's todos and notes either way. The web just doesn't offer them.

- (a) Parity: not offered. `a` lists only what's installed, and `/todo` in such a room says "Todos
  isn't installed in this room. Install it from Room info on the web."
- (b) Always offer Todos and Notes in every room.

**Recommendation: (a).** Installing is a room decision that members make on purpose (D.2), and
showing an app the room never installed would hand it a surface the web says it doesn't have.

**D. Should a todo row show asks (who it was asked of, who asked)?**

`TodoEntry.assignee` / `requested_by` are set on an ask, and `pepper todos list --mine` filters by
them. The web todo page draws neither.

- (a) Parity: not shown.
- (b) A dim second line, "asked of Sam by you", plus `m` to filter to asks of you (`assignee=me`).
  P2 adds asking a member (`create_todo(assignee_person_id=…)` via a roster picker).

**Recommendation: (b), display and filter in P1, asking in P2, as a stated departure.** An ask
with no visible asker reads as a plain todo, and the TUI user has no Dash to see it in. Asks are
data `/api/v1` already sends.

**E. Is a 30-second poll while open fresh enough, or file the event frames now?**

- (a) Poll (§6.4) and revisit after P1 is in daily use.
- (b) File §11 item 3 now.

**Recommendation: (a).** The web todo page doesn't push either, so a poll is already parity. This
is the same reasoning as `terminal-ui.md` §15 E.

**F. Note order past 200: re-sort on the client, or ask for `order_by`?**

- (a) Re-sort by `updated_at` on the client, with a "showing 200" row when `truncated`.
- (b) File §11 item 1 now and use it once the snapshot has it.

**Recommendation: (a) for P1, filing (b) only if a real room passes 200 notes.** It's exact below
the cap, and the cap is the board's own.

**G. How much due-date entry does P2's add need?**

- (a) An explicit date: a second prompt taking `YYYY-MM-DD` or `YYYY-MM-DD HH:MM` (the `todos create
  --due` rule), blank for none.
- (b) Port the web's phrase recognition ("tomorrow at 5pm") to Python.
- (c) Also `set_due` and `set_reminder` on existing rows. Those are app actions, not `/api/v1/todos`
  verbs, so this would go through `edit_todo` or the apps actions route.

**Recommendation: (a).** It matches the CLI's own rule, and (b) is a second copy of a parser that
already lives in one place. (c) can follow if it's asked for.

**H. Should checklist ticking in the note viewer be in scope?**

The board's one-click write. The route exists, but the client doesn't wrap it.

- (a) Yes, in P3, with `toggle_checklist` added to `PepperClient`.
- (b) No. Editing the note in `$EDITOR` covers it.

**Recommendation: (a).** It's the board's most frequent write, it can't conflict with a
concurrent edit to another line, and it's small.

## 14. Phases

One PR each. Each phase gets an issue when it's picked up.

| Phase | Contents | Depends on |
|---|---|---|
| P1 | **Read-only.** Room shelf; `a` launcher picker (non-native apps open in the browser); `/todo` and `/notes`; the app pane with Todos (sections, fold, details) and Notes (list, viewer, checklist render, `o`); the nested app row; composer hide/restore; 30s poll while open; `list_todos("all")`; §13 A/D in their decided form; tests from §9; README/SKILL.md lines | `terminal-ui.md` P3 (merged) |
| P2 | **Todo actions.** Done, dismiss, reopen, add with an optional due date; asks if §13 D is (b); room sections on your own Todos if §13 A is (b); `_parse_due` moved to a shared module | P1 |
| P3 | **Note writes.** New, edit through suspended `$EDITOR` with If-Match and the three-way 412 resolution, delete with confirm, checklist toggle (§13 H); editor and conflict helpers moved out of `cli.py`; a manual Windows Terminal check | P1 (P2 not required) |
