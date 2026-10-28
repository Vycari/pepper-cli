# Changelog

All notable changes to `pepper` (distribution `vycari-pepper`). Newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/) (while 0.x, a minor bump may change flags or output).

## Unreleased

The terminal UI no longer decides what is unread: it shows the server's `unread` rows
(read-state-sync phase 5c, tracked by Vycari/groups#274).

Upgrade notes:

- A fresh profile now matches the web: unread markers are what the server counts, with your own
  messages and anything from before you joined left out. Before, the first poll marked every topic
  read as of its last activity.
- A server without `unread` rows on `GET /api/v1/groups/{gid}/read` (older than phase 5b) shows no
  unread markers at all, where the local rule used to light them.

### Changed

- Unread dots and `@` mention markers come from the server's `unread` rows (`unread_count`,
  `has_unread_mention`). The mention marker no longer reads a topic's newest page of messages to look
  for you. The local read marks are a cache of the server's answer: `read_state.advanced` and the
  poll fallback still update them, and a mark that passes a topic's newest activity clears its row
  until the server's next answer (#145).

### Removed

- `ReadMarks.seed_first_sight`, the local "activity later than the mark" unread rule, the
  open-topic exception that went with it, and the per-topic mention fetch (`Mentions`) (#145).

## 0.6.0 — 2026-10-26

The CLI now declares how it renders replies, and the terminal UI follows the person event stream so
read state and new messages arrive without waiting for a poll. Every PR merged since 0.5.0 is listed
below.

Upgrade notes:

- Piped or redirected `pepper chat` turns now send a `capabilities` object asking for plain text.
  Once the server's `client_capabilities_enabled` flag is on, piped output is converted to plain text
  by the server; before that, nothing changes. Old servers ignore the field.
- Existing logins have no stored declaration until the next `pepper login`; piped turns still
  declare per turn, and a terminal gets Markdown, the server default.
- The TUI marks a topic read only once its newest row has been on screen, and never while the thread
  is scrolled up or the terminal is blurred.

### Added

- Client capabilities (client-capabilities phase 4): sign-in stores `md:pepper/1` with
  `PUT /api/v1/client/capabilities`, and a piped `chat_send` or `chat_send_stream` declares
  `text:plain/1` for that turn. A 404 or 403 from the PUT is silent and never fails a login (#129,
  fixes #128).
- The terminal UI follows `GET /api/v1/groups/events/stream`: a read on another device clears its
  marker at once, and a new message fetches the rail and open topic immediately. Polling stays as the
  fallback and runs at its normal rate whenever the stream is down (#133, fixes #131; read-state sync
  phase 4, Vycari/groups#274).

### Changed

- The TUI marks a topic read only once its newest row has been on screen, and nothing is marked while
  the thread is scrolled up (#133). Opening at the read cursor and progressive marking are tracked in
  #132.

### Fixed

- The TUI ignores background results once shutdown has begun, which fixes the Windows CI flake where
  teardown failed with `NoMatches('#header')` (#136, fixes #76).

### Internal

- A TUI test waits on a recorded notice instead of a toast that expires after 2 seconds (#125, fixes
  #124).

## 0.5.0 — 2026-10-24

`pepper agents claude-channel`: a room agent can now run inside a Claude Code session and have the
room's messages pushed to it, instead of polling with `rooms watch` under Monitor. Room messages
composed in Pepper are labelled, and a room research schedule shows its topic.
Every PR merged since 0.4.0 is listed below.

Upgrade notes:

- For now `claude-channel` needs an interactive Claude Code session started with
  `--dangerously-load-development-channels server:<name>`, where `<name>` is the server's name in
  `.mcp.json`. Claude Code ignores that flag under `claude -p`. Later (P3 in the design), a Team or
  Enterprise admin can allowlist a plugin that carries it, and the dev flag is no longer needed.
  Whether an allowlisted plugin loads under `-p` is untested.
- It negotiates MCP `2025-11-25`, and never `2026-07-28`.
- `mcp>=2.3,<3` is now a core dependency of `vycari-pepper`. Only `claude-channel` imports it, so no
  other command pays for the import, but it adds the SDK's dependencies to the install.
- A reply's default idempotency key is now `claude-channel:<reply_to>:<digest>`, where the digest
  covers the room, topic, parent, text, mentions and `on_behalf_of`. A retry of the same reply is
  deduplicated by the server. A different reply to the same parent posts. An identical reply to the
  same parent is deduplicated, so pass your own `Idempotency-Key` to repeat yourself on purpose.
- `(via Pepper)` on composed messages needs the server to send the field (pepper#5261); until then
  nothing changes on screen. Showing a room schedule's topic and skipping an unchanged edit need
  pepper#5287 on the server. An older server leaves those out, and `edit` falls back to the 0.4.0
  check.

### Added

**Claude Code channel** (`planning/claude-code-channel.md`, tracking #112; designed in #113):

- `pepper agents claude-channel [--room R] [--topic T] [--cursor-file PATH] [--kinds K,…]
  [--ignore-kinds K,…] [--self PERSON_ID]`: an MCP server over stdio that Claude Code starts from an
  `.mcp.json` entry. It follows the event stream of the agent whose `pep_agt_` token you are signed
  in with, and pushes each new message from someone else, with its body, into the session as a
  `claude/channel` event. The flags are `rooms watch`'s, with the same meaning, and there is no
  `--json` or `--since`: stdout carries the protocol and nothing else (#115).
- Tools `messages_post`, `messages_list` and `messages_react`, limited to the room you named with
  `--room`. A post that contains a credential is refused without echoing it (#115).
- The cursor file moves only after an event has been pushed, so a restart resumes where the session
  left off. `backlog`, `resync` and `ended` notices tell the agent what it missed (#115).
- The bundled agent guide (`pepper agents guide`) and README describe the command (#115).

**Rooms:**

- Room messages composed in Pepper show `(via Pepper)` in `messages list` and the TUI, including on
  quoted parents. Bridged output is unchanged (#116).
- `research-projects show --room` names the topic a schedule posts to, or says `room default`
  (#111).

### Changed

- `research-projects edit --cron … --topic T` in a room says "unchanged" and sends nothing when the
  cron, zone and topic already match (#111).

### Fixed

- Fixes to `claude-channel` from the review of #115 (#119):
  - Two different replies to one parent message both post. Before, the second was deduplicated into
    the first (#117).
  - A message whose body keeps failing with a 5xx is pushed as `body="unavailable"` after five
    attempts, and the stream goes on, instead of stalling (#118).
- Fixes to `claude-channel` from its first live run (#121, issue #120):
  - The `backlog` and `resync` notices carry the `room_id` that `messages_list` needs.
  - A first run no longer sends a spurious `resync` for old events in the server's retained window,
    and a `resync` that is sent names what caused it. The wording is safe for a first run.
  - Every message tag carries the agent's own `self_id`, from `--self` or from the scoped stream.

### Docs

- The design for `claude-channel`, `planning/claude-code-channel.md` (#113).

### Internal

- The vendored `/api/v1` contract snapshot was refreshed for the schedule topic (#111).

## 0.4.0 — 2026-10-23

Research projects come to the terminal: `research-projects` and `research-items`, for personal
projects and for rooms, covering reading, writing, running, schedules and steering. Todos gain next actions.
Every PR merged since 0.3.0 is listed below.

Upgrade notes:

- The new commands need a server with the research routes (pepper#5080, #5091, #5113) and the
  research flags on. Pending steering in `show` needs pepper#5175, and built-in room skills need
  pepper#5205. An older server leaves those fields out, and nothing shows.
- Project names must be lowercase words joined by single hyphens, at most 64 characters. The server
  enforces this (pepper#5203). `research-projects create --help` states the rule, and a refused name
  comes back with a suggested name.
- Research commands print the server's error sentence without the `Server returned N:` prefix
  (#99). Other commands are unchanged.

### Added

**Research projects** (`planning/research-projects.md`, tracking #85; designed in #86):

- `pepper research-projects list`, `show` (`--report` for the full report) and `runs`, and
  `pepper research-items list --project P` and `show` (#88).
- `research-projects create`, `edit` (brief and filters), `run` (`--wait` follows the run to the
  end), `steer`, `pause` and `resume`, and `research-items take` and `reconsider` (#90).
- Schedules: `edit --cron C [--timezone TZ]` turns a schedule on or changes it, after one y/N
  question that shows the cron and the zone. `edit --clear schedule` turns it off and keeps the run
  history (#93).
- `--room` on every `research-projects` and `research-items` command, plus `edit --topic` to choose
  where a room schedule posts. In a room, `create`, `run`, `steer` and changing the schedule ask y/N
  first; `--yes` answers it when there is no terminal (#96).
- `show` lists each pending steering note with its author and time. `runs` puts a queued or
  running manual run at the top of the first page (#101, #105).
- `skills list --room` and `skills share --room` handle built-in skills that a room owner has
  granted to the room (#105).

**Todos:**

- Todo next actions. `todos list` shows a "Next:" line for each todo, dimmed when Pepper wrote it,
  and shows Pepper's newer suggestion on a dim "Or:" line. `pepper next` starts each todo with its
  next action. `todos create` and `todos edit` can set or clear the next action, or accept or dismiss
  Pepper's suggestion (#94).

### Fixed

- Fixes from the research smoke test on 2026-10-22 (#99):
  - `show --report` no longer prints the report's metadata marker.
  - `pause` and `resume` on a project that is already in that state say so.
  - A numbered ref from a different project's list says which project that list was, and prints
    the command that lists this one.
  - In a room, `show`'s hints keep `--room`.
  - `run --wait` reports a single duration.
  - Turning on a room schedule names the topic it posts to.
  - `research-items list` without `--project` explains how to name the project.
- Fixes from the research retest on 2026-10-23 (#107):
  - `edit --cron` with an unchanged cron and zone says they are unchanged, also when `--topic` is
    given.
  - The "Grant it with:" hint is on its own line.
  - Built-in room skills are labelled "granted by <name>" and not "shared by".
  - Research and skills command hints are not wrapped mid-command.
- `pepper tui` in macOS Terminal.app: clicks land on the cell under the pointer, so the message bar
  can be clicked again (#82).

### Docs

- The bundled agent guide (`pepper agents guide`) lists `attachments download` as a command an
  agent token can run (#97).

### Internal

- The vendored `/api/v1` contract snapshot was refreshed (#81, #93, #105). The CLI core coverage plan
  is in `planning/` (#61). Dependency bumps: #83, #84.

## 0.3.0 — 2026-10-17

A full-screen terminal UI, which bare `pepper` now opens on a terminal; `pepper next` rebuilt from
the todo list; todo assignees; and new commands for the feed, what you follow, the morning brief,
artifacts and preferences. Every PR merged since 0.2.0 is listed below.

Upgrade notes:

- Scripts that parse `pepper next --json` or pass `--first` must change (see Breaking).
- Bare `pepper` on an interactive terminal opens the TUI instead of the REPL. Scripts and agents
  are unaffected: piped or redirected I/O still gets the REPL, and `PEPPER_NO_TUI=1` or
  `pepper chat` keeps the old behaviour on a terminal.
- New core dependency `textual>=8.2`, which raises the `rich` floor to `>=14.2`.

### Breaking

`pepper next` is rebuilt from the todo list (Vycari/pepper-cli#50, #62). Since pepper#4820 the
schedule plan's `plans` come back empty, so `next` showed an empty Do next, only email deadlines
under Due soon, a Later of 0, and ignored `--first`. Scripts that parse `pepper next --json` must be
updated.

The CLI makes no decisions here: it fetches and renders. It does no ranking, scoring or filtering by
urgency, so every client shows the same order. Ordering belongs to the server.

- `--first urgent|important|fast` is removed; argparse rejects it with exit 2. There are no plans to
  choose between any more.
- The sections changed: Now, Do next, Due soon and Later are now Now, Today's calendar, and Todos.
  Todos are listed in the order Pepper returns them: due date soonest first, undated last, then
  creation order. The top 10 show, and `--all` lists every todo (it used to list every Later item).
  Each line carries the server's own fields as labels: due date or time (tagged `overdue` once it
  is behind you, which never moves the todo), priority, and source when it is not `user`.
  Email-deadline cards from Planning are no longer shown, and todo deadlines and reminders are not
  turned into calendar rows.
- `--room` and `--source` narrow the todos only. `--room` now reads that room's own todo list
  (same server order), and `--source` is a case-insensitive prefix match on the todo's `source`
  field (`user` or a producer name), not a source label or room name.
- The `--json` shape is replaced. It was `{generated_at, source, now: [item], plan, do_next,
  due_soon, later}`. It is now `{generated_at, timezone, source: "todos", note, now,
  next_event, calendar: [event], todos: {count, items: [todo]}}`, where `timezone` is the account's
  IANA zone name (null when unreadable) and every time is shown in it. `now` is one event object or
  null (it was a list), `plan`, `do_next`, `due_soon` and `later` are gone, and `source` is always
  `"todos"` (it was `"schedule-plan"` or `"fallback"`). Each todo item is `{id, title, due,
  due_is_date_only, remind_at, priority, source}`, a subset of the server's todo fields with values
  unchanged (`due` is the server's `due_date`), in the server's order; the README has the details.

### Added

**Terminal UI** (`planning/terminal-ui.md`, tracking #54; built in #60, #64, #67, #69):

- `pepper tui`, a full-screen terminal client built on Textual. A sidebar holds the Pepper chat
  and your rooms and topics; the main pane shows the open conversation. Unread dots come from the
  server's read marks, opening a conversation marks it read, and new messages arrive by polling.
- Posting: room messages go out with an idempotency key, so a retry never double-posts. `@` mentions
  complete from the room roster, `r` on a selected message quote-replies (or retries a failed
  send), and each conversation keeps its own unsent draft.
- Pepper chat: turns stream into the pane the way the REPL streams them. `--root PATH` shares a
  folder with Pepper's turns, as `pepper chat --root` does. The REPL's streaming code moved into a
  shared module that both use; REPL output is unchanged.
- Messages: `+` reacts, `e` edits and `d` deletes your own messages (delete asks first), `o` opens
  a message's link, and moving up past the first message (or reaching the top) loads the page
  before it. Messages render Markdown, link previews and app cards.
- Navigation: Ctrl+N and Ctrl+P jump to the next and previous unread conversation, Ctrl+K opens a
  fuzzy jump to any room or topic, Ctrl+B hides the sidebar, Left and Right fold and unfold a room,
  `/` focuses the composer, `i` shows room info, and Ctrl+Q quits. A "new topic" row in each room
  creates a topic and opens it.
- A topic or collapsed room with unread activity that mentions you shows `@` instead of the dot,
  and the terminal title reads `@ Pepper`. `PEPPER_TUI_BELL=1` also rings the terminal bell when a
  mention is first found (off by default). This reads `person_id` from `/api/v1/whoami`
  (pepper#4868); against an older server the TUI learns it from your own messages instead.

**Todo assignees** (streams-to-pepper-room P2b, Vycari/vycari#143; #75):

- `pepper todos create --room R --assignee PERSON` asks a room member. The name resolves through the
  room roster, the same matcher as `--mention`.
- `pepper todos list --mine` (what was asked of you) and `--asked` (what you asked, needs `--room`).
  Rows show who asked, who was asked and the ask's state.
- `pepper todos edit <id>` changes title, details, due, priority or assignee; `--clear` removes a
  field. A todo has no version token, so there is no `--if-match`; the server's 409 is shown with a
  pointer to `todos list`.
- `pepper todos dismiss --reason TEXT`: an optional reason, sent as given.

**Core coverage** (Vycari/pepper-cli#61; #71, #73). Each command fetches what the server returns and
renders it; none ranks, filters, orders or decides on this side.

- `pepper feed list [--limit N] [--group G] [--json]` and `pepper feed show N [--transcript]
  [--read [--full]] [--open] [--json]`: what is new in everything you follow, in the order the
  server's ranked `data.feed` gives (pepper#4852). `feed` is a noun like `following`, `todos` and
  `artifacts`. Rows carry a coloured type tag; `feed show N` shows one item in full (its summary,
  what the server folded under it, and the game block for a sports entry). `--transcript` uses the
  entry's `episode_id`; `--read` fetches the article's page from its own site and never sends the
  Pepper token. A server that does not rank yet gets one line saying so.
- `pepper feed show N --open` opens the item's link in your default browser and prints it. An item
  with no usable `http(s)` link exits 2; with no usable browser (any SSH session unless `BROWSER`
  is set, or Linux with no `DISPLAY`) it prints the link and a note, and exits 0. It works with
  `--json`.
- Item titles in `feed list` and `feed show` are terminal hyperlinks (OSC 8) to the item's link when
  stdout is a terminal; the plain link line stays for copy-paste, and pipes and `--json` carry no
  escape sequences.
- `pepper following list | show | search | create | delete`: manage what you follow. `create` takes
  only a number from the last `following search` (saved per profile): search first, even for a web
  address, then `following create 1`. It never picks a match for typed text.
- `pepper brief [--date YYYY-MM-DD] [--now] [--json]`: your morning brief, readably. The Brief
  document already lists editions newest first, so the first is the latest and `--date` is a lookup
  by the edition's `date`; `--now` runs `brief.run_now`.
- `pepper artifacts list | show | download | create | delete`: notes, links and files. `list`
  numbers its rows (`show 2`, `download 2`, `delete 2`) and sends `--kind`, `--label`, `--search`
  and `--limit` to the server as query parameters.
- `pepper preferences show | edit`: the Calendar > Preferences values (timezone, addresses, work
  days and hours, meals, workouts, weather switches). `edit` sends only the fields given; the
  server checks the values.

**Other:**

- `PEPPER_CONFLICT_DIR`: when `pepper notes edit` hits a 412 conflict, the `.mine.md` copy of your
  text is written to this directory (created if missing) instead of the current directory. Unset
  or blank behaves as before (#59, Vycari/pepper-cli#56).

### Changed

- Bare `pepper` opens the TUI when stdin and stdout are both a terminal. It still opens the REPL
  when either is piped or redirected, when `TERM=dumb`, or when `PEPPER_NO_TUI=1`; `pepper chat`
  is the explicit way to the REPL. `pepper <message>`, every other command, and the exit codes
  and stdout of non-interactive runs are unchanged (#69).
- `textual>=8.2` is now a core dependency, and the `rich` floor rises to `>=14.2` because Textual
  needs it. Textual is imported only when the TUI starts, so other commands do not pay its
  ~180 ms import (#60).
- `pepper rooms create` now requires `--name`. Without one (or with a blank one) it fails locally,
  exit 2, with a message saying so, and sends nothing. The service answers an unnamed create with
  422 once groups phase 3 lands; this reports it first (#52, groups simplification
  vycari/groups#249 phase 5).
- `pepper rooms show` labels a room whose wire `kind` is `sealed` as `email/text thread`
  (it was `room`). The CLI still parses `sealed`; only the wording changed. The apps list these
  under "Other conversations" (#52).
- Help and README no longer describe an email or text thread as a "sealed room"; no flag or JSON
  field changed (#52).
- `pepper skills share --room` explains a 409 with Pepper's own wording: "Sharing a skill into this
  room isn't allowed. A skill can only be shared into a named channel." Email and text threads are
  refused by design, so the message no longer says "yet" (#58, Vycari/pepper-cli#55).
- `pepper messages list --help` now says that omitting `--topic` picks the most recently active
  unarchived topic (or the most recent topic when all are archived), not "the room's default
  topic". Behaviour is unchanged (#66).
- The vendored `/api/v1` contract is refreshed to pepper f48317b, which adds `person_id` to
  `whoami` (#69, #75).

### Fixed

- `pepper feed show N --read` follows redirects itself: a redirect response's body is never read (it
  used to be read in full, outside the 2 MB cap and the 45 s deadline), at most 5 hops are
  followed, each hop is checked for a public address, and the cap and deadline cover the whole
  fetch (#72).
- `pepper feed show N --transcript` says "the server returned no transcript for this episode" when
  the server's answer is empty, instead of labelling it "no transcript yet" (#72).
- `pepper following list` and `pepper feed list` order groups by the server's `counts.by_group` key
  order instead of a built-in list; a group absent from it comes last, and an older server with no
  `by_group` gives the order the items first appear in. `--group` checks against those names (#72).
- Windows: when `pepper notes edit` cannot write its conflict copy in the working directory, the
  fallback now goes to `%LOCALAPPDATA%\pepper` (a per-user folder) instead of `%TEMP%`, which can
  point at a shared folder. Windows ignores the file's `0o600` mode, so the folder's inherited ACL
  is what keeps the copy private; no ACL is set explicitly (#48).

### Internal

- CI runs the full test suite on `windows-latest` as well as Linux (#44), and the zone-rendering
  tests pin the zone through a seam in `render.py` instead of `time.tzset`, so they run on Windows
  too (#47). The FIFO test stays POSIX-only on purpose.
- The TUI has snapshot tests via the new dev dependency `pytest-textual-snapshot` (#67).

## 0.2.0 — 2026-10-15

One grammar for the whole CLI: `pepper <noun> <verb> [--room ROOM [--topic TOPIC]]`. Commands are
named for the product's own nouns (`rooms`, `topics`, `messages`, `members`, `apps`, ...) instead of
the old `groups` bag, every leaf takes `--json` (or set `PEPPER_JSON=1`), and every list emits one
envelope, `{"items": [...], "next_before": ...}`. The conformance test in
`tests/test_cli_conformance.py` holds the grammar in place, and both `JSON_PENDING` and
`ROOM_PENDING` are empty: every leaf has `--json`, and every room-scoped verb takes `--room`.
Design: `planning/cli-information-architecture.md`.

### Breaking

This is a hard break with no aliases (the CLI is pre-1.0 and its main users are agents whose skills
move in lockstep). Old verbs and flags under a surviving noun fail with argparse's usual
`invalid choice` / `unrecognized arguments` (exit 2), and the message lists the new verbs. The old
nouns `groups`, `trajectory` and `agent-skill` stay registered as hidden stubs: they run nothing,
print one line pointing at the new name, and exit 2, so a stale agent never sends
`pepper groups read ...` to Pepper as a chat message.

Saved agent allow rules that match on prefixes such as `Bash(pepper groups post <id>:*)` or
`groups watch:*` match nothing after the upgrade. Rewrite them, or pin `vycari-pepper<0.2` until
they are.

`--room` on `next` changed meaning: it used to be a name prefix filter, and now resolves strictly
to exactly one room (a UUID or a unique case-insensitive name prefix), like `--room` everywhere
else. The old prefix filter is `--source PREFIX`: `next --room PREFIX` becomes
`next --source PREFIX`.

Old to new, every command that changed:

| 0.1.0 | 0.2.0 |
|---|---|
| `groups list [--archived]` | `rooms list [--archived]` |
| `groups show GROUP` | `rooms show ROOM` |
| `groups new --name PEOPLE` | `rooms create [--name N] PEOPLE...` |
| `groups rename GROUP NAME` | `rooms edit ROOM --name NAME` |
| `groups archive/unarchive GROUP` | `rooms archive/unarchive ROOM` |
| `groups leave GROUP --yes` | `rooms leave ROOM --yes` |
| `groups mute/unmute GROUP` | `rooms mute/unmute ROOM` |
| `groups watch --room --topic` | `rooms watch [--room R]... [--topic T]` |
| `groups activity [GROUP] --since` | `rooms activity [--room R] [--since]` |
| `groups topics GROUP` | `topics list --room R [--archived]` |
| `groups new-topic GROUP NAME` | `topics create --room R NAME` |
| `groups rename-topic GROUP TOPIC NAME` | `topics edit --room R TOPIC --name NAME` |
| `groups archive-topic/unarchive-topic GROUP TOPIC` | `topics archive/unarchive --room R TOPIC` |
| `groups search QUERY` | `topics search [--room R] QUERY` |
| `groups read GROUP [TOPIC]` | `messages list --room R [--topic T] [--limit --before --since --mark]` |
| `groups post GROUP TOPIC [MSG\|-]` | `messages post --room R --topic T [MSG\|-]` |
| `groups edit GROUP MESSAGE TEXT` | `messages edit --room R MESSAGE [TEXT\|-]` |
| `groups delete-message GROUP MESSAGE` | `messages delete --room R MESSAGE --yes` |
| `groups react/unreact GROUP MESSAGE EMOJI` | `messages react/unreact --room R MESSAGE EMOJI` |
| `groups download GROUP ATTACHMENT` | `attachments download --room R ATTACHMENT [--out]` |
| `groups roster GROUP` | `members list --room R` |
| `groups invite GROUP HANDLE` | `members invite --room R HANDLE` |
| `groups remove GROUP PERSON` | `members remove --room R PERSON --yes` |
| `groups role GROUP PERSON ROLE` | `members edit --room R PERSON --role owner\|member --yes` |
| `groups apps GROUP` | `apps list --room R` |
| `groups install-app GROUP KEY` | `apps install --room R KEY` |
| `groups uninstall-app GROUP APP` | `apps uninstall --room R APP --yes` |
| `groups sharing GROUP` | `sharing show --room R` |
| `groups sharing GROUP --set CAL=LEVEL` | `sharing edit --room R --set CAL=LEVEL...` |
| `trajectory show ID` | `trajectories show ID` |
| `trajectory list --limit --offset` | `trajectories list --limit --before` (`--offset` removed) |
| `todos add TITLE` | `todos create TITLE` |
| `scheduled-tasks add NAME` | `scheduled-tasks create NAME` |
| `scheduled-tasks add/destinations --topic NAME` | `--deliver-topic NAME` (`--topic` removed on this noun) |
| `scheduled-tasks destinations TASK` | `scheduled-tasks edit TASK --to/--default/--silent` |
| `skills new NAME` | `skills create NAME [--file]` |
| `notes new NAME` | `notes create NAME` |
| `notes rm NOTE` | `notes delete NOTE --yes` |
| `notes edit --rename` | `notes edit --name` |
| `agent-skill [--write DIR]` | `agents guide [--write DIR]` |
| `next --room PREFIX` | `next [--room R] [--source PREFIX]` (see above) |

Unchanged: `todos list/done/dismiss/reopen`, `scheduled-tasks list/show/runs/thread/edit/pause/resume`,
`skills list/show/edit/delete/share/revoke`, `notes list/search/show/edit`, `calendar`, `chat`,
`login`, `logout`, `whoami`, `takeout`.

List output also changed shape. Every `list`, `search` and `activity` leaf, plus
`scheduled-tasks runs/thread`, now emits `{"items": [...], "next_before": str|null}`, replacing the
0.1.0 shapes (the `groups` key of `groups list`, the `messages` key of `groups read`, and the bare
arrays of `todos list` and `groups topics/roster/apps/activity/search`). `next_before` is always
present, `null` on the last page and on unpaged lists. List-level metadata the server returns sits
beside them as extra top-level keys (`rooms list`: `enabled`, `degraded`, `total`,
`archived_total`; `notes list/search`: `truncated`). `next` and `calendar` are views, not lists,
and keep their 0.1.0 object shapes.

### Added

- `--room ROOM` on room-scoped verbs across `notes`, `skills`, `scheduled-tasks` (including
  `scheduled-tasks delete`), `trajectories`, `apps` and `calendar --planning`. It takes a UUID or
  a unique case-insensitive name prefix, and resolves strictly.
- `--json` on every leaf, and `PEPPER_JSON=1` as the environment switch.
- Pagination: `--before CURSOR` and `--limit` on `trajectories list` and `scheduled-tasks runs`,
  with `next_before` in the envelope and a next-page hint in text output.
- `apps show SLUG [--room R]` (an app's data document) and `apps run SLUG [ACTION] [--room R]
  [--param k=v]...` (with no ACTION, lists the declared actions).
- `trajectories list/show`, replacing `trajectory`, with `--room` to scope to a room's
  trajectories.
- `scheduled-tasks renew TASK [--room R]`, which prints the server's reason when it refuses.
- `scheduled-tasks delete TASK [--room R] --yes`.
- `agents guide [--write DIR]`, the bundled agent skill under its new name.
- `skills create` / `skills edit --file PATH`, and `scheduled-tasks edit --to/--default/--silent`
  in place of `destinations`.

## 0.1.0

Initial release.
