# Terminal UI: Todos and Notes, a room's and your own

**Status:** Decided. The maintainer answered every open question on 2026-10-28 (§12). Ready to implement, phase by phase.
**Author:** drafted with Claude Code for the maintainer
**Last updated:** 2026-10-28
**Tracked by:** #157
**Related:** `planning/terminal-ui.md` (the TUI this extends; its §2 non-goal "A TUI tab for them
can be a later doc" is this doc); pepper `planning/apps-in-groups-web.md` (how the web opens a
room's copy of an app), pepper `planning/groups-phase-d.md` D.3 (app state owned by the group
principal). Server paths below are relative to `src/pepper/` in Vycari/pepper, read on
`origin/main` 2026-10-28.

## 1. Problem

`pepper tui` shows conversations and nothing else: neither your own todo list and notes, nor a
room's shared ones. All of them are real server-side, and the one-shot commands already reach
them (`pepper todos list [--room]`, `pepper notes list [--room]`). Someone working in the TUI still
has to leave it and run those commands to see what's due, or to read the packing list. The web
offers both from its navigation and from inside each room. The terminal should too.

## 2. Goals and non-goals

**Goals**

- Open a room's **Todos** and **Notes** from inside that room, the way the web does: from the
  room's launcher, or by typing `/todo` or `/notes` in the room's composer. No room gets a
  top-level tab.
- Open **your own** Todos and Notes from two top-level sidebar rows above Pepper, in the place the
  web's Primary nav sits above Messages. The web has a Notes row but no To-dos row, so the Todos
  row is a departure (§12, decision 2). Your Todos also lists your rooms' open items, a section
  per room, as the web's does. The Pepper composer stays under your own panes, so you can ask
  Pepper about the list without leaving it (§12 decision 10).
- Show what the web shows, in the web's order. For Todos: open items due-first, with closed ones
  folded under "Closed (N)", and asks shown on the row (a departure, §12 decision 5). For Notes:
  the notes, most recently edited first, each one opening to its full text.
- Act on them in place, phased: tick, dismiss, reopen and add todos (P2); create, edit and delete
  notes, with `$EDITOR` and the same conflict handling `pepper notes edit` has (P3).
- No new server routes are required. Everything uses `/api/v1` routes `PepperClient` already calls,
  plus two existing routes it doesn't wrap yet: rooms' todos (P1) and checklist toggle (P3).

**Non-goals**

- The room's other apps (meals, shopping, recipes, calendar, …) and your own other apps. A room's
  launcher lists them, and picking one opens it in the browser. Native panes for them can each be a
  later doc.
- The Pepper composer that floats over a room's app on the web, and the per-app in-app conversation
  that goes with it (pepper `apps-in-groups-web.md` §6–§7). This is a later doc.
- Installing or uninstalling apps. Room admin stays out of the TUI (`terminal-ui.md` §2).
- Notes' personal places (Voice, Journal, Add notes). They're web capture surfaces, and the CLI
  already has `pepper notes` for adding.
- Due-date phrase recognition ("tomorrow at 5pm"), the date and reminder pickers, and set-due or
  reminder on existing rows. P2 takes an explicit date (§12, decision 7).
- Live push. The person stream carries no todo or note events (§6.6).

## 3. What the web does, mapped to the terminal

| Web behaviour | Source | Terminal |
|---|---|---|
| A room's apps open from **the room's composer launcher** (the waffle dock). Its tiles are the room's **installed** apps in install order, and only the installed ones. Unknown and retired keys drop out | `web/composer.py` `group_room_tiles`, `agent/apps/registry.py` `room_shelf`, `templates/applets/_drawer.html` | `a` opens a "Apps in <Room>" `PickerScreen` built from `GET /api/v1/groups/{gid}/apps/launcher` (`PepperClient.group_app_launcher`, same `room_shelf`). Same order, and only installed apps |
| Every app is also `/{slug}` in the composer's `/` menu: `/todo`, `/notes`, in a room and in `/chat` | `web/composer.py` `app_slash`, `SlashCommand` | `/todo` and `/notes` typed alone in a room's composer open the room's pane. In the Pepper DM composer they open your own. The TUI's slash handling today is just `/quit` (`tui/app.py`), so these two join it |
| A tile opens **the room's copy**, never the viewer's own (`?principal=<group id>`, `pepper-group-principal` meta) | `templates/group_room.html`, `static/applet.js` | Every room read and write passes the room: `group_id=` for todos, `principal=` for notes. Your own reads pass neither |
| The app opens over the conversation. The room's topbar band (room name, members) stays above it, because the room's copy and yours look the same | `group_room.html` `data-shell-stage-top`, `_group_header.html` | The app pane replaces the thread pane. The header reads `Family › Todos` for a room's copy and `Todos` for your own |
| While the room's app is open, a highlighted row naming it is nested **first** inside the room's expanded rail entry, before the topics. It disappears when the app closes. Clicking it returns to the app's front door | `static/shell.js` `paintRoomAppRow`, `roomAppState` | A `kind="app"` sidebar row, first among the open room's children, carrying the `▌` current marker. It's only present while the pane is open. `Enter` on it goes back to the list (from an open note, or after scrolling) |
| **Primary nav sits above Messages**: Dash, Calendar, **Notes** (opens your Notes inside `/chat`), Apps. Then the "Messages" section: Pepper, then rooms. There's no To-dos row; your Todos opens from `/chat`'s launcher or `/todo` | `templates/_sidebar.html` (`nav aria-label="Primary"`, then `"Messages"`) | Two top-level rows, **Todos** then **Notes**, above Pepper. Notes is parity. Todos is a departure (§12 decision 2). It goes first because the web has no slot for it, and none of Dash, Calendar or Apps is in the TUI to set an order between them. The sidebar cursor starts on the open conversation (Pepper at launch), not the first row, and `↑`/`↓` step over the rule under the two rows |
| A room's app never lights the viewer's own Notes nav row or its sub-items | `_sidebar.html` (Notes block), `shell.js` `roomNotesOpen` | Same. A room's pane marks only its nested row. Your own pane marks the top-level row |
| While a room's app is open, **the groups composer bar is hidden**: the topic isn't visible, so it would be unclear where a post would land | pepper `planning/apps-in-groups-web.md` decision log 2026-09-15, phase 7 | The composer box is hidden while a room's pane is open, and the room's draft is kept. Closing the pane brings it back. Your own panes are different (next row) |
| Over **your own** app in `/chat`, the Pepper composer stays. A message is an ordinary turn in your Pepper thread, sent with the open app as `subject: app:{slug}` (and, over Notes, the open note as `client_context.note_id`). The reply shows in the composer's pane over the app, and the app stays on screen | pepper `planning/pepper-over-apps.md` §2–§3.1; `api/router.py` `ChatSendRequest.subject`, `client_context` | Parity (§12 decision 10). The Pepper DM composer stays under your own pane and sends to the Pepper DM with the same `subject` and `client_context`. The exchange shows in a reply strip at the foot of the pane, and the pane stays (§6.9) |
| **Todos page**: a title and an "N open" count, an add row, the open list sorted by due date (undated last), then created; closed items (done and dismissed) folded under "Closed (N)", newest first | `web/apps/defaults/todo.html` `sortItems`, `render` | Same sections, order and fold. The sort and fold are pure functions in `state.py`. The web's `config.sort = "created"` alternative isn't on `/api/v1`, so the TUI always sorts due-first |
| **Your** Todos page also lists each of your rooms' open items, a section per room, and merges the rooms' closed items into its Closed fold | `todo.html` (`GET /apps/todo/rooms`); `api/router.py` `list_room_todos` (`GET /api/v1/todos/rooms`, the PAT twin, rooms by recency, empty rooms left out) | Same. A section per room under your own open items, headed by the room's name, in the route's order. Needs a new `PepperClient.list_room_todos` (P1) |
| A todo row: tick (done), title, due chip, source link ("from <note>", an email, …), × (dismiss). Undo reopens | `todo.html` `row`, `sourceFor`; `static/notes_todos.js` | `space`/`x` done, `d` dismiss, `u` reopen (on a closed row). The due date and the source label are shown as dim text; `o` opens a source URL. A todo whose source is a request shows no separate source label: the web labels it "asked by X [in Room]", which the ask line (§6.5) already carries |
| A room's list is the room's `app:todo:data` document. The page offers the same controls on a room's list as on your own (maintainer decision 2026-09-15) | `todo.html` header comment; `features/todos/app.py` | Same. `GET /api/v1/groups/{gid}/todos` reads that same document (`api/groups.py` `room_todos` → `_room_todos_owner`) |
| The web todo page doesn't draw asks (`assignee`, `requested_by`) | `todo.html` (no ask rendering) | A departure (§12 decision 5): a dim "asked of Sam by you" line under an ask, and `m` to show only the asks made of you |
| **Notes board**: the owner's `inline_text` artifacts, most recently **edited** first, up to 200, each card with a ~400-char preview and its checklist items | `agent/artifacts/notes/applet.py` `read_notes` (`order_by="updated_at"`, `LIST_LIMIT`, `PREVIEW_CHARS`) | `list_artifacts(kind="inline_text", limit=200)`, plus `principal=gid` for a room. That route orders by `created_at` and carries no preview, only `summary` (§6.4), so the TUI re-sorts by `updated_at` and shows name, edited date and summary |
| A room's Notes has only the library and its notes. Voice, Journal and Add notes are the viewer's own | `static/notes_places.js` (`PERSONAL` gate) | List and viewer only, for both copies (§2 non-goals) |
| A note opens **rendered** (server-sanitized markdown). Edit is something you ask for, in a plain textarea holding exactly what was typed. A save sends `If-Match`, and a 412 is a conflict | `web/apps/defaults/notes.html` header comment | The viewer renders the body as markdown through the TUI's existing body renderer. `e` suspends the TUI into `$EDITOR` holding the raw body, and the save sends `If-Match` (§6.8) |
| Ticking a checklist box on the board rewrites that one line under a row lock | `notes.html`, `api/artifacts.py` `toggle_checklist` (`POST /api/v1/artifacts/{id}/toggle`, by line text, 409 when it no longer matches) | P3, `space` on a checklist line in the viewer (§12 decision 8). Needs a new `PepperClient.toggle_checklist` |
| Delete is the Notes app's `delete` action (owner-scoped) | `notes.html` (`POST /apps/notes/actions/delete`) | `D` with a `ConfirmScreen`, via `PepperClient.delete_artifact` (with `principal=gid` for a room) |
| Imported notes the original owns are read-only ("Open original" in place of Edit) | `agent/artifacts/notes/applet.py` `READ_ONLY_SOURCES` | The edit refusal is shown verbatim. The list shows no extra marker in P3, because the list route doesn't say which notes are imported |

**Three surprises.**

1. **A room's Todos and Notes are installed apps, not built-in places.** The web offers them only
   in rooms that have them installed (`room_shelf`), and so does this doc (§12 decision 4).
   `pepper todos --room` works on any room, installed or not, so the gate is a UI rule, not an API
   one.
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
│ Todos                      ││ Todos  3 open                                             │
│ Notes                      ││                                                           │
│ ────────────────────────── ││ ○ Book the dog sitter                          due Oct 30 │
│ Pepper                     ││   asked of you by Sam                                     │
│ Family                     ││ ○ Pack the cooler                              due Nov 1  │
│ ▌ Todos                    ││ ○ Return the library books          from "Errands" note   │
│   General                  ││                                                           │
│   Trip                   ● ││ ▸ Closed (2)                                              │
│   Packing                  ││                                                           │
│   + New topic              ││                                                           │
│ Book club                  ││                                                           │
│ Work                     ● ││                                                           │
│ 4 people · active 2m ago   ││                                                           │
└────────────────────────────┘└───────────────────────────────────────────────────────────┘
 space done  d dismiss  n add  m asks of me  a apps  Esc back to topic  ^Q quit
```

Your own Todos, with your rooms' open items below your own. The Pepper composer stays, and the
last exchange with Pepper shows in a strip at the foot of the pane:

```
┌ Messages ──────────────────┐┌ Todos ────────────────────────────────────────────────────┐
│ ▌ Todos                    ││ Todos  3 open                                             │
│ Notes                      ││                                                           │
│ ────────────────────────── ││ ○ Renew passport                               due Nov 3  │
│ Pepper                     ││ ○ Call the dentist                                        │
│ Family                   ● ││ ○ Buy stamps                                              │
│ Book club                  ││                                                           │
│ Work                       ││ Family                                                    │
│                            ││ ○ Book the dog sitter                          due Oct 30 │
│                            ││   asked of you by Sam                                     │
│                            ││ ▸ Closed (5)                                              │
│                            ││ ───────────────────────────────────────────────────────── │
│                            ││ you    add buy stamps                                     │
│                            ││ Pepper Added "Buy stamps" to your todos.                  │
│                            │├───────────────────────────────────────────────────────────┤
│                            ││ Ask Pepper about your todos…                              │
└────────────────────────────┘└───────────────────────────────────────────────────────────┘
 space done  d dismiss  n add  m asks of me  Tab composer  Esc back  ^Q quit
```

Notes (a room's or your own looks the same, apart from the header), then one note open:

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

- The pane takes the thread pane's place (`#right`'s `ThreadView`). Under a room's pane the
  composer box is hidden. Under your own pane it stays, as the Pepper DM's composer, with the
  placeholder "Ask Pepper about your todos…" or "…your notes…". The reply strip appears only once
  you've sent something (§6.9).
- The top-level **Todos** and **Notes** rows are always present, even on a server with rooms
  turned off, because they don't depend on rooms. A thin rule separates them
  from Pepper, standing in for the web's "Messages" section label. They carry no unread dot and no
  count.
- The sidebar's nested `▌ Todos` row under Family is the web's room-app rail row. It disappears
  when the room's pane closes (`Esc`, opening a topic, or switching rooms).
- The second line of a notes-list row is the server's `summary`, clipped to one line. It's empty
  when the note has none. The web's preview is the opening of the body, which the list route
  doesn't send (§11, item 2).

## 5. Keybindings

New keys apply only where noted. `terminal-ui.md` §5's keys are unchanged. The thread's own `r`,
`e`, `d`, `o`, `+` bindings sit on `ThreadView`, which is hidden while the pane is open, so they
don't collide.

| Key | Where | Action | Web analogue |
|---|---|---|---|
| `Enter` | top-level Todos or Notes row | Open your own pane | Notes nav row (Todos: a departure) |
| `a` | outside the composer, scoped as `i` is, with a room open or highlighted | Open the room's launcher: a picker of its installed apps. Todos and Notes open the pane, and anything else opens `/groups/{gid}?open=<slug>` in the browser | The waffle |
| `/todo`, `/notes` + `Enter` | room composer | Open that room's pane (only when installed; otherwise a notice) | `/{slug}` |
| `/todo`, `/notes` + `Enter` | Pepper DM composer | Open your own pane, the same as the top-level row | `/{slug}` in `/chat` |
| `Ctrl+K` | anywhere | The jump list gains "Todos" and "Notes" (your own), first, before Pepper | — |
| `Esc` | pane | From an open note: back to the list. From a list: close the pane and go back to where you came from (the room's topic, or the Pepper DM for your own) | The stage's ✕ |
| `Esc` | composer under your own pane | `terminal-ui.md` §5's rule, with the pane standing in for the thread: the first press cancels a pending quote or `/` menu, and the next moves focus to the pane. It never closes the pane, so a draft can't strand you in the DM | — |
| `Tab` / `Shift+Tab` | room pane open | Cycle focus: sidebar → pane (no composer to reach) | — |
| `Tab` / `Shift+Tab` | your own pane open | Cycle focus: sidebar → pane → composer, the same three stops as a conversation, with the pane in the thread's place | — |
| `Enter` | nested room app row | Back to the room pane's list | Click the rail row |
| `↑`/`↓`, `PgUp`/`PgDn` | pane | Move the selection | — |
| `Enter` | todo row | Show its details (title, details, due date, source, asks) in an `InfoScreen` | — |
| `m` | Todos | Toggle "asks of me": only the todos someone asked of you | — (departure) |
| `space` or `x` | open todo | Mark done (P2) | Tick |
| `d` | open todo | Dismiss (P2) | × |
| `u` | closed todo | Reopen (P2) | Undo |
| `n` | Todos | Add a todo: `PromptScreen` for the title, then an optional due date (P2) | Add row |
| `Enter` | "Closed (N)" | Fold or unfold | `<details>` summary |
| `Enter` | note row | Open the note | Card click |
| `c` | Notes | New note in `$EDITOR`: the first line is the title (P3) | New note |
| `e` | note row or open note | Edit in `$EDITOR` (P3) | Edit |
| `D` | note row or open note | Delete, with a confirmation (P3) | Delete |
| `space` | checklist line in an open note | Tick or untick (P3) | Checkbox |
| `o` | todo or open note | Open the todo's source link, or the first link in the note | Click |
| `r` | pane | Refresh now | — |

Opening a pane puts focus on the pane, never on the composer, so the single-letter keys above act
at once. With focus in the composer under your own pane, letters type into the draft as usual.

`D` is uppercase so that `d` on a todo (dismiss, which `u` undoes) can't be misread as deleting a
note, which can't be undone from the TUI.

The Pepper DM row gets **no `a` launcher.** For your own items, the two top-level rows already
are the launcher, and the rest of your apps would only be browser links, which `pepper apps`
already lists. `/todo` and `/notes` do work in the Pepper DM composer, because typing them there is
the web's `/chat` habit and they cost nothing extra: they open the same pane the top-level row
opens.

`Ctrl+K` gets your own Todos and Notes only. Room copies aren't added, because a room's shelf is
read lazily (§6.1) and listing "Family › Todos" for rooms that turn out not to have it would
offer a dead target. `JumpTarget.conversation` widens to a target that is either a conversation
or an app pane.

## 6. Data flow

Every call runs on the TUI's existing worker path: `App._start(kind, work, deliver)` →
`run_worker(thread=True)` → `_post` back to the UI thread, with a `_Loop` per kind for busy and
generation checks (`tui/app.py`). Fetchers go in `tui/poller.py` beside `fetch_room_info`. Pure
shaping (sort, fold, sections, selection, row keys) goes in `tui/state.py`, so the coverage
ratchet holds.

A pane's **owner** is either a room (`group_id`) or you (`None`). Every fetch and write below takes
the owner and passes it as `group_id=` (todos) or `principal=` (notes), or passes nothing for your
own.

### 6.1 The room's shelf

`a` and a room's `/todo` and `/notes` need to know whether the room has the app.
`group_app_launcher(gid)` returns `(tiles, installed_unavailable)`. It's read when first needed
and cached per room for the session. The cache is dropped when room info (`i`) is re-read and
when an `app.installed` or `app.uninstalled` frame names the room; a shelf read still in flight
when that frame arrives is discarded, as is one that answers after the person has left the room. If
the read is `installed_unavailable`, the TUI says "Apps unavailable right now" and doesn't treat
the room as having no apps. This mirrors the reason the server sends the flag
(`features/groups/contract.py`, #3147). Your own panes need no shelf: the top-level rows are always
offered, as the web's Notes row is.

### 6.2 Todos (P1 read)

`list_todos(status="all", group_id=owner)`. Both `/api/v1/todos` and
`/api/v1/groups/{gid}/todos` accept `status=all` (vendored snapshot; `api/groups.py` `room_todos`),
but `PepperClient.list_todos` documents only open, done and dismissed. P1 widens its docstring and
any `Literal` to include `all`, so a pane makes one call instead of three. That's a client change
only.
`state.todo_sections(entries)` returns `(open_sorted, closed_sorted)`: open by due date (undated
last), then `created_at`, then server order; closed by `done_at` (else `created_at`), newest first.
Both match `todo.html`. A `due_is_date_only` due date shows as a bare day with no zone conversion
(`TodoEntry` docstring).

### 6.3 Your rooms' items on your own Todos (P1 read)

`GET /api/v1/todos/rooms` (`api/router.py` `list_room_todos`, already in the vendored OpenAPI
snapshot) returns the todos on each of your rooms, most recently active first, with empty rooms
left out. Its items are stored to-do documents, not `TodoEntry` rows: they carry no `assignee` or
`requested_by`, so the ask parties are rebuilt from `source_ref` (`assignee_user_id`/
`assignee_label`, `requester_id`/`requester_label`, only when `source == "request"`), as pepper's
`asks._party` does. The snapshot types the route as an open object, so the mirror is checked by an
allowlist entry in `tests/test_api_contract.py` rather than the schema. It takes `assignee=me` and `requester=me`. P1 adds `PepperClient.list_room_todos(mine=…)`
with a `# MIRROR:` comment. `state.own_todo_sections` puts your own open items first, then a section
per room of that room's open items, and merges every room's closed items into the one Closed fold,
as `todo.html` does. A room's section names the room through `sanitize_line`.

### 6.4 Notes (P1 read)

`list_artifacts(kind="inline_text", limit=200, principal=owner)`, re-sorted by `updated_at`
descending in `state.notes_order`. If `truncated`, a dim last row says "Showing 200 notes. `pepper
notes list` searches the rest", rather than implying the list is complete. Opening a note calls
`get_artifact_detail(id)` and keeps its `etag` for P3. A detail whose `kind` isn't `inline_text`
is refused, as `pepper notes` does.

### 6.5 Asks (P1 read, P2 write)

`TodoEntry.assignee` and `requested_by` are `TodoPerson`s. Under any todo that has either, a dim
line reads "asked of Sam by you", "asked of you by Sam", and so on. "You" is decided by comparing
`TodoPerson.user_id` against the core user id from `whoami` (`user_id`), not the groups
`you_person_id` used for mentions: pepper builds `TodoPerson.user_id` from the todo's
`source_ref` assignee and requester, which hold core user ids. `m` re-reads the
pane with `mine=True`, which `list_todos` already sends as `assignee=me` on a room and
`asked_of_me=true` on your own, and as `assignee=me` on `/todos/rooms`. The footer shows `m asks
of me` while it's on. In P2, `n` in a room's Todos has a third, optional step: a member picker
over the room's roster (from room info), which sends `create_todo(…, assignee_person_id=…)`. Your
own Todos has no member to ask, so the step isn't offered there.

### 6.6 Freshness

With no event frames (§3, surprise 2), the pane re-reads:

- when it opens, and when it's re-entered from its sidebar row;
- after every write it makes (the write's response is applied at once, then the list is re-read);
- on `r`, and when `m` toggles;
- on a 30s poll while it's open and the terminal is focused, paused while blurred or suspended,
  using the same `_blurred` signal as the topic poll. Nothing is polled while no pane is open.
- for an open note: when the polled list shows a different etag for it, the note is read again.

While a pane is open, the topic polls and the person stream keep running, so unread dots still
move. An optional server addition for push is §11, item 3.

### 6.7 Todo writes (P2)

`complete_todo`, `dismiss_todo`, `reopen_todo` and `create_todo`, each with `group_id=owner`. A
row in one of the room sections on your own Todos belongs to that room, so it writes with that
room's `group_id`. All four are owner-scoped single-document mutations under the document's row
lock on the server (`todo.html` header comment), so there's no rev and no retry loop. The row shows
a pending mark until the response lands, and the returned `TodoEntry` replaces the row. A failure
restores the row and shows the server's message through the existing `_action_failed`. Add takes a
title (max 500, as the web field does) and an optional due date parsed by the same rule as `todos
create --due` (`cli.py` `_parse_due`: `YYYY-MM-DD` or `YYYY-MM-DD HH:MM`, local zone). `_parse_due`
moves to a shared module so the TUI doesn't import `cli.py`. On your own Todos, `n` always adds to
your own list, never to a room section.

### 6.8 Note writes (P3)

- **Edit.** Read the detail fresh, then `with self.app.suspend():` run the editor on the raw body
  (`$VISUAL`/`$EDITOR`, `vi` fallback). On return, an unchanged body (up to one trailing newline,
  `_texts_match`) sends nothing. Otherwise `edit_artifact(id, body=…, principal=owner,
  if_match=etag)`.
- **412.** `ArtifactConflictError` carries the server's current body and etag. The TUI saves the
  user's text exactly as `pepper notes edit` does (`_save_conflict_copy`, `./<slug>.<etag>.mine.md`)
  and offers the same three choices in a `PickerScreen`: re-open the editor on their own text against
  the new etag, overwrite with the new etag, or quit (the saved copy is kept). The diff the CLI prints
  goes into an `InfoScreen`.
- **New.** `c` opens the editor on an empty template. The first non-blank line is the title and the
  rest is the body. `create_artifact(name, body, principal=owner)`. An empty result aborts.
- **Delete.** `ConfirmScreen("Delete <name>?")`, then `delete_artifact(id, principal=owner)`.
- **Checklist toggle.** `POST /api/v1/artifacts/{id}/toggle` (with `principal=gid` for a room)
  names the line by its text, and a 409 shows the server's sentence verbatim and re-reads.

The editor helper is `cli.py` `_open_in_editor`. It reports errors through `render.error` and names
its temp file `pepper-skill-`. P3 moves it to a shared module that returns an error string instead
of printing (the TUI shows it as a notice), and takes a temp-file prefix. The CLI keeps its
behaviour. Suspending needs a real terminal: under Pilot, or when `App.suspend` raises
`SuspendNotSupported`, the action shows a notice pointing at `pepper notes edit` instead.

### 6.9 Talking to Pepper over your own pane (P1)

Under your own Todos or Notes, the composer sends to **the Pepper DM**, through the TUI's existing
turn path (`chat_send_stream`, the same `_turns` queue and one-at-a-time rule as the DM). Two
fields go with it, as the web's `/chat` sends them: `subject="app:todo"` or `"app:notes"`, and over
an open note `client_context={"note_id": …}`. The server resolves both as keys and reads the data
itself (`api/router.py` `ChatSendRequest`), so Pepper answers about the list you're looking at.
`chat_send_stream` gains the two optional parameters. Both fields are already in the vendored
snapshot, so this is a client change only.

Pepper's reply shows **in the pane**, not by switching to the DM. That's the web's behaviour,
where the reply shows in the composer's pane over the app and the app stays put, and it's the
least surprising choice: you asked about the list, so the list stays on screen. A **reply strip**
docks at the foot of the pane, above the composer. It holds the exchange in flight and the one
before it (your line, then Pepper's streamed answer, markdown-rendered and sanitized as DM rows
are), scrolls if longer than about eight lines, and shows the DM's existing turn status line while
streaming. It's cleared when the pane closes. Nothing is lost: every exchange is an ordinary turn in
your Pepper thread, so opening the Pepper row shows it in full history, and the Pepper row's unread
dot behaves as it does for any turn you started.

When the turn finishes, the pane re-reads its list (§6.6), because Pepper may have just added or
ticked something. A turn already running when the pane opens keeps streaming into the DM as usual
and doesn't appear in the strip. The DM draft is shared: what you type under your own pane is the
Pepper DM's draft, the same one the DM shows.

Room panes don't get this. Asking Pepper over a room's app (pepper `pepper-over-apps.md` 3b) stays
a non-goal (§2).

## 7. Rendering

- All server text goes through `sanitize_line` (titles, names, room names, ask names, due labels,
  source labels, `summary`) or `sanitize_text` (note bodies, todo details) into `Text`, never into
  Rich markup. This is the same invariant as `terminal-ui.md` §7. A note body renders through the
  same markdown path the thread uses for message bodies, so a note can't style itself beyond what a
  message can.
- Dates use the TUI's existing local-time helpers. The web formats in the Pepper timezone on the
  server; the TUI uses the machine's zone, as `terminal-ui.md` §7 already does for messages.
- Checklist lines (`- [ ]`, `- [x]`, outside code fences) render as `☐`/`☑`. The rule for what
  counts as a checklist line is the server's (`agent/artifacts/notes/checklist.py`), so a small
  `state.checklist_lines` mirrors it, with fixture tests copied from the server's own cases.
- Nothing shows a count except "N open" on Todos and the "Closed (N)" fold, which are the web's own.
  No Todos or Notes row, top-level or nested, carries a dot.

## 8. Package layout

- `tui/apps.py` (new): `AppPane` (a `ContentSwitcher` of `TodoList`, `NoteList`, `NoteView`) and its
  bindings. Widget glue only.
- `tui/state.py`: `todo_sections`, `own_todo_sections`, `notes_order`, `checklist_lines`,
  `ask_line`, `AppPaneState` (owner, which app, which note, selection, fold, asks filter); the
  top-level `kind="own_app"` rows and separator in the sidebar rows ahead of Pepper; the nested
  `kind="app"` row in `_room_children`; `jump_targets` gaining your own Todos and Notes.
- `tui/poller.py`: `fetch_room_shelf`, `fetch_todos(owner, mine)`, `fetch_own_room_todos(mine)`,
  `fetch_notes(owner)`, `fetch_note`.
- `tui/app.py`: the `a` binding, `/todo` and `/notes` in `_composer_key`/send (room and Pepper DM),
  pane open and close, composer hide for a room's pane, the composer's `subject`/`client_context`
  and the reply strip under your own pane, focus cycle per pane, the jump target type, and the 30s
  loop in `_LOOPS`.
- `api_client.py`: `list_todos` accepts `"all"` (P1); `chat_send_stream(subject=…,
  client_context=…)` (P1); `list_room_todos` (P1, `# MIRROR:
  src/pepper/api/router.py::list_room_todos`); `toggle_checklist` (P3, `# MIRROR:
  src/pepper/api/artifacts.py::ArtifactToggleResponse`). Both routes are already in the vendored
  OpenAPI snapshot.
- Shared helpers moved out of `cli.py`: `_parse_due` (P2), `_open_in_editor`, `_texts_match`,
  `_save_conflict_copy` (P3).
- `README.md` and the bundled `SKILL.md` get a line per phase.

## 9. Testing

- **`state.py`** unit tests carry the coverage: the due-first sort with undated, date-only and timed
  due dates and ties; the closed fold order, including room closed items merged into your own; room
  sections in route order with empty rooms absent; the ask line for each of you/them/both/neither;
  the `updated_at` re-sort; checklist parsing against the server's cases (fenced code, nested,
  `[X]`); the top-level rows always first; the nested app row appearing first, only for the open
  room, and only while its pane is open; jump targets starting with Todos and Notes.
- **Pollers** with pytest-httpx: the shelf (installed, none installed, `installed_unavailable`),
  room and own todos with and without `mine`, `/todos/rooms`, notes with `truncated`, a 403/404 on
  a room the caller left.
- **Pilot** (`App.run_test`, fake server, as in `tests/test_tui_parity.py`): `Enter` on the
  top-level Todos row opens your own pane with the header `Todos`; `a` → picker → Todos opens a
  room's with the header `Room › Todos`; `/notes` in a room composer and in the Pepper composer open
  the right copy; the composer is hidden under a room's pane and the draft survives; under your own
  pane it stays, a send carries `subject` (and `note_id` over an open note), the reply streams into
  the strip, the list re-reads when the turn ends, and the DM's history holds the exchange; `Tab`
  cycles the right stops for each pane; `Esc` in that composer moves focus to the pane; `Esc` walks
  note → list → where you came from; `m` re-reads with the filter; an uninstalled room shows a
  notice; `Ctrl+K` "Notes" opens your own. P2: done, dismiss, reopen and add round-trip (including
  a room-section row writing to its room, and asking a member); a failed write rolls back. P3: edit
  with `suspend` patched to run a fake editor, and a 412 with each of the three choices; delete
  confirm and cancel.
- **Snapshots** for the layouts in §4 once P1 settles, as `terminal-ui.md` §11 does.
- **Windows**: nothing new. Pilot is headless. `$EDITOR` suspend is checked by hand in Windows
  Terminal before P3 merges.

## 10. Ordering and failure posture

- A pane read that fails shows the error in the pane, with `r` to retry. It never closes the pane,
  and the last good list stays up, marked stale.
- On your own Todos, a failed `/todos/rooms` read shows your own items and one line, "Couldn't load
  your rooms' todos", instead of failing the pane.
- A room the caller has left (403/404) closes that room's pane with a notice. The existing rail
  refresh removes the room. On your own Todos, the room's section drops out on the next read.
- Switching rooms closes a room's pane. The pane belongs to one room, as a room's applet does on the
  web. Your own pane closes when you open any conversation.
- A worker result for a pane or note that's no longer open is dropped by the existing generation
  check.

## 11. Server changes

**None needed for P1–P3.** All routes exist: `GET /api/v1/groups/{gid}/apps/launcher`, `GET/POST
/api/v1/todos` and `/api/v1/groups/{gid}/todos` with their `…/{id}/done|dismiss|reopen`, `GET
/api/v1/todos/rooms`, `GET /api/v1/artifacts`, `GET …/{id}/detail`, `POST …/new`, `POST
…/{id}/edit` (If-Match/412), `POST …/{id}/toggle`, `POST /api/v1/apps/notes/actions/delete`.

Optional, all additive under `/api/v1`. None is filed now (§12 decisions 6 and 9):

1. `order_by=updated_at` on `GET /api/v1/artifacts`. Makes the list exact past 200 notes. File it
   if a real room or person passes 200 notes.
2. A `preview` field on list rows, the board's ~400-char opening, so a list row can show the
   opening of the note as the board does.
3. `todo.changed` / `artifact.changed` frames on the person stream, carrying ids only, as the
   stream's other frames do. Would let the pane drop its poll. Revisit after P1 is in daily use.

## 12. Decisions

| | Decision |
|---|---|
| 1 | **2026-10-28, Allen.** Model it as the web does. A room's notes and todos are reached **through the room** (its launcher, its `/` commands, its nested rail row), not as top-level tabs. Mirror the web's entry points, ordering and what it shows. Departures are listed in §3, each with its reason. |
| 2 | **2026-10-28, Allen (was question A).** Your own items get **two top-level rows, Todos and Notes**, above Pepper, where the web's Primary nav sits above Messages. Notes matches the web's Notes row. Todos is a stated departure, since the web has no To-dos row. Your Todos also lists your rooms' open items, a section per room, as the web's does. They open the same pane as a room's app, with the header `Todos`/`Notes` (the composer rule is decision 10). The Pepper DM row gets no `a` launcher, but `/todo` and `/notes` in its composer open the same panes (§5). `Ctrl+K` lists both. Both are in P1, read-only, alongside the room panes. |
| 3 | **2026-10-28, Allen (was B).** A room's entry points stay at parity: `a`, `/todo` and `/notes`, with the room's app row shown only while it's open. The footer shows `a apps` whenever a room is open. |
| 4 | **2026-10-28, Allen (was C).** Rooms without Todos or Notes installed aren't offered them. `a` lists only what's installed, and `/todo` in such a room says "Todos isn't installed in this room. Install it from Room info on the web." |
| 5 | **2026-10-28, Allen (was D).** Show asks: a dim "asked of X by Y" line and the `m` filter in P1; asking a member in P2 (§6.5). A departure from the web todo page, which draws neither. |
| 6 | **2026-10-28, Allen (was E).** A 30s poll while a pane is open (§6.6). Server event frames are revisited after P1 is in daily use. |
| 7 | **2026-10-28, Allen (was G).** P2's add takes an explicit due date, `YYYY-MM-DD` or `YYYY-MM-DD HH:MM` (the `todos create --due` rule), blank for none. No phrase parsing; no set-due or reminder on existing rows for now. |
| 8 | **2026-10-28, Allen (was H).** Checklist ticking in the note viewer is in P3, with `toggle_checklist` added to `PepperClient`. |
| 9 | **2026-10-28, Allen (was F).** Notes re-sort by `updated_at` on the client, with a "Showing 200" row when `truncated`. `order_by` (§11 item 1) is filed only if a real list passes 200. |
| 10 | **2026-10-28, Allen.** Under **your own** Todos and Notes panes, the **Pepper DM composer stays visible**, matching the web's `/chat`. It sends to the Pepper DM with `subject: app:{slug}` (and the open note), and Pepper's reply shows in a strip at the foot of the pane while the pane stays (§6.9). Under a **room's** app pane the composer stays hidden, as the web hides the groups bar. |

## 13. Questions

None open. Former questions A–H are decisions 2–9 in §12.

## 14. Phases

One PR each. Each phase gets an issue when it's picked up.

| Phase | Contents | Depends on |
|---|---|---|
| P1 | **Read-only, your own and rooms'.** Top-level Todos and Notes rows and `Ctrl+K` targets; your own Todos with room sections (`list_room_todos`); room shelf; `a` launcher picker (non-native apps open in the browser); `/todo` and `/notes` in room and Pepper composers; the app pane with Todos (sections, fold, details, ask line, `m`) and Notes (list, viewer, checklist render, `o`); the nested room app row; composer hide/restore under a room's pane; the Pepper composer, `subject`/`client_context` and reply strip under your own; 30s poll while open; `list_todos("all")`; tests from §9; README/SKILL.md lines | `terminal-ui.md` P3 (merged) |
| P2 | **Todo actions.** Done, dismiss, reopen, add with an optional due date, on your own list, your room sections and a room's list; asking a member in a room's add; `_parse_due` moved to a shared module | P1 |
| P3 | **Note writes.** New, edit through suspended `$EDITOR` with If-Match and the three-way 412 resolution, delete with confirm, checklist toggle, for both copies; editor and conflict helpers moved out of `cli.py`; a manual Windows Terminal check | P1 (P2 not required) |
