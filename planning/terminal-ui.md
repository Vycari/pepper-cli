# Terminal UI: rooms, topics and Pepper in one full-screen view

**Status:** Proposed. The maintainer has decided the items in §14. The lettered questions in §15 are still open.
**Author:** drafted with Claude Code for the maintainer
**Last updated:** 2026-10-16
**Tracked by:** #54
**Related:** `planning/cli-information-architecture.md` (nouns and `--json`); pepper
`planning/nav-consolidation.md` (pepper#4807, left nav), pepper#4829 (poll-cursor race fix);
server paths below are relative to `src/pepper/` in Vycari/pepper.

## 1. Problem

Today, bare `pepper` opens a line REPL (`cli.py` `_run_repl`) that talks only to the personal Pepper
conversation. A person who uses rooms has to leave it and run one-shot commands (`pepper messages
list`, `pepper messages create`). Nothing tells them that a room has moved on, and they can't reply
to a room in context. The web has one place for all of this: the **Messages** section of the left
nav. The terminal should have the same.

## 2. Goals and non-goals

**Goals**

- One full-screen view with a sidebar: the Pepper DM first, then rooms, with topics nested under
  rooms. Next to it, the open conversation, and below that a composer.
- Match the web wherever a terminal can: names, ordering, the unread dot, message layout and
  composer keys.
- Live enough to use all day. New messages appear without any keypress, and so do unread dots in
  other rooms.
- Keep the agent surface unchanged. Exit codes, stdout and `--json` behave exactly as they do today
  for every non-interactive use.
- Works on macOS, Linux and Windows Terminal, and can be tested headless in CI on all three.

**Non-goals**

- Inbox, Calendar, To-dos, Notes, Apps. These stay as one-shot commands. A TUI tab for them can be a
  later doc.
- Room admin: members, sharing, room settings, installing apps. These stay as commands.
- Images inline. Pictures and attachments show as a name plus a URL.
- Push notifications outside the terminal. The most a phase may add is the terminal bell or a title
  change (§15 C).
- Replacing the REPL for scripted or piped use. It stays.

## 3. What the web does, mapped to the terminal

Read on origin/main 2026-10-16. Chris's nav consolidation (pepper#4807) reorders only the top of the
nav (Inbox, Calendar, To-dos, …) and puts **Messages** last. It explicitly leaves rooms out of
scope, so the Messages section described here is stable.

| Web behaviour | Source | Terminal |
|---|---|---|
| A "Messages" section label with a compose button ("Start a conversation") | `web/templates/_sidebar.html` | Sidebar title "Messages". Room create is out of scope for P1–P3 |
| **Pepper** row first (`/chat`, "P" avatar). It has **no unread dot** | `_sidebar.html` | First row, "Pepper". No dot in P1 (§15 D) |
| Rooms below, recency-ranked. Each row shows avatar/colour, name, a sub-line ("N people · active 2h ago") and an unread dot | `_sidebar.html`, `web/static/shell.js` `fillGroupsRail`, `api/groups.py` `list_groups` | Server order kept as-is. The name shows in the room `color`, the dot `●` sits at the right edge, and the sub-line shows in a footer for the highlighted row only |
| Only the **active** room expands to show its topics. Others stay collapsed | `shell.js`, `GET /groups/{id}/topics/nav` | Same rule: the selected room auto-expands. `→`/`←` expand or collapse any room by hand (§14 decision 2) |
| Topics in recency order with unread dots, capped at 5 behind "Show more". Archived topics sit behind "Show archived (N)"; "+ New topic" | `shell.js` | Recency order with dots, and the same 5 + "Show more" (§15 F). Archived topics fold. "New topic" waits for P3 |
| Stale rooms fold under "Show inactive (N)" (server `inactive` flag) | `_sidebar.html` | P3. Computed client-side from `topics[].last_activity_at` against the same threshold, because `/api/v1/groups` has no `inactive` flag |
| Exactly one Messages row has the accent bar | `_sidebar.html` | Tree cursor highlight. The open conversation also gets a `▌` marker |
| Header shows the room name and topic name, with an "archived" pill and "Room info" | `web/templates/_group_header.html` | Header line `Room › Topic`, with `[archived]` where it applies. Room info is P3 |
| Unread is a **dot, not a count** ("a number would claim a precision…") | `web/static/unread_badge.js`, `web/static/groups_unread.js` | `●`. Never a number. The terminal title gets a `●` prefix while anything is unread |
| Read marks: the server is the store of record. `GET /groups/unread` seeds them and `PUT …/topics/{tid}/read` publishes. An open topic is always read. Your own sends never badge | `web/static/groups_read_marks.js` | Same endpoints (§6.3). Unread itself is the server's `unread` rows (read-state-sync phase 5c) |
| Room poll is adaptive: 3s while active (interaction within 60s), 10s cooling (to 120s), 15s idle, paused while hidden. The rail polls every 15s with an ETag | `web/static/group_poll_cadence.js` (mirrors iOS `GroupLogic.roomPollInterval`) | Same cadence. "Hidden" maps to the terminal losing focus, where Textual reports it, and to the screen being suspended |
| Poll cursor = newest `created_at` the server returned, minus a 10s lookback. Rows are deduped by id and placed in `(created_at, id)` order | `web/static/group_room.js` (pepper#4829) | Same rule, in `poller.py` (§6.2) |
| One composer. Enter sends; Shift+Enter or Alt+Enter adds a newline. An `@` menu comes from the roster (none in chat). Quote-reply works in rooms only, and Esc cancels it. A slash menu (apps, commands, skills, topics). Drafts are kept per destination. The placeholder names the room | `web/static/composer.js` | Enter sends. **Alt+Enter or Ctrl+J** adds a newline, because most terminals can't tell Shift+Enter from Enter. Mentions, quote-reply, drafts per destination and the slash menu are covered in §5 and §13 |
| A room send is a POST with no stream. Pepper's reply arrives through the room poll. A chat send is a POST `/chat/send` that streams over SSE | `composer.js` `transport` | Same two transports (§6.4, §6.5) |
| DM catch-up: `/chat/messages?since=` (session-only HTML) runs when you return to the tab or after a dropped stream | `web/chat/router.py` | No PAT equivalent. Re-fetch `GET /api/v1/chat/history` and diff by id (§6.5, §15 E) |

**Two surprises.**

1. **The DM is not a room.** It is `/api/v1/chat/*`: one persistent conversation per user. The PAT
   history route (`api/router.py` `GET /chat/history`) returns today's live thread only, with no
   pagination and no `since`. It has no read mark and no unread dot on the web.
2. **Humans do get a push stream, and the TUI follows it.** `GET /api/v1/groups/events/stream`
   serves a signed-in person (session cookie or PAT) their own feed: their rooms' events, the kinds a
   client needs, and `read_state.advanced` for their own read cursor, as `text/event-stream` with an
   `event: heartbeat` every ~15s and `Last-Event-ID` resume (`api/groups.py` `agent_events_stream`,
   `person_feed.py`). This note once said a PAT got 403 here ("agent members only"); that was true
   of the room-scoped and agent routes and stopped being true for this one when the person feed
   landed (pepper `planning/ui-snappiness.md` Phase 13). The stream carries metadata only, so the TUI
   uses it as the cue to fetch, and keeps its polls as the fallback (§6.2).

## 4. Layout

```
┌ Messages ──────────────────┐┌ Family › Trip ────────────────────────────────────────────┐
│▌Pepper                     ││                    ── Today ──                            │
│ Family                   ● ││ Sam                                               9:41 AM │
│   General                  ││   Booked the cabin for the 14th.                          │
│ ▌ Trip                     ││   📎 cabin-confirmation.pdf                                │
│   Packing                ● ││   👍 2                                                    │
│   Show more (3)            ││ Pepper (for Sam)                                  9:42 AM │
│ Book club                  ││   ┃ Booked the cabin for the 14th.                        │
│ Work                     ● ││   Added "Cabin trip" to the Family calendar, 14–16 Oct.   │
│ Show inactive (2)          ││ You                                               9:50 AM │
│                            ││   @Sam can we bring the dog? (edited)                     │
│                            ││                                                           │
│                            │├───────────────────────────────────────────────────────────┤
│                            ││ Message Family › Trip                                     │
│ 4 people · active 2m ago   ││                                                           │
└────────────────────────────┘└───────────────────────────────────────────────────────────┘
 ^N next unread  ^K jump  Tab focus  Enter send  Alt+Enter newline  r reply  ^Q quit
```

The sidebar is 28–32 columns wide and can be hidden with `Ctrl+B` so that narrow terminals (< 80
cols) can use the thread pane full-width. The footer shows key hints only. There is no unread
count anywhere, following the web's dot-not-count rule; `Ctrl+N` goes to the next unread.

## 5. Keybindings

| Key | Action | Web analogue |
|---|---|---|
| `Tab` / `Shift+Tab` | Cycle focus: sidebar → thread → composer | — |
| `↑`/`↓` (sidebar) | Move. `Enter` opens the conversation. `→`/`←` expand or collapse a room | Click |
| `Ctrl+N` / `Ctrl+P` | Next / previous **unread** conversation, opening it | — |
| `Ctrl+K` | Fuzzy-jump to any room or topic by name | — |
| `Ctrl+B` | Toggle the sidebar | — |
| `Enter` (composer) | Send | Enter |
| `Alt+Enter`, `Ctrl+J` | Newline | Shift+Enter / Alt+Enter |
| `Esc` (composer) | Cancel the quote. A second press moves focus to the thread | Esc |
| `/` (thread or sidebar) | Focus the composer and type `/` | `/` |
| `↑`/`↓`, `PgUp`/`PgDn` (thread) | Select a message and scroll. Scrolling up past the top loads older messages (P3) | Scroll |
| `r` (thread, message selected) | Quote-reply to that message (rooms only) | Reply |
| `e` / `d` (own message) | Edit / delete, with a confirmation (P3) | Menu |
| `+` (message selected) | React, with a short emoji picker (P3) | Reaction bar |
| `o` (message selected) | Open the first link, attachment or card URL in the browser | Click |
| `Ctrl+Q`, or `/quit` in the composer | Quit | — |

In the composer, `@` opens the roster menu (rooms only) and `/` at the start opens the slash menu.
Both reuse `_SlashCompleter`'s list of commands and skills, with topics added.

## 6. Data flow

All network calls run in Textual thread workers (`@work(thread=True)`) on the existing sync
`PepperClient`. Results come back to the UI through `call_from_thread`. No async httpx client is
needed. The spike in §8 checked this pattern.

### 6.1 Initial load

1. Load credentials and the profile exactly as the REPL does (a 401 shows the login hint and exits
   1, as `_report_api_error` does today).
2. `GET /api/v1/groups` (`list_groups`). If `enabled=false`, the sidebar is **Pepper only**, with no
   rooms section and no error. If `degraded=true`, a one-line notice says rooms may be stale. Keep
   the ETag.
3. `GET /groups/unread` → every room's `unread` rows (`{group_id, topic_id, read_through, floor,
   unread_count, has_unread_mention}`; `read_through` is the read mark the count was made from) and
   `unread_total`, the server's deduped count. One call for all rooms, cached for the session and
   asked again only when a room's topics moved. A total shown anywhere is `unread_total`, never a
   sum of the rows.
4. `GET /groups/{gid}` for the **open** room only, to get `members[]`: `is_you` (the viewer's
   `person_id`, used for mentions) and the roster for `@`.
5. Open the Pepper DM by default. Optionally, reopen the last conversation (its id is the only
   local state, kept beside the REPL history file).

### 6.2 Live updates (the person stream, with polling as the fallback)

The TUI opens the person event stream (§1 surprise 2, `tui/stream.py`) once `whoami` has answered,
as the web does (`groups_stream.js`) and iOS does (`GroupsLiveStream`). The stream carries ids and
kinds, never bodies, so a frame is a cue to fetch through the paths below, not a replacement for
them:

- `read_state.advanced` (`{topic_id, group_id, last_read_at}`) is merged into the read marks
  directly, forward only, comparing at the server's full (microsecond) precision
  (`ReadMarks.merge_advanced`). A mark that has passed the topic's newest activity clears its
  cached unread row (`ReadMarks.settle`) and the sidebar repaints; no rail request is made. A
  mark short of it (a partial read) leaves the server's count in place and asks the rail again.
- A message, mention, member, room or app event fetches the rail now (a 304 when nothing changed),
  and the open topic too when the event names it. `chat.message` fetches the Pepper DM.
- An edit, delete or preview event for a row of the open topic that is already on screen reads
  that row again by itself (`?at=` its id, keeping only that row; one request at a time), since
  a row paged back to is in no poll's window. A row the server no longer has becomes a
  tombstone; a failed read is retried with backoff, and paused while the terminal is blurred.
  An event for a row not held asks the next poll to reconcile the newest page.
- A connection that names no position starts at the feed's head and opens with a `cursor` frame;
  the app catches up with one fetch of everything. A drop reconnects with `Last-Event-ID`. The
  server's `cursor_ahead` error (the saved position is past every id it issued) catches up and
  reopens with `?since=` the id it names. Unknown frame kinds are ignored.
- While the stream is healthy the rail and open-topic polls run as a safety net (120s and 60s). When
  it drops they return to the cadence below; a 401/403/404 on the stream, or a final `retry: false`
  error, ends it for the session and the polls stay at their normal rate.

The cadence below is what runs with no stream.

- **Sidebar**: `GET /api/v1/groups` every 15s with `If-None-Match`. On a change, recompute order and
  dots.
- **Open topic**: `GET …/messages?since=<cursor − 10s>` at the adaptive 3/10/15s cadence (§3).
  Following pepper#4829:
  - The **cursor is the newest `created_at` the server returned**, and nothing else.
  - Your own echo never moves it.
  - Rows are deduped by `id` and inserted in `(created_at, id)` order, never just appended.
  - A reconcile poll on every 5th tick re-reads the recent window (`limit` of a page) to catch
    edits and deletes, and any row the lookback missed.
- **Pinned to bottom**: if the thread is scrolled to the end, new rows keep it pinned. If the user
  has scrolled up, the view stays put and a `↓ N new` marker appears.

### 6.3 Unread and read marks

The server decides what is unread (read-state-sync §6, phase 5): messages newer than the later of
the person's cursor and their floor, not their own, not tombstones, through the context rule.
`ReadMarks` in `tui/state.py` is the cache of that answer:

- A topic is unread when the server's `unread` row for it has `unread_count > 0`. A topic with no
  row (an older server, a room whose read failed) is not unread: unknown is not unread. The
  client never raises a count.
- A room is unread when any of its non-archived topics is.
- **No first-sight seed.** A fresh profile shows exactly what the server says; nothing is written
  back (first sight never PUTs).
- The one local derivation can only clear: a cursor (our own mark, or `read_state.advanced`) at or
  after a topic's newest activity leaves nothing unread under any rule, so the cached row is
  dropped until the server's next answer. A cursor short of it clears nothing. Only a cursor that
  moved after the server counted the row can clear it: the rail reads `/groups` before `/groups/unread`,
  so the activity it compares against can be older than the answer. For the same reason every
  row is kept, including one counted from a cursor older than ours; the newer cursor settles it.
- **Read = the newest row has been on screen** (read-state-sync §5.2): with the terminal focused
  and the thread at its end, the newest row's `created_at` is published as `PUT …/topics/{tid}/read`
  (the server's own string, never a device clock). **Scrolled up, nothing is marked**, and the open
  topic stays unread (the server's count) while rows sit below the screen; reaching the end marks the newest row. Your
  own send puts the thread back at the end and counts as read once the server stores it, if the
  thread is still there: a send that lands after you scrolled up, blurred the terminal or left
  the topic marks nothing (rows stored before it have not been on screen; groups does not advance
  a cursor on post, Q7). Publishes are coalesced to at most
  one in flight. The server is monotonic, so a retry is harmless.
- A read on another device arrives as `read_state.advanced` (§6.2) and is merged forward only, so
  its marker clears here without a rail poll.
- Marks are held in memory. Nothing goes to a local file: the server is the store of record, and
  that keeps the TUI and web in agreement.

**Mentions.** A message mentions you when `message.mentions` contains your `person_id`. In the
**open** topic this is free, and the row shows in the accent colour with an `@` marker. For other
topics the server's unread row carries `has_unread_mention`, so a mention marker costs no message
fetch (the per-topic fetch of P4 retired with read-state-sync phase 5c). §15 C.

### 6.4 Posting to a room (P2)

- `POST …/messages` (`post_group_message`) with an `idempotency_key` generated per send attempt and
  reused on retry. Send `mentions` resolved from the `@` menu, and `parent_message_id` for a
  quote-reply.
- Show the row at once as *pending* (dimmed). When the poll returns the same id, swap the pending
  row for the real one. Pending rows never badge and never move the cursor.
- The response runs no turn. Pepper's reply arrives through the next poll, as on the web. After a
  send, the cadence goes to "active" (3s).
- On failure, the row stays with `failed, r to retry`, and retrying reuses the key.

### 6.5 The Pepper DM (P1 read, P2 send)

- **Read**: `GET /api/v1/chat/history` on open, then again on each idle tick of the same cadence,
  diffed by `id`. This catches phone replies, scheduled-task announcements and drafts that land
  while the TUI is open. **That goes further than the web**, which catches up only on tab return or
  after a dropped stream.
  - The cost is one small request every 15s while the DM is open.
  - It is today's thread only, and the header says "Today", as the web does.
- **Send**: `chat_send_stream` in a `turn` worker. Thought, tool-call and retry events render as a
  dim live status line above the composer. The final `reply` renders as a message.
  - The event-kind dispatch is lifted out of `_stream_one_turn` into a shared function, so the REPL
    and the TUI handle the same kinds and skip unknown ones the same way.
  - **Client tools** (`--root`): `client_tool_call` is served synchronously inside the worker
    thread, exactly as in the REPL. `pepper tui --root DIR` is accepted.
- **Stream drop**: never resend the turn. Show "connection lost, checking…" and re-fetch history
  until the reply row appears (or 2 minutes pass). Then show it, or show "no reply yet".
- The composer stays usable while a turn streams. A second send waits until the turn finishes
  (button-dim state), as on the web.

### 6.6 Reconnect

Every poller backs off on a network error: 3 → 6 → 12 → 30s, capped. The header shows `offline,
retrying`. On recovery:

- the sidebar refetches without an ETag;
- the open topic polls from its cursor (lookback included), and the reconcile tick then repairs
  edits;
- read marks owed from a failed PUT are republished on the next non-empty poll, as in pepper#4829.

A 401 at any point stops the pollers, shows the login hint, and exits 1 after a keypress. That
matches the REPL bailing out on a 401.

## 7. Rendering parity

| Element | Web | TUI | Phase |
|---|---|---|---|
| Body | `rendered_html` (server markdown) | `raw_body` through `sanitize_text`, rendered with **`markup=False`**. This is an invariant: message text never becomes Rich markup | P1 |
| Markdown | Server dialect | Rich `Markdown` on `raw_body`, after sanitizing. The dialect may differ from the server's (tables, autolinks), and we accept that. Plain text stays the fallback | P3 |
| Author, time, run grouping | `display_name`, `created_at_display`, `run_start` | Name line only on `run_start`. Time right-aligned. Name shown in the `avatar_tint` colour | P1 |
| Day divider | `day_label` | `── Today ──` centred | P1 |
| Agent/via tags | "for X", "via X" | `(for X)`, `(via X)`, the same strings as `render_group_messages` | P1 |
| Edited / deleted / broken | badge / tombstone | `(edited)`; `Message deleted` dim; `Couldn't show this message` dim | P1 |
| Quote | `quote` / `quote_excerpt` | A `┃ excerpt` line above the body | P1 |
| Mentions | `mention_names` highlighted | `@Name` in bold. A mention of you shows in the accent colour | P1 |
| Reactions | chips | `👍 2  ❤ 1` line. You can add your own in P3 | P1 read, P3 write |
| Attachments, pictures | thumbnails, files | `📎 name (size)`. `o` opens the URL | P1 |
| Link previews | cards | `↗ title — domain`, one line | P3 |
| App cards (`cards[]`) | rich card | Boxed `title` + `summary_lines`, with `o` to open `url` | P3 |
| DM drafts / schedule proposals / brief | interactive cards | Title + summary only, with "open in web" | P3 |
| Load older | scroll up | `before=` page, inserted at the top | P3 |

Emoji widths: Rich measures them. On Windows conhost (not Windows Terminal), wide glyphs may
misalign, so the plan uses `●`, `▌` and `┃`, which are single-width.

## 8. Framework: Textual

**Recommendation: Textual.** A spike in the scratch dir (not committed) ran Textual 8.2.8 on Python
3.13. It used a Tree sidebar, a scrolling message pane, and a `TextArea` composer with a priority
`Enter` binding, plus a thread-worker poller and a streamed turn. A Pilot test passed headless in
1.7s. The test typed into the composer while poller rows kept arriving, checked that the draft was
untouched, pressed Enter, and saw the streamed reply.

| | Textual | prompt_toolkit full-screen `Application` |
|---|---|---|
| Already a dependency | No | Yes (`>=3.0.53`) |
| Headless widget tests | `App.run_test()` + Pilot (press keys, query widgets, snapshot) | No equivalent. Tests would drive `create_pipe_input` by hand |
| Rich rendering | Native: Rich renderables and `Markdown` | Rich output would have to be converted to ANSI and then to formatted text |
| Layout | CSS-like (`.tcss`), docks, scroll containers, Tree, TextArea | Manual `HSplit`/`VSplit`. Write our own tree and scroll views |
| Background work | `@work(thread=True)` + `call_from_thread`. Fits the sync httpx client | asyncio. Needs `run_in_executor` around the sync client |
| Windows | Supported (Windows Terminal; conhost degraded) | Supported |
| Cost | ~3.9 MB wheel. Pulls `markdown-it-py[linkify]`, `mdit-py-plugins`, `platformdirs`, `pygments`, `typing-extensions` (all pure Python; Rich already pulls some). **Raises our Rich floor from `>=13` to `>=14.2`.** `import textual.app` takes about 180 ms | 0 |

prompt_toolkit would save the dependency, but we would spend that saving several times over by
hand-building a tree, a scroll view and test harnessing. It would also give us a weaker test story,
and the coverage ratchet makes that matter. Textual's cost is mitigated by importing it lazily, only
from `pepper tui`, so `pepper messages list` doesn't pay the 180 ms. Packaging is §15 A.

## 9. Package layout

```
src/pepper_cli/tui/
  __init__.py      run(client, args) -> int; the only import cli.py makes (lazily)
  app.py           PepperApp: compose, bindings, focus, quit
  sidebar.py       Messages tree: Pepper row, rooms, topics, dots, folds
  thread.py        message list widget, pinning, selection, "N new" marker
  composer.py      TextArea subclass: Enter/Alt+Enter, @ and / menus, quote, per-destination drafts
  poller.py        workers: rail poll, topic poll (cursor + lookback + reconcile), DM history poll, backoff
  state.py         pure: ordering, the server-unread cache, cadence, cursor merge, draft keys
  rows.py          GroupMessage / ChatHistoryEntry -> Rich renderables (shares helpers with render.py)
  styles.tcss
```

- `cli.py` gains a `tui` subcommand (`pepper tui [--root DIR]`) and, in P4, the bare-`pepper`
  branch. Everything else stays where it is.
- `render.py` stays the only place that writes to **stdout**. The TUI draws to the alternate screen
  and prints nothing to stdout, so `rows.py` returns renderables rather than printing. Shared pieces
  (tag strings, the sanitizer) move into small helpers that `render.py` and `rows.py` both import.
- The SSE event-kind dispatch is lifted out of `_stream_one_turn` into one function that both the
  REPL and the TUI call (§6.5).
- `SKILL.md` (`src/pepper_cli/data/agent-skill/SKILL.md`) gains one line: `pepper tui` is for
  humans, and agents should use the commands. This lands in P1 with the subcommand, per the SKILL
  invariant.

## 10. Entry points and the REPL fallback

| Invocation | Today | After P4 |
|---|---|---|
| `pepper` on a TTY | REPL | **TUI** (§15 B) |
| `pepper` with stdin or stdout not a TTY, or `TERM=dumb`, or `PEPPER_NO_TUI=1` | REPL | REPL, unchanged |
| `pepper chat` | REPL | REPL, unchanged. This is the explicit fallback |
| `pepper <words>` (implicit chat) | one turn, printed | unchanged |
| `pepper tui` | — | TUI (P1 on). If stdout is not a TTY, exits 2 with a one-line error |
| Every other command | — | unchanged |

Invariants: exit codes and stdout of every non-interactive invocation are byte-for-byte unchanged.
The TUI exits 0 on a normal quit, 1 on auth failure (as the REPL does) or an unexpected error, and 2 on
usage errors such as no TTY, after restoring
the terminal and printing the traceback hint to stderr.

## 11. Testing

- **`state.py`**: plain unit tests covering ordering, the server-unread cache (the server's rows, a
  cursor clearing a row, a stale answer), cursor and lookback merging, cadence transitions, and draft keys. These carry most of
  the coverage.
- **Pollers**: pytest-httpx, as the rest of the suite does. Cover the ETag 304 path, the 403
  `enabled=false` path, backoff, a 401 stop, and the pepper#4829 race (an own echo, then an
  earlier-committed row, which must still land, once and in order).
- **Widgets**: `App.run_test(size=…)` + Pilot. Cover sidebar navigation, `Ctrl+N` to the next
  unread, sending while rows arrive, an Esc-cancelled quote, and a turn that streams with a
  client-tool call. No real network, because the client is injected.
- **Snapshots**: hold `pytest-textual-snapshot` back until layout settles (P3), so CI isn't churned
  by intentional visual changes.
- **Windows**: Pilot is headless (no console driver), so the same tests run in the existing
  `windows` CI job unchanged. A manual smoke run in Windows Terminal before P4 flips the default.
- **Coverage**: `tui/` counts toward the ratchet from P1. Untested widget glue can't lower the floor,
  so state logic belongs in `state.py`.

## 12. Server changes

**None needed for P1–P4.** Everything above uses existing PAT routes: `/groups`, `/groups/{id}`,
`/read`, `/topics/{tid}/read`, `/messages` (GET/POST/PATCH/DELETE), reactions, `/chat/history`,
`/chat/send`.

These are optional, all additive under `/api/v1`, and open as server issues only if §15 E says so:

1. `GET /chat/history?since=` (or an id cursor). Lets the DM poll fetch only new rows instead of the
   whole day.
2. ~~A PAT-authed person event stream~~ Landed server-side (§1) and followed by the TUI (§6.2).
3. `inactive` on `/api/v1/groups` rows, matching the web rail. Today the client computes it.

## 13. Phases

One PR each. P1 is releasable on its own.

| Phase | Contents | Depends on |
|---|---|---|
| P1 | **Read-only TUI.** `pepper tui`; sidebar (Pepper DM first, rooms by recency, topics nested, the selected room expanded, unread dots from server marks); message pane with the P1 rows from §7; rail and topic polling with the pepper#4829 cursor; marking read on view; DM history read and poll; reconnect and backoff; Textual added (§15 A); SKILL.md line; tests from §11 | — |
| P2 | **Posting.** Room composer with idempotency key, pending rows, `@` mentions from the roster, quote-reply; DM turns streamed in the pane with status line and client tools (`--root`); drafts per destination; shared SSE dispatch with the REPL | P1 |
| P3 | **Parity.** Reactions, edit/delete own, load older, link previews and app cards, markdown bodies, "Show more"/archived/inactive folds, new topic, room info, `o` to open, `Ctrl+K` jump, snapshot tests | P2 |
| P4 | **Default entry.** Bare `pepper` on a TTY opens the TUI; REPL stays for `pepper chat`, non-TTY, `TERM=dumb`, `PEPPER_NO_TUI=1`; optional bell or title on mention (§15 C); README and release notes | P3 + Windows Terminal smoke |

## 14. Decisions (2026-10-16, by Allen)

| | Decision |
|---|---|
| 1 | The personal Pepper DM appears in the sidebar, as the first row, as if it were a room. On the server it is not a room, so the TUI hides that difference. |
| 2 | Topics nest under their room. As on the web, the selected room auto-expands and the others stay collapsed. `→`/`←` expand or collapse any room by hand. |
| 3 | Follow the web UI as closely as a terminal allows: layout, naming, ordering, unread/mention treatment, rendering and composer behaviour. Departures are listed in §3 and §7, each with its reason. |
| 4 | Design doc first, with phases. One tracking issue, and one issue per phase as each is picked up. |
| 5 | Textual is a core dependency from P1, not an optional extra (§15 A). |
| 6 | Bare `pepper` on a TTY opens the TUI from P4, not earlier (§15 B). |
| 7 | Mentions: web parity through P3; P4 adds `@` on a sidebar row whose unread activity mentions you (§15 C). |
| 8 | The Pepper row gets a session-local unread dot when a poll finds an unseen assistant message (§15 D). |
| 9 | No server additions for now; ship on polling and revisit `chat/history?since` after P1 is in daily use (§15 E). |
| 10 | The open room shows its 5 most recent topics plus "Show more", as on the web (§15 F). |

## 15. Questions (all decided 2026-10-16)

**A. How does Textual ship? (Coupled with B.)**

- (a) A core dependency in P1.
- (b) An optional extra, `vycari-pepper[tui]`, until P4, then core in the same PR that makes the TUI
  the default.
- (c) Optional forever. Bare `pepper` falls back to the REPL with a one-line "install
  `vycari-pepper[tui]` for the full-screen view" hint.

**Decided (a), 2026-10-16.** Textual is a core dependency from P1. The TUI will be complete before
the next CLI release, so no released version would carry an extra that only half the feature uses.
The lazy import (§8) still keeps the ~180 ms off every non-TUI command.

**B. When does bare `pepper` open the TUI?**

- (a) In P4, after the parity work.
- (b) Already in P2, once you can post.
- (c) Never: the TUI is only ever `pepper tui`.

**Decided (a), 2026-10-16.** Until P3, the REPL does things the TUI can't yet, such as long DM
sessions with `/skill` commands. P4 flips the default; since the TUI ships complete before the next
CLI release, released users see the TUI default arrive with the TUI itself. `PEPPER_NO_TUI=1` and
`pepper chat` remain the way out.

**C. Should mentions get more than the web's treatment?**

- (a) Web parity: a highlighted row in the open topic, and nothing extra elsewhere.
- (b) As in (a), plus `@` in place of `●` on a sidebar row whose unread activity mentions you. This
  costs one message fetch per newly-unread topic (P4; since read-state-sync phase 5c the
  server's `has_unread_mention` flag makes it free).
- (c) As in (b), plus the terminal bell when a mention arrives.

**Decided (a) for P1–P3, and (b) in P4, 2026-10-16.** A terminal user can't see a phone
notification in the corner of the screen the way a web user can. The extra fetch is bounded: a topic
is fetched only when it flips to unread, so it costs at most one per new activity. The bell (c) is
opt-in only.

**D. Does the Pepper row get an unread dot?**

- (a) No, matching the web.
- (b) Yes, when a history poll finds an assistant row newer than the last one shown. That covers
  scheduled-task announcements and phone replies.

**Decided (b), TUI-only and session-local, 2026-10-16.** Without it, proactive messages (morning
brief, scheduled tasks) go unnoticed in the terminal. There is no server mark to agree with, so
nothing can drift.

**E. Ask the server for `chat/history?since` or a person event stream now?**

- (a) Neither. Ship on polling.
- (b) File `chat/history?since` now. Leave the stream for later.
- (c) File both now.

**Decided (a) for now, 2026-10-16; revisit (b) after P1 is in daily use.** The full-day history is
small, the poll interval matches the web's, and the request has to be measured before it is worth a
server change.

**F. How many topics show under the open room?**

- (a) The web's 5 most recent, plus "Show more".
- (b) All of them.

**Decided (a), 2026-10-16.** It is web parity, and it keeps the sidebar usable in a 24-row terminal.
`Ctrl+K` reaches any topic by name.
