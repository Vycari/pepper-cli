# CLI core coverage — every core Pepper task from the terminal

**Status:** Proposed. Chris decided the items in §10 on 2026-10-16. The questions in §11 are open.
Nothing here is built on `main` yet: the commands below were prototyped on a local integration
branch (`proto`) and land as the PRs in §9.
**Author:** drafted with Claude Code for Chris
**Last updated:** 2026-10-17
**Tracked by:** not filed
**Related:** `planning/cli-information-architecture.md` (the grammar this extends; its §2 rows and
§3.1 verbs change in the PRs below, not here), pepper-cli#50 (`pepper next` after pepper#4820),
vycari#142 (`docs/design/streams-to-pepper-room.md`, todo assignment), pepper `planning/requests.md`
(the Requests primitive), pepper `planning/dash.md`, pepper#4802 (the Dash/Inbox rename). Server
paths below are relative to `src/pepper/` in Vycari/pepper; CLI paths are relative to
`src/pepper_cli/`.

## 1. Goal and working rule

Chris's goal (2026-10-16): **"A CLI which can functionally do every core task Pepper does."**

Three things follow from it, and the rest of this doc applies them:

1. **Core tasks, not every screen.** A core task is something a person does with Pepper most days:
   see what to do now, read and change the calendar, decide what is waiting on them, keep todos,
   read what they follow, talk to rooms and to Pepper, find what they saved. Small features stay
   reachable through chat or through the generic `pepper apps show/run` (§7).
2. **New product ideas are prototyped on the CLI first.** A command is cheaper to build, test and
   throw away than a web or iOS screen, so an idea gets a CLI shape before it gets a screen. The
   ranking in `feed` (§4.3) and the rework of `next` (§5) were both worked out this way.
3. **Agents are the CLI's main users.** Every leaf keeps `--json`, every list keeps the
   `{"items": [...], "next_before": ...}` envelope, and writes take `--yes` off a terminal
   (`cli-information-architecture.md` §3.1, §5). That is also why the email rule in §6 exists: an
   agent runs with `--yes`, so the CLI itself has to refuse what an agent must never do.

## 2. Where the CLI stood

A coverage audit on 2026-10-16 walked every product area against the server's `/api/v1` routes and
the owner-scoped app actions a PAT can run. Before this work the CLI covered rooms, topics,
messages, members, sharing, apps, notes, todos (list, create, done, dismiss, reopen), scheduled
tasks, skills, trajectories, `calendar`, `next` and chat. The gaps that matter for core tasks were:

| Area | Gap | Server today |
|---|---|---|
| Undecided (Dash) | no way to see or act on Dash cards | web-only `/apps/dash/data`; no PAT action route |
| Calendar | read-only; no event show, create, edit, delete or RSVP | calendar app actions over the PAT action route |
| What's new | nothing for what the person follows | Following app document and actions |
| Todos | no show or edit; no asking someone else | no `GET`/`PATCH /api/v1/todos/{id}` |
| Brief | only `apps show brief` | Brief app document, `brief.run_now` |
| Chat | no attachments, no history, no thumbs | `/artifacts/upload` + `attachment_ids`, `/chat/history`, `/trajectories/{id}/feedback` |
| Saved things | `notes` only touches text notes | `/artifacts*` for every kind |
| People, preferences, booking | nothing | `/people/*`, `/calendars/preferences`, booking app actions |
| `next` | broken by pepper#4820: plans are always empty (pepper-cli#50) | `schedule-plan` keeps answering with no plans |

## 3. The intent map

`pepper --help` now opens with a map from the question a person (or an agent) has to the command
that answers it, before argparse's alphabetical-ish command list. Chris approved the wording below
as canonical on 2026-10-16. It lives in `cli.py` as `INTENT_MAP`, and `tests/test_cli_intent_map.py`
checks that every command it names exists.

```
What do you want to know?
  What should I do now / today?              pepper next
  What did my morning brief say?             pepper brief
  What have I committed to?                  pepper calendar
  What's undecided, waiting on me?           pepper dash
  What do I need to do?                      pepper todos
  What's new in things I follow?             pepper feed list  (manage: following)
  What's going on in my rooms?               pepper rooms / messages  (rooms watch to stream)
  What have I written down?                  pepper notes
  What have I saved (notes, links, files)?   pepper artifacts
  Who is this person?                        pepper people
  My timezone, work hours, booking links     pepper preferences / booking-pages
  What runs without me?                      pepper scheduled-tasks / skills
  Ask Pepper anything                        pepper <message>  (or chat)
  What did Pepper do, and why?               pepper trajectories
  Account                                    pepper login / whoami / logout / takeout

In a room
  Its topics, people and apps                pepper topics / members / apps
  What it sees of your calendars             pepper sharing
  A message's files                          pepper attachments
  Taking part as an agent                    pepper agents guide
```

One change since the approval: the "What's new" row points at `feed list  (manage: following)`
rather than at `following`, because `feed` became a noun (§4.3) and the stream is what the
question asks for; `following` is where the person changes what feeds it.

## 4. New nouns and verbs

### 4.1 The table

"Works today" means against production as it is, through existing PAT routes or app actions run
through `POST /api/v1/apps/{slug}/actions/{name}`. "Needs" names the server change it waits on
(§8).

| Command | What it does | Server dependency |
|---|---|---|
| `dash list` / `show N` | The open Dash cards, newest first and numbered (`--kind`, `--source`, `--snoozed`, `--limit`); one card in full with its context, options, links and draft bodies | Works today from the Dash app document. Prefers `GET /api/v1/apps/dash/view` when the server has it |
| `dash do N OPTION` / `dismiss N` / `snooze N` / `undo` | Take one of a card's options exactly as its web button does; decline a card; hide it until later; reverse the last action | Needs `POST /api/v1/apps/dash/actions/{name}` and `GET /api/v1/apps/dash/view` (pepper branch `inbox-pat-actions`) |
| `feed list` | One ranked stream of the recent items of everything followed, numbered (`--limit`, `--group`, `--why` for each item's score breakdown) | Works today: ranking is client-side over the Following app document (§4.3) |
| `feed show N` | One item in full: source, score, sports or episode detail. `--transcript` for a podcast episode, `--read` (with `--full`) fetches the article from its own site | Works today. `--transcript` uses the Following app's `episode_transcript` action; `--read` is a plain client-side GET that never carries the Pepper token |
| `following list` / `show` / `search` / `create` / `delete` | What the person follows, by group; one followed thing; find something to follow (team, show, podcast, website address); follow number N from the last search; unfollow | Works today over the Following app's owner-scoped actions |
| `events show` / `create` / `edit` / `delete` / `rsvp` | One event in full; add, change or delete an event on the primary calendar; reply yes, no or maybe | Works today over the calendar app's actions. **Writes hit the person's real Google Calendar.** `rsvp` has no note (§6) |
| `brief [--date D] [--now]` | The latest brief edition, readably; `--date` an older one; `--now` runs a fresh one (it arrives where the brief is delivered) | Works today: Brief app document, `brief.run_now` |
| `chat --attach PATH` | Send a file with the message (repeatable, up to 8). The REPL gets `/attach PATH` | Works today: `/artifacts/upload`, then `attachment_ids` on `/chat/send` |
| `chat --history [--date D] [--limit N]` | The past chat thread, read-only | Works today: `GET /api/v1/chat/history` |
| REPL `/good`, `/bad` | Thumbs on the last turn | Works today: `/trajectories/{id}/feedback` |
| `trajectories feedback ID up\|down\|clear [--note]` | Thumbs on any turn, or take the thumb back | Works today: `POST`/`DELETE /api/v1/trajectories/{id}/feedback` |
| `artifacts list` / `show` / `download` / `create` / `delete` | Every kind of saved thing (note, link, file): find, read, fetch the bytes, upload a file, delete | Works today: `GET /api/v1/artifacts`, `/{id}/detail`, `/{id}/raw`, `POST /upload`, the artifacts app's `delete` action |
| `people show NAME\|EMAIL` / `search QUERY` | What Pepper can say about a person (contact match, prior meeting notes); search saved contacts | Works today: `/people/context`, `/people/contacts/search`. Read-only; contacts only where Google Contacts is enabled |
| `preferences show` / `edit` | Timezone plus the Calendar > Preferences values (addresses, work days and hours, meals, workouts, weather switches) | Works today: `GET`/`POST /api/v1/calendars/preferences` |
| `booking-pages list` / `show` / `create` / `edit` / `pause` / `resume` / `delete` | The links people use to book time: list with their links, one in full, create on the booking defaults, change, take offline and back, delete | Works today over the booking app's actions. The public link is built client-side (optional server field, §8) |
| `todos show N` / `edit N` | One todo in full; change title, details, due or priority in place | Needs `GET`/`PATCH /api/v1/todos/{id}` (pepper branch `todo-pat-writes`) |
| `todos create --room R --assignee PERSON`, `list --mine` / `--asked`, `edit --assignee`, `dismiss --reason`, `assign N --to PERSON [--room R]` | Ask someone in a room to do something, see what is asked of you and what you asked (§4.4) | Needs vycari#142 phase P1a in the server |

Commands that print a list a later command can point into are numbered (§4.2). Destructive verbs
and anything that reaches another person follow the prompt / `--yes` rule (§6).

### 4.2 Numbering

`todos list`, `calendar`, `dash list`, `feed list`, `artifacts list` and `booking-pages list` print
a number on each row, and later commands accept that number in place of an id: `dash do 3 o1`,
`events rsvp 2 yes`, `feed show 5`. The last listing of each noun is saved per profile, next to the
credentials file (`credentials.credentials_path()`), one small JSON file per noun, so two profiles
never see each other's numbers. A reference longer than four digits, or not all digits, is an id or
a unique id prefix. On `proto`, `numbered_view.py` holds the shared mechanism for `artifacts` and
`booking-pages`; `todo_numbers.py`, `events_view.py` and `dash_view.py` each keep their own copy of
the same idea.

### 4.3 `feed`

`feed` flattens the recent items of every followed thing into one stream. The ranking runs in the
client (`feed.py`): `Weights` holds every tunable, so tuning is one dataclass and `--why` prints the
breakdown. The rules Chris asked for:

- **One entry per team**, and at most two per site or show. The rest fold under it ("+N more").
- **Results count as news.** A Sports final gets the same boost as a new article.
- No long runs from one source or one group.
- Recency halves every 36 hours; upcoming events further out than a week drop out, and those
  inside a day ramp up; a source that is live right now leads.
- Each row gets a **coloured type tag** (sports, podcast, article, show, event).

`feed show N --read` fetches the article's page from its own site and prints its readable text
(`article_reader.py`). It is a plain third-party GET with its own HTTP client, a timeout, a redirect
cap and a byte cap, and it **never sends the Pepper token, cookies or any other credential** to the
third party.

`feed` is a noun with `list` and `show`, not a view like `calendar`
(`cli-information-architecture.md` §3 rule 4), because actions on items are expected: hide this,
less like this, mark played. Whether that holds is §11 A, for Allen.

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

`next` was rebuilt around the todo list. This fixes pepper-cli#50: since pepper#4820 the server's
`schedule-plan` returns no plans, so the old view showed an empty "Do next", hid the person's own
todos due soon, and silently ignored `--first`. The work is on branch `next-from-todos`.

The view has three parts (`next_view.py`):

1. **Now.** The live event, and the next commitment. A timed deadline or a reminder counts as a
   commitment.
2. **Today's calendar.** Commitments only, never work blocks, with `due:` and `remind:` rows at
   their times and a "due today:" line for todos due today with no time.
3. **Todos, ranked, with reasons.** Overdue, then due today, then importance, then due soon, short
   first among equals. Each row says why it is where it is. **Todos are never put into time
   slots.**

Two rules shape the ranking:

- **Exponential backoff on due-driven boosts.** The boost from a due date decays with distance, so
  something due in three weeks barely moves; overdue and due today get the full boost.
- **A 24-hour grace for passed timed deadlines and reminders.** For a day after its time, a passed
  deadline or reminder stays visible and is marked passed; after that it drops out of every part of
  the view.

**`--first` is removed.** It chose a packing strategy for the old time-slot fitter, which no longer
exists. This is a breaking change: `next --first ...` exits 2 with argparse's usual error. It is
listed under Breaking in the CHANGELOG (§9).

## 6. Rules

- **The CLI never sends email as the person** (Chris, 2026-10-16). Agents drive the CLI with
  `--yes`, so a confirmation prompt is not a safeguard here; the CLI has to refuse. So:
  - `dash do` refuses an option whose draft is an email, and says to send it from the web or iOS
    Dash. A drafted room message is allowed.
  - There is no `drafts send`.
  - `events rsvp` has no note: a note is an email to the organizer.
  - Accepted: the invitations, updates and cancellations Google sends to guests because of
    `events create/edit --guests` or `events delete`, and the ask that `todos assign` delivers in
    Pepper. These
    are notices from the system, not mail written in the person's voice.
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

## 7. Rejected or removed

| What | Decision |
|---|---|
| `todos signal` / `todos signals`: local hints an agent could give `next` about a todo ("started", "blocked") | Removed (Chris, 2026-10-16): no usage yet. `next` ranks from the todo list alone |
| Fitting todos into time slots in `next` | Rejected: todos are ranked, never scheduled (§5). It matches pepper#4820, which stopped planning todos into the day |
| Dedicated commands for calls (Answering), shopping, meals, recipes, workouts, journal, voice notes and images | Rejected (Chris, 2026-10-16): small features, or `artifacts` covers them. They stay reachable through chat and `pepper apps show/run` |
| `drafts send` (send an email draft Pepper staged in chat) | Rejected: the CLI never sends email as the person (§6) |

## 8. Server changes

**No database change anywhere.** Every change below is a route or a field over storage that exists.

| Branch / item | What | Unblocks |
|---|---|---|
| pepper `inbox-pat-actions` | `GET /api/v1/apps/dash/view` and `POST /api/v1/apps/dash/actions/{name}`: the PAT twins of the web Dash view and action routes (`api/dash.py`, `features/dash/web.py`) | `dash do/dismiss/snooze/undo` |
| pepper `todo-pat-writes` | `GET`/`PATCH /api/v1/todos/{id}` | `todos show/edit`. A PR with only show and edit is ready to open on Chris's go |
| pepper `todo-pat-writes`, rest | assign, asked and withdraw routes | Superseded by vycari#142 P1a; not opened |
| vycari#142 P1a | `assignee_person_id` on room todo create, `?assignee=me` / `?requester=me` / `?asked_of_me=true`, decline and withdraw over dismiss, PATCH with `assignee_person_id` | todo assignment (§4.4) |
| Optional | `public_url` on booking pages in the booking app's data | `booking-pages` stops building the link itself (`booking_view.py`) |
| Optional | An ISO time on Following recent items | `feed` stops parsing a moment back out of the display label (`FeedEntry.when`) |

## 9. Phases

One PR per area, each cut fresh from `main` (not from `proto`), each with its own tests, README and
bundled `SKILL.md` changes, and a CHANGELOG `Unreleased` entry. Releases are separate `release:`
PRs. Phases without a dependency can run in parallel, kept file-disjoint where `cli.py` allows.

| Phase | PR | Contents | Depends on |
|---|---|---|---|
| 1 | Intent map | `INTENT_MAP` at the top of `pepper --help` (§3), its test | — |
| 2 | `next` | §5; refs pepper-cli#50. Listed under **Breaking** (`--first` removed) | — |
| 3 | Dash | `dash list/show/do/dismiss/snooze/undo` (§4.1) | `inbox-pat-actions` deployed, snapshot refreshed |
| 4 | Feed and following | `feed list/show`, `following list/show/search/create/delete`, `article_reader.py` | — |
| 5 | Events | `events show/create/edit/delete/rsvp`, numbered `calendar` | — |
| 6 | Brief, chat, feedback | `brief`, `chat --attach/--history`, REPL `/attach /good /bad`, `trajectories feedback` | — |
| 7 | Account and data | `artifacts`, `people`, `preferences`, `booking-pages`, `numbered_view.py` | — |
| 8 | Todos show and edit | `todos show/edit` | `todo-pat-writes` (show/edit only) deployed, snapshot refreshed |
| 9 | Todo assignment | §4.4 | vycari#142 P1a deployed, snapshot refreshed |

The IA doc's §2 rows and §3.1 verbs for each noun move in the PR that adds the noun, so the doc and
the code stay in step.

## 10. Decisions (2026-10-16, by Chris)

| | Decision |
|---|---|
| 1 | Goal: a CLI that can functionally do every core task Pepper does. New product ideas are prototyped on the CLI first. Agents are its main users (§1) |
| 2 | The intent map in §3 is the canonical wording for the top of `pepper --help` |
| 3 | The product goes back to the **Dash** name, so the noun is `dash`, not `inbox` |
| 4 | `artifacts` covers every kind (note, link, file). This replaces the IA doc's "never artifacts" rule; `notes` stays the surface for editing note text |
| 5 | `brief` joins the set of views; `--now` runs a fresh one, rather than a `brief run` verb |
| 6 | The CLI never sends email as the person (§6) |
| 7 | `next` ranks todos with reasons and never fits them into time slots; `--first` goes (§5) |
| 8 | `todos signal/signals` removed; dedicated commands for small features and `drafts send` rejected (§7) |
| 9 | Todo assignment follows vycari#142; `todos assign` is a CLI-only handoff, create then dismiss (§4.4) |
| 10 | One PR per area, cut fresh from `main`, in the order of §9 |

## 11. Open questions

**A. `feed`: a noun or a view?** (for Allen) It is a noun today because item actions are expected
(hide, less like this, mark played). If those never come, it is a read like `calendar` and could be
a view, `pepper feed`, with `feed show N` becoming a flag. Allen decided the view set is closed in
the IA doc, so this is his call.

**B. Fold `notes` into `artifacts`?** `artifacts` lists, reads, fetches, uploads and deletes every
kind; `notes` lists, searches, shows, creates, edits and deletes text notes. The overlap is
everything but editing text. Options: keep both (today); make `notes` a thin alias over
`artifacts --kind note` plus `edit`; or move `edit` and `create --text` onto `artifacts` and retire
`notes`. The last is a breaking change for agents that use `notes`.

**C. `brief --now` or a `brief run` verb?** `--now` keeps `brief` a view (a view takes flags, not
verbs). A verb would make `brief` a noun with `show` and `run`.

**D. Show drafts read-only in `chat --history`?** Email drafts Pepper staged in chat could appear in
the history as read-only blocks, so a person can see what is waiting for them on the web. They
cannot be sent from the CLI either way (§6).

**E. `people search` with no results.** The empty message does not tell "no match" apart from "this
account has no Contacts access". The server answer may not carry the difference.

**F. `todos assign` without `--room`.** Which room the ask lands in when `--room` is not given (the
one room shared with `PERSON`, or a refusal when there are several) is not settled.
