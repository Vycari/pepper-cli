# CLI core coverage — every core Pepper task from the terminal

**Status:** Proposed. Chris decided the items in §10 on 2026-10-16, and revised them on 2026-10-17
to follow Allen's review: no business logic in the CLI (§6), `feed` ranked on the server and kept a
view (§4.3), `next` presentation only (§5). The questions in §11 are open. Nothing here is built on
`main` yet: the commands below were prototyped on a local integration branch (`proto`) and land as
the PRs in §9.
**Author:** drafted with Claude Code for Chris
**Last updated:** 2026-10-17
**Tracked by:** not filed
**Related:** `planning/cli-information-architecture.md` (the grammar this extends; its §2 rows and
§3.1 verbs change in the PRs below, not here), pepper-cli#50 (`pepper next` after pepper#4820),
pepper-cli#62 (the `next` fix), pepper#4847 (merged; RSS `summary` and `published_at` on Following recent
items), vycari#142 (`docs/design/streams-to-pepper-room.md`, todo assignment), pepper
`planning/requests.md` (the Requests primitive), pepper `planning/dash.md`, pepper#4802 (the
Dash/Inbox rename). Server paths below are relative to `src/pepper/` in Vycari/pepper; CLI paths
are relative to `src/pepper_cli/`.

## 1. Goal and working rule

Chris's goal (2026-10-16): **"A CLI which can functionally do every core task Pepper does."**

Three things follow from it, and the rest of this doc applies them:

1. **Core tasks, not every screen.** A core task is something a person does with Pepper most days:
   see what to do now, read and change the calendar, decide what is waiting on them, keep todos,
   read what they follow, talk to rooms and to Pepper, find what they saved. Small features stay
   reachable through chat or through the generic `pepper apps show/run` (§7).
2. **New product ideas are prototyped on the CLI first.** A command is cheaper to build, test and
   throw away than a web or iOS screen, so an idea gets a CLI shape before it gets a screen. The
   `feed` (§4.3) and the rework of `next` (§5) were both worked out this way. What a prototype
   settles about ranking or filtering then moves to the server, because the CLI carries no
   business logic (§6).
3. **Agents are the CLI's main users.** Every leaf keeps `--json`, every list keeps the
   `{"items": [...], "next_before": ...}` envelope, and writes take `--yes` off a terminal
   (`cli-information-architecture.md` §3.1, §5). A question an agent asks often gets a command it
   can run without hunting for a verb, which is why `feed` stays a view (§4.3). That agents run
   with `--yes` is also why the email rule in §6 exists: the CLI itself has to refuse what an agent
   must never do.

## 2. Where the CLI stood

A coverage audit on 2026-10-16 walked every product area against the server's `/api/v1` routes and
the owner-scoped app actions a PAT can run. Rechecked against `main` on 2026-10-17 (`pepper <noun>
--help` and the README), the CLI covers rooms, topics, messages (list, post with `--attach`, edit,
delete, react, unreact), attachments, members, sharing, apps, notes, todos (list, create, done,
dismiss, reopen, with `--room`), scheduled tasks, skills, trajectories, `calendar`, `next`, chat
(one-shot and the REPL) and `tui` (pepper-cli#60, a read-only view of the Pepper conversation and
rooms). The table lists what each core area has on `main` and what is still missing.

| Area | On `main` | Missing | Server today |
|---|---|---|---|
| Undecided (Dash) | nothing dedicated | seeing and acting on Dash cards | web-only `/apps/dash/data`; no PAT view or action route |
| Calendar | `calendar` (today, week, schedule, any window, `--planning`, `--room`), read-only | event show, create, edit, delete and RSVP | calendar app actions over the PAT action route |
| What's new | followed events in `calendar --planning`; the Following document and actions through `apps show/run following` | a readable stream of recent items, and managing follows by name | Following app document and actions |
| Todos | `todos list/create/done/dismiss/reopen`, personal or a room's (0.2.0) | `show` and `edit` only, plus asking someone else | no `GET`/`PATCH /api/v1/todos/{id}` |
| Brief | `apps show brief` (the raw document) | a readable edition | Brief app document, `brief.run_now` |
| Chat with Pepper | one-shot `chat` (a message and its reply), the REPL, `--root`, `--json`; `tui` reads the conversation | thumbs on a turn. Whether `chat` should take attachments or print history is §11 A | `/trajectories/{id}/feedback` |
| Room messages | `messages list/post/edit/delete/react/unreact`, `post --attach`, `attachments download`, `rooms watch` | nothing for core tasks | — |
| Saved things | `notes` touches text notes only | links and files | `/artifacts*` for every kind |
| People, preferences, booking | nothing | all of it | `/people/*`, `/calendars/preferences`, booking app actions |
| `next` | present, but broken by pepper#4820: plans are always empty (pepper-cli#50) | a working view | `schedule-plan` keeps answering with no plans |

## 3. The intent map (removed)

An "intent map" (each question a person has, next to the command that answers it) led
`pepper --help` for a short while: it was approved on 2026-10-16 and shipped in pepper-cli#71.
It is removed again in pepper-cli#72 (decision 15). Agents, the CLI's main users, read the plain
command list, and each command's one-line help already says what it answers. The map mostly
restated those lines and pushed the list further down. The intent thinking stays useful for
deciding what a command is for; it does not belong in `--help`.

## 4. New nouns and verbs

### 4.1 The table

"Works today" means against production as it is, through existing PAT routes or app actions run
through `POST /api/v1/apps/{slug}/actions/{name}`. "Needs" names the server change it waits on
(§8).

| Command | What it does | Server dependency |
|---|---|---|
| `dash list` / `show N` | The open Dash cards in the server's order, numbered (`--limit`); one card in full with its context, options, links and draft bodies | Needs `GET /api/v1/apps/dash/view` (pepper branch `inbox-pat-actions`), the server's filtered list. The CLI keeps no copy of the filters (§6) |
| `dash do N OPTION` / `dismiss N` / `snooze N` / `undo` | Take one of a card's options exactly as its web button does; decline a card; hide it until later; reverse the last action | Needs `POST /api/v1/apps/dash/actions/{name}` and `GET /api/v1/apps/dash/view` (pepper branch `inbox-pat-actions`) |
| `feed list` | The server's ranked stream of the recent items of everything followed, numbered (`--limit`) | Needs `data.feed` in the Following app data (pepper#4852) (§4.3) |
| `feed show N` | One item in full (`--open` opens it in a browser; titles are clickable links in a terminal): source, sports or episode detail, the RSS summary. `--transcript` for a podcast episode, `--read` (with `--full`) fetches the article from its own site | `--transcript` uses the Following app's `episode_transcript` action; `--read` is a plain client-side GET that never carries the Pepper token |
| `following list` / `show` / `search` / `create` / `delete` | What the person follows, by group; one followed thing; find something to follow (team, show, podcast, website address); follow number N from the last search; unfollow | Works today over the Following app's owner-scoped actions |
| `events show` / `create` / `edit` / `delete` / `rsvp` | One event in full; add, change or delete an event on the primary calendar; reply yes, no or maybe | Works today over the calendar app's actions. **Writes hit the person's real Google Calendar.** `rsvp` has no note (§6) |
| `brief [--date D] [--now]` | The latest brief edition, readably; `--date` an older one; `--now` runs a fresh one (it arrives where the brief is delivered) | Works today: Brief app document, `brief.run_now` |
| REPL `/good`, `/bad` | Thumbs on the last turn | Works today: `/trajectories/{id}/feedback` |
| `trajectories feedback ID up\|down\|clear [--note]` | Thumbs on any turn, or take the thumb back | Works today: `POST`/`DELETE /api/v1/trajectories/{id}/feedback` |
| `artifacts list` / `show` / `download` / `create` / `delete` | Every kind of saved thing (note, link, file): find, read, fetch the bytes, upload a file, delete | Works today: `GET /api/v1/artifacts`, `/{id}/detail`, `/{id}/raw`, `POST /upload`, the artifacts app's `delete` action |
| `people show NAME\|EMAIL` / `search QUERY` | What Pepper can say about a person (contact match, prior meeting notes); search saved contacts | Works today: `/people/context`, `/people/contacts/search`. Read-only; contacts only where Google Contacts is enabled |
| `preferences show` / `edit` | Timezone plus the Calendar > Preferences values (addresses, work days and hours, meals, workouts, weather switches) | Works today: `GET`/`POST /api/v1/calendars/preferences` |
| `booking-pages list` / `show` / `create` / `edit` / `pause` / `resume` / `delete` | The links people use to book time: list with their links, one in full, create on the booking defaults, change, take offline and back, delete | Works today over the booking app's actions, except the public link, which the CLI shows only when the server sends it (§8) |
| `todos show N` / `edit N` | One todo in full; change title, details, due or priority in place | Needs `GET`/`PATCH /api/v1/todos/{id}` (pepper branch `todo-pat-writes`) |
| `todos create --room R --assignee PERSON`, `list --mine` / `--asked`, `edit --assignee`, `dismiss --reason`, `assign N --to PERSON [--room R]` | Ask someone in a room to do something, see what is asked of you and what you asked (§4.4) | Needs vycari#142 phase P1a in the server |

Commands that print a list a later command can point into are numbered (§4.2). Destructive verbs
and anything that reaches another person follow the prompt / `--yes` rule (§6).

`chat --attach` and `chat --history` were on this list. They are now §11 A: `pepper chat` is a
one-shot message to Pepper and its reply, and room `messages` already covers reading a thread and
attaching files.

### 4.2 Numbering

`todos list`, `calendar`, `dash list`, `feed`, `artifacts list` and `booking-pages list` print a
number on each row, and later commands accept that number in place of an id: `dash do 3 o1`,
`events rsvp 2 yes`, `feed 5`. The number is the row's place in the list as printed, in the
server's order; the CLI does not reorder. The last listing of each noun is saved per profile, next
to the credentials file (`credentials.credentials_path()`), one small JSON file per noun, so two
profiles never see each other's numbers. A reference longer than four digits, or not all digits, is
an id or a unique id prefix. On `proto`, `numbered_view.py` holds the shared mechanism for
`artifacts` and `booking-pages`; `todo_numbers.py`, `events_view.py` and `dash_view.py` each keep
their own copy of the same idea.

### 4.3 `feed`

`feed` is a **noun** with `list` and `show`, like `dash`, `todos`, `following` and `artifacts`
(pepper-cli#73, decision 16): `pepper feed list` lists the recent items of everything the person
follows as one numbered stream, and `pepper feed show N [--read] [--transcript] [--open]` shows
item N in full. It was briefly a view (`pepper feed`, `pepper feed N`, shipped in #71 but never
released), based on a misreading of Allen's review comment, which was about the intent map, not
the feed's verbs. The same `list`/`show N` shape on every stream is the more predictable one for an
agent, and leaves room for item actions. `following` stays the noun for changing what is followed.

**The server ranks.** The order, the folding and the scoring belong to the Following app on the
server, so the stream is the same on web, iOS and the CLI. pepper#4852
(which adds the RSS `summary` and `published_at` to recent items) adds a ranked `data.feed` to the
Following app data. The CLI reads `data.feed` and prints it in the order given. `feed.py`'s
`Weights`, the client-side scoring and folding, and the `--why` score breakdown on `proto` leave the
CLI (§6). The rules worked out on `proto`, offered as input to the server ranking:

- **One entry per team**, and at most two per site or show. The rest fold under it ("+N more").
- **Results count as news.** A Sports final gets the same boost as a new article.
- No long runs from one source or one group.
- Recency halves every 36 hours; upcoming events further out than a week drop out, and those
  inside a day ramp up; a source that is live right now leads.

Each row gets a **coloured type tag** (sports, podcast, article, show, event). That is
presentation, from the item's type as the server sends it.

`feed show N --read` fetches the article's page from its own site and prints its readable text
(`article_reader.py`). It is a plain third-party GET with its own HTTP client, a timeout, a redirect
cap and a byte cap, and it **never sends the Pepper token, cookies or any other credential** to the
third party.

### 4.4 Todo assignment

Assignment follows vycari#142 (`docs/design/streams-to-pepper-room.md`, accepted by Allen
2026-10-16), which Allen builds in the server as phase P1a. An assigned room todo is a Requests
`complete` request, with the assignee's own todo and a display copy on the room's list. The CLI
side is that doc's §5:

- `todos create --room R --assignee PERSON` asks a member of room R. `PERSON` is a name (unique
  prefix) or roster person id, resolved as `--mention` is; the CLI sends `assignee_person_id` and
  never a user id.
- `todos list --mine` is what is asked of you (every room without `--room`, that room's copies with
  it); `todos list --asked` is what you asked.
- `todos edit --assignee PERSON` reassigns (requester only).
- `todos dismiss --reason TEXT` declines an ask; the reason is required.

**Scope** (Chris, 2026-10-16): room-scoped. Anyone can be assigned, as long as it is in a room you
share. vycari#142 Q-E narrows the v1 server check to members of the todo's own room.

**`todos assign N --to PERSON [--room R]`** is a CLI-only handoff Chris asked for: take one of
your personal todos and give it to someone. It is two calls: create an assigned room todo with the
same title and details, then dismiss your personal one. It is **not atomic**, and the order is
deliberate: if the second call fails, the ask exists and your own todo is still on your list, so
the ask is never lost; the worst case is a duplicate you dismiss by hand.

The `assign`, `asked` and `withdraw` verbs on `proto` came from an earlier server design on the
`todo-pat-writes` branch (`/api/v1/todos/{id}/assign`, `/todos/asked`). vycari#142 supersedes them:
`asked` becomes `list --asked`, and withdraw becomes `dismiss` by the requester.

## 5. `pepper next`

`next` is rebuilt as presentation only. This fixes pepper-cli#50: since pepper#4820 the server's
`schedule-plan` returns no plans, so the old view showed an empty "Do next", hid the person's own
todos, and silently ignored `--first`. The fix is pepper-cli#62, revised after Allen's review so
that it carries no ranking of its own.

The view has three parts (`next_view.py`):

1. **Now.** The live event, and the next one.
2. **Today's calendar.** Today's events as the server returns them.
3. **Todos, in the server's order.** The open todo list as `GET /api/v1/todos` returns it, with no
   client-side ranking, scoring or reordering. **Todos are never put into time slots.**

The ranking prototyped on `proto` (overdue, then due today, then importance, then due soon; a
due-date boost that decays with distance; a 24-hour grace for passed timed deadlines and reminders)
does not ship in the CLI (§6).

**Direction: a server-side ranked "what should I do now" list.** The ranked list belongs on the
server, with the Todos redesign, so that web, iOS and the CLI show the same order. When the server
returns it, `next` prints it as given; the prototype's rules above are input to that design, not a
CLI feature.

**`--first` is removed.** It chose a packing strategy for the old time-slot fitter, which no longer
exists. This is a breaking change: `next --first ...` exits 2 with argparse's usual error. It is
listed under Breaking in the CHANGELOG (§9).

## 6. Rules

- **No business logic in the CLI** (Allen's review; Chris adopted it on 2026-10-17). The server
  ranks, filters, decides and owns ordering; the CLI fetches what the server returns and renders
  it. Numbering a printed list, truncation, colour and formatting are presentation and belong in
  the CLI. Allen's two reasons: the CLI is open source, so business logic in it gives the IP away;
  and logic in one client makes the experience differ between apps, or forces every app (web, iOS,
  CLI) to rebuild it. When the CLI needs an order, a filter or a decision the server does not give,
  the answer is a server change (§8), not client code. The table after this list is what `proto`
  loses under this rule.
- **The CLI never sends email as the person** (Chris, 2026-10-16). Agents drive the CLI with
  `--yes`, so a confirmation prompt is not a safeguard here; the CLI has to refuse. So:
  - `dash do` refuses an option whose draft is an email, and says to send it from the web or iOS
    Dash. A drafted room message is allowed.
  - There is no `drafts send`.
  - `events rsvp` has no note: a note is an email to the organizer.
  - Accepted: the invitations, updates and cancellations Google sends to guests because of
    `events create/edit --guests` or `events delete`, and the ask that `todos assign` delivers in
    Pepper. These are notices from the system, not mail written in the person's voice.
- **Destructive verbs prompt on a terminal, and need `--yes` otherwise** (exit 2 without it).
  This extends `cli-information-architecture.md` §3.1 to the new verbs: `events delete`,
  `booking-pages delete`, `artifacts delete`, `following delete`. Commands that reach another
  person follow the same rule though nothing is destroyed: `events create/edit` with guests,
  `todos assign`. `dash do` and `dash dismiss` do too, because a mistyped card number would act on
  the wrong card.
- **No hand edits to the contract snapshot.** `contracts/api-v1.openapi.json` is refreshed by the
  sync workflow only (`CLAUDE.md`).
- **Server first, then the snapshot refresh, then the CLI.** `tests/test_api_contract.py` checks
  every `# MIRROR:` field against the snapshot, so a CLI PR that mirrors a new server field merges
  only after the server change is deployed and synced. On `proto` the snapshot carries the
  unmerged server branches' routes; those hunks are dropped when each PR is cut from `main`.

Client-side logic on `proto` removed under the no-business-logic rule:

| On `proto` | Where it goes |
|---|---|
| `feed.py`: `Weights`, scoring, folding, `--why` | The server's ranked `data.feed` (§4.3) |
| `feed.py` `FeedEntry.when`: a time parsed back out of a display label | `published_at` on recent items (pepper#4847) |
| `next_view.py`: todo ranking, due-date backoff, the 24-hour grace | The server's order now; a server-ranked list with the Todos redesign (§5) |
| `dash_view.py`: dropping switched-off sources and snoozed cards, the newest-first sort, the web filter-chip map behind `--source`, the `--kind` filter | `GET /api/v1/apps/dash/view`, already filtered (pepper branch `inbox-pat-actions`). `--kind`, `--source` and `--snoozed` stay only as parameters the view route takes |
| `brief_view.py`: sorting editions by date | The Brief document's own order |
| `booking_view.py`: building the public link, hiding deleted pages and cancelled bookings | The booking app's data (§8) |
| `following_search.best_match`: choosing which search result typed text means | `following create` takes a number from the last `search`; matching typed text is the server `search` action's job |

## 7. Rejected or removed

| What | Decision |
|---|---|
| Client-side ranking, filtering and ordering (`feed`, `next`, `dash` and the rest of the §6 table) | Removed (Chris, 2026-10-17, following Allen's review): no business logic in the CLI (§6) |
| `feed` as a view (`pepper feed`, `pepper feed N`) | Reverted (Chris, 2026-10-17): it came from misreading Allen's comment, which was about the intent map. `feed` is a noun with `list`/`show` (§4.3, #73) |
| The intent map at the top of `pepper --help` | Removed (Chris, 2026-10-17, following Allen's review): agents prefer the plain command list (§3) |
| `todos signal` / `todos signals`: local hints an agent could give `next` about a todo ("started", "blocked") | Removed (Chris, 2026-10-16): no usage yet |
| Fitting todos into time slots in `next` | Rejected: todos are never scheduled (§5). It matches pepper#4820, which stopped planning todos into the day |
| Dedicated commands for calls (Answering), shopping, meals, recipes, workouts, journal, voice notes and images | Rejected (Chris, 2026-10-16): small features, or `artifacts` covers them. They stay reachable through chat and `pepper apps show/run` |
| `drafts send` (send an email draft Pepper staged in chat) | Rejected: the CLI never sends email as the person (§6) |

## 8. Server changes

**No database change anywhere.** Every change below is a route or a field over storage that exists.

| Branch / item | What | Unblocks |
|---|---|---|
| pepper `inbox-pat-actions` | `GET /api/v1/apps/dash/view` and `POST /api/v1/apps/dash/actions/{name}`: the PAT twins of the web Dash view and action routes (`api/dash.py`, `features/dash/web.py`) | All of `dash`: `list/show` read the filtered view, `do/dismiss/snooze/undo` act |
| pepper#4847 | RSS `summary` and `published_at` on Following recent items | `feed show N` detail; no time parsed out of a display label |
| pepper#4852 | A server-ranked `data.feed` in the Following app data | `feed` (§4.3) |
| pepper `todo-pat-writes` | `GET`/`PATCH /api/v1/todos/{id}` | `todos show/edit`. A PR with only show and edit is ready to open on Chris's go |
| pepper `todo-pat-writes`, rest | assign, asked and withdraw routes | Superseded by vycari#142 P1a; not opened |
| vycari#142 P1a | `assignee_person_id` on room todo create, `?assignee=me` / `?requester=me` / `?asked_of_me=true`, decline and withdraw over dismiss, PATCH with `assignee_person_id` | todo assignment (§4.4) |
| Todos redesign (direction, §5) | A server-ranked "what should I do now" list | `next` printing a ranked list |
| Booking app data | `public_url` on booking pages, and only live pages and bookings in what the list reads | `booking-pages` shows the server's link and list without building or filtering them itself |

## 9. Phases

One PR per area, each cut fresh from `main` (not from `proto`), each with its own tests, README and
bundled `SKILL.md` changes, and a CHANGELOG `Unreleased` entry. Releases are separate `release:`
PRs. Phases without a dependency can run in parallel, kept file-disjoint where `cli.py` allows.

| Phase | PR | Contents | Depends on |
|---|---|---|---|
| 1 | Intent map | Shipped in pepper-cli#71, removed in pepper-cli#72 (§3) | — |
| 2 | `next` | §5, pepper-cli#62; refs pepper-cli#50. Listed under **Breaking** (`--first` removed) | — |
| 3 | Dash | `dash list/show/do/dismiss/snooze/undo` (§4.1) | `inbox-pat-actions` deployed, snapshot refreshed |
| 4 | Feed and following | `feed list`, `feed show N` (#71, reshaped in #73), `following list/show/search/create/delete`, `article_reader.py` | pepper#4847 and pepper#4852 deployed |
| 5 | Events | `events show/create/edit/delete/rsvp`, numbered `calendar` | — |
| 6 | Brief and feedback | `brief`, REPL `/good /bad`, `trajectories feedback`. `chat --attach/--history` and REPL `/attach` only if §11 A says so | — |
| 7 | Account and data | `artifacts`, `people`, `preferences`, `booking-pages`, `numbered_view.py` | Booking app data (§8) for the public link |
| 8 | Todos show and edit | `todos show/edit` | `todo-pat-writes` (show/edit only) deployed, snapshot refreshed |
| 9 | Todo assignment | §4.4 | vycari#142 P1a deployed, snapshot refreshed |

The IA doc's §2 rows and §3.1 verbs for each noun move in the PR that adds the noun, so the doc and
the code stay in step.

## 10. Decisions

**2026-10-16, by Chris:**

| | Decision |
|---|---|
| 1 | Goal: a CLI that can functionally do every core task Pepper does. New product ideas are prototyped on the CLI first. Agents are its main users (§1) |
| 2 | The intent map in §3 is the canonical wording for the top of `pepper --help` (superseded by 15) |
| 3 | The product goes back to the **Dash** name, so the noun is `dash`, not `inbox` |
| 4 | `artifacts` covers every kind (note, link, file). This replaces the IA doc's "never artifacts" rule; `notes` stays the surface for editing note text |
| 5 | `brief` joins the set of views; `--now` runs a fresh one, rather than a `brief run` verb |
| 6 | The CLI never sends email as the person (§6) |
| 7 | `next` never fits todos into time slots; `--first` goes (§5). Its client-side ranking is superseded by 13 |
| 8 | `todos signal/signals` removed; dedicated commands for small features and `drafts send` rejected (§7) |
| 9 | Todo assignment follows vycari#142; `todos assign` is a CLI-only handoff, create then dismiss (§4.4) |
| 10 | One PR per area, cut fresh from `main`, in the order of §9 |

**2026-10-17, Chris, following Allen's review of this doc:**

| | Decision |
|---|---|
| 11 | No business logic in the CLI: the server ranks, filters, decides and owns ordering; the CLI fetches and renders (§6) |
| 12 | Feed ranking and folding move to the server (`data.feed`, pepper#4852); `feed.py`'s `Weights` leave the CLI. `feed` was briefly a view; see 16 |
| 13 | `next` is presentation only: now and next event, today's calendar, todos in the server's order (pepper-cli#62). A server-ranked "what should I do now" list belongs with the Todos redesign (§5) |
| 14 | `dash` reads the server's filtered `GET /api/v1/apps/dash/view` and drops its copy of the filters (§4.1, §6) |
| 15 | The intent map is removed from `pepper --help` and the README (pepper-cli#72): agents prefer the plain command list (Allen's review), and Chris agreed it isn't useful (§3) |
| 16 | `feed` is a noun with `list` and `show` (pepper-cli#73), for consistency with the other streams; `--open` is a flag on `show`. The view shape came from misreading Allen's intent-map comment (§4.3) |

## 11. Open questions

**A. `chat --attach` and `chat --history`?** (for Chris) `pepper chat` is a one-shot message to
Pepper and its reply (Allen's review). Room `messages` already lists, posts with `--attach`, edits,
deletes and reacts, `attachments download` fetches files, and `pepper tui` reads the Pepper
conversation. Whether 1:1 chat needs its own attachments or history, and with them whether email
drafts Pepper staged in chat would show read-only in that history, is not decided. Neither is
planned until it is.

**B. Fold `notes` into `artifacts`?** `artifacts` lists, reads, fetches, uploads and deletes every
kind; `notes` lists, searches, shows, creates, edits and deletes text notes. The overlap is
everything but editing text. Options: keep both (today); make `notes` a thin alias over
`artifacts --kind note` plus `edit`; or move `edit` and `create --text` onto `artifacts` and retire
`notes`. The last is a breaking change for agents that use `notes`.

**C. `brief --now` or a `brief run` verb?** `--now` keeps `brief` a view (a view takes flags, not
verbs). A verb would make `brief` a noun with `show` and `run`.

**D. `people search` with no results.** The empty message does not tell "no match" apart from "this
account has no Contacts access". The server answer may not carry the difference.

**E. `todos assign` without `--room`.** Which room the ask lands in when `--room` is not given (the
one room shared with `PERSON`, or a refusal when there are several) is not settled.
