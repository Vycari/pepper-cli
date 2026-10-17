# CLI information architecture — one grammar, rooms everywhere, `--json` everywhere

**Status:** Accepted 2026-10-14. The maintainer decided §10 A–F; the `groups` → `rooms` rename was
decided earlier. Lands as one coordinated breaking release, **0.2.0** (§7).
**Author:** drafted with Claude Code for the maintainer
**Last updated:** 2026-10-17
**Tracked by:** #20
**Related:** pepper-cli#17 (scheduled-task renew), pepper-cli#18 (generic `pepper apps`); server
pepper#4311 (room tasks, join links, GitHub), pepper#4556 (room skills), pepper#4557 (skill-backed
room tasks), pepper#4673 (response models for groups routes), pepper#2740 (session-only routes);
pepper `planning/naming-strategy.md`, pepper `planning/cli-artifacts.md`

## 1. Problem

The CLI grew one family at a time and each family made its own choices. Before Chris widens it, the
maintainer wants one grammar, room support on every command where a room means something, `--json`
on every command, and nouns that match what the product calls things. Concretely:

| # | Inconsistency | Where (0.1.0) | Fixed in |
|---|---|---|---|
| 1 | `groups` is one flat namespace over five object kinds (room, topic, message, member, app) plus sharing, search and activity | `groups *` | §3, §4 |
| 2 | Create is `add` (todos, scheduled-tasks), `new` (skills, notes, groups, `new-topic`); delete is `delete` (skills), `rm` (notes), `remove` (members), `delete-message`; scheduled tasks cannot be deleted at all | all families | §3.2, §10 B, §10 E |
| 3 | The product says "room", the CLI says `groups`; the room is a positional `GROUP` in `groups` and a `--room` flag everywhere else | `groups *` vs `todos`/`notes`/`skills`/`scheduled-tasks` | §2, §3 rule 2 |
| 4 | `trajectory` is singular; every other noun is plural | `trajectory` | §2 |
| 5 | `--json` exists on 17 of 69 leaves | §5.4 | §5 |
| 6 | `scheduled-tasks` takes `--room` on list/add/pause/resume/destinations but not show/runs/thread/edit | `scheduled-tasks` | §6 |
| 7 | `edit` opens `$EDITOR` in skills and notes but is a flag patch in scheduled-tasks | `skills edit`, `notes edit`, `scheduled-tasks edit` | §3.3 |
| 8 | Pagination is `--limit/--offset` (trajectory), `--limit/--before/--since` (read), `--limit` only (runs, thread) | `trajectory list`, `groups read` | §5.3 |
| 9 | `groups sharing` is a read, and a write when `--set` is given | `groups sharing` | §4 |
| 10 | `agent-skill` (print the agent guide) collides with the `skills` noun | `agent-skill` | §4 |
| 11 | `groups search` matches topic names; `notes search` matches names and bodies; nothing says so | `groups search`, `notes search` | §3.4 |
| 12 | `next --room` is a client-side prefix filter on room *or source*, not room scoping | `next` | §4 |
| 13 | `scheduled-tasks add/destinations --topic` names a delivery target, not a scope — it collides with `--topic` as the topic selector | `scheduled-tasks` | §4 |
| 14 | `--since` is an int event cursor (`watch`), a message cursor (`read`), an ISO timestamp (`activity`) | `groups watch/read/activity` | §5.3 |
| 15 | `calendar` has no `--room` although `GET /api/v1/calendars/planning?principal=` exists | `calendar` | §6 |

## 2. Nomenclature

Rule: the CLI uses the word the product's own UI uses. Where the product is mixed, this table
picks one and names the evidence on both sides. Paths are in the pepper server repo at
`origin/main` unless marked `cli:`.

| CLI today | Product says | Evidence | CLI from now on |
|---|---|---|---|
| `groups`, "rooms/channels", positional `GROUP` | "room" in UI chrome; "group" in some prose and URLs | room: `src/pepper/web/templates/_group_info_body.html` ("Room info", "Rename room", "Leave this room", "Open room"/"Sealed room"), `_group_header.html` ("Room info"), iOS `clients/pepper-ios` ("Room brief", "Message the room…", "Room info"). group: `src/pepper/features/help/skills/pepper-guide/reference/channels.md` ("Seeing your groups", `/groups`), `scheduled_task_detail.html` ("Group:") | **`rooms`**, `--room ROOM`, `ROOM` metavar (decided). Server-side "group" prose is a server docs follow-up (§6.2) |
| "channel" (help: "Room/channel name") | three meanings: SMS/email/web delivery (`reference/channels.md`); open vs sealed room (pepper#4556 "Channel (open) vs Room (sealed)"); "rooms/channels" in CLI help | as cited | **never used** in CLI help or flags. A named room is a "channel" in `rooms show`; the wire kind `sealed` (an email or text thread, listed under "Other conversations" in the apps) reads "email/text thread" (groups-simplification phase 5) |
| `--to groups_service` (scheduled tasks) | "Delivery", "Delivered to:" | `scheduled_task_detail.html` | accept **`--to room`**; send `groups_service` on the wire (server enum unchanged) |
| topics (`groups topics`, `new-topic`) | "Topics", "New topic", "Rename topic" | `_group_info_body.html`, iOS | **`topics`** |
| messages (`read`, `post`, `edit`, `delete-message`, `react`) | "Edit", "Delete", "Reply", "Report message" on a message | `_room_fragment.html` | **`messages`** |
| `roster` | "Who's here", "Members" | `_group_info_body.html`, iOS, `scheduled_task_detail.html` ("Members:") | **`members`** (`roster` stays the API path name only) |
| `role … owner\|member` | "Owner", "Make owner", "Remove owner" | `_group_info_body.html` | `members edit --role owner\|member` |
| `invite` | "Invite someone" | `_group_info_body.html` | `members invite` |
| `mute`/`unmute` | "Notifications" toggle; no "mute" word | `_group_info_body.html` | keep `mute`/`unmute` (no product verb to defer to; the help says "notifications") |
| `apps`, `install-app`, `uninstall-app` | "Apps", "Install", "No apps installed in this room yet.", "launcher" | `_group_info_body.html`, `reference/apps.md` | **`apps`** with `install`/`uninstall`. Never "mini-apps" (appears nowhere in UI) |
| `sharing` | "What I share with this room"; "Rooms see" column | `_group_info_body.html`, Manage calendars | **`sharing`** |
| `trajectory` | "Trajectories", "Activity history" | `trajectories_list.html` | **`trajectories`** |
| `scheduled-tasks`, `runs`, `thread`, `destinations` | "Scheduled tasks", "Pause task", "Resume task", "Keep it going", "Run history", "Thread", "Delivery" | `scheduled_tasks_list.html`, `scheduled_task_detail.html`, `settings/_scheduled_tasks.html` | `scheduled-tasks`; `runs`, `thread` kept; `destinations` folds into `edit --to` (the product has one "Delivery" setting, not a separate object); `renew` for "Keep it going" |
| `skills` | "Skills"; "Sharing a skill with a group" | `settings/_skills.html`, `reference/skills.md` | `skills` with `share`/`revoke` |
| `notes` (kind `inline_text`) | "Notes" (sidebar, room "Notes board"); "Artifacts" on the settings page | `_sidebar.html`, `reference/artifacts.md`, `settings/_artifacts.html` | `notes`, the text-editing surface: `notes edit` takes the body (`$EDITOR`, `--file`, `If-Match`), which no other kind has. It is not the whole collection: see the `artifacts` row |
| `artifacts` (every kind: `inline_text`, `external_url`, `blob`) | "Artifacts": the app is titled "Artifacts" (`agent/apps/registry.py` `ARTIFACTS_APP`), as are the room page surface (`features/groups/surfaces.py`, `key="artifacts"`), the settings section ("The notes, links and files you've asked Pepper to keep", `settings/_artifacts.html`) and the help topic (`features/help/content.py`, `reference/artifacts.md`). The kinds are "Note", "Link", "File" (`KIND_LABELS`, `agent/artifacts/applet.py`); images, PDFs, recordings, recipe photos are Files, a research report or a recipe is a Note. The iOS app has `ArtifactDetail.swift` | as cited | **`artifacts`** with `list` (`--kind note\|link\|file`, `--label`, `--search`, `--limit`, `--room`; the server filters, the CLI never does), `show`, `download`, `create` (uploads a file), `delete`. Replaces the earlier "never `artifacts`" rule (maintainer, 2026-10-16: all kinds are supported). `notes` stays for writing note text; `artifacts` is for finding, reading, fetching, uploading and deleting anything. Text search is `artifacts list --search` (the maintainer's spelling), not a separate `search` leaf. Routes: `GET /api/v1/artifacts`, `/{id}/detail`, `/{id}/raw`, `POST /upload`, and the `artifacts` app's `delete` action |
| timezone, calendar preferences | "Preferences" (Calendar sub-page, gear titled "Preferences", `<h1>Preferences</h1>`) holds Home address, Work address, **Timezone** ("The same timezone as in Settings."), work days, hours, meals, workouts, weather (`web/apps/defaults/calendar.html`); Settings > Profile also has "Timezone" (`settings/_profile.html`) | as cited | **`preferences`** with `show` and `edit` (`--timezone`, `--home-address`, `--work-days`, ...). No `timezone` noun: the Preferences route takes the account timezone and the page says it is the same one, so one noun covers both. `whoami` keeps printing the timezone as a read. Which calendars count as busy is not on this route (it is per booking page, `booking.set_conflicts`) and is not covered |
| `feed` (the ranked stream of what you follow) | The Following app (`agent/follows/app.py`, `title="Following"`) draws a "Recent" timeline per followed thing (`web/apps/defaults/following.html`) and the Reading filter has a "Daily feed briefing"; no screen is titled "Feed". "Feed" is the word Chris used for the combined stream ("pp feed"), so this is a requested word, not a UI word, and the maintainer decides whether it stays | as cited | **`feed`**, a view (§3 rule 4), not a noun: `feed [N] [--limit] [--group] [--transcript] [--read] [--full]`. The server ranks, folds and decides (`data.feed` of the Following document, pepper#4852); the CLI fetches and renders, with no business logic of its own. Without `N` it lists the server's order, numbered; with `N` it shows that item. Managing what you follow stays under `following` |
| `following` (the Following app) | "Following": the app is titled "Following" (`agent/follows/app.py`, `title="Following"`), and its page lists what you follow by group (Sports, Shows, Reading, Podcasts, Events) with a Follow / "Follow again" button per thing (`web/apps/defaults/following.html`) | as cited | **`following`** with `list`, `show`, `search`, `create`, `delete`. `search QUERY` runs the app's `search` action and numbers the matches; `create N` follows number N of the last search (saved per profile) and takes nothing else, because choosing a match for typed text is the server `search` action's job, not the CLI's (no business logic in the CLI, Vycari/pepper-cli#61 §6). `delete` is the app's `unfollow` (the app keeps the thing, with Follow again). Actions through the PAT route `POST /api/v1/apps/following/actions/{name}` |
| `todos` | "Todos", "Add a todo"; "to-do" in prose | `reference/todos.md`, iOS | `todos` |
| `calendar`, `--planning` | "Calendar", "Planning" | `_sidebar.html` | unchanged |
| `agent-skill` | "agent member" | cli: `SKILL.md` §1 | **`agents guide`** — frees `skill` for the `skills` noun; leaves room for sponsored-agent commands |
| `notes rm`, `delete-message` | "Delete" | `_room_fragment.html` | `delete` |

Naming principle (pepper `planning/naming-strategy.md`): plain descriptive names. So no new coinage
above; every noun is a word on a product screen.

## 3. Command grammar

```
pepper [--profile P] <noun> <verb> [--room ROOM [--topic TOPIC]] [OBJECT…] [flags]
```

1. **Nouns are plural** and come from §2. Sub-nouns are not used: every child object (topic,
   message, member) is its own top-level noun scoped by `--room`.
2. **One room rule.** The object a verb acts on is positional. A room that is scope or container is
   always `--room ROOM`; a topic that is container is always `--topic TOPIC`. So `rooms show ROOM`
   (the room is the object) but `topics list --room ROOM` (the room is the container). No positional
   `GROUP` survives. `--room` is required when the verb acts inside a room and optional when
   omitting it means "yours"; §6 is authoritative per command.
3. **Canonical order puts `--room` right after the verb.** Harness allow rules are prefix matches
   (`Bash(pepper --profile claude-code messages post --room <room-id>:*)`), so the room must come
   before anything variable. Other orders still parse; they just don't match a pinned rule and get
   prompted, which fails safe. Pin by UUID, not name.
4. **Views and session commands are not noun–verb**: `calendar [WINDOW]`, `next`, `brief`, `feed [N]`, `chat`, `login`,
   `logout`, `whoami`, `takeout`. That set is closed; anything new is a noun. (`brief` joined it as a
   view: it is a daily read like `next` with no managed collection behind it; `--date` picks an older
   edition and `--now` runs a fresh one, because a view takes flags, not verbs. `feed` is a view so an
   agent asking what is new runs one word.)

### 3.1 Verbs

| Kind | Verbs | Meaning |
|---|---|---|
| Read | `list`, `show`, `search`, `watch`, `activity` | `list` = many, filterable; `show OBJECT` = one; `search QUERY` = list by text (help names the fields matched); `watch` = stream |
| Write | `create`, `edit`, `delete` | `create` makes, `edit` changes fields, `delete` destroys the object |
| Association | `invite`/`remove`, `share`/`revoke`, `install`/`uninstall`, `leave` | break a link, never destroy the object |
| Domain | `post`, `react`/`unreact`, `archive`/`unarchive`, `mute`/`unmute`, `pause`/`resume`/`renew`, `done`/`dismiss`/`reopen`, `download`, `run`, `guide` | one each, product-named |

`rename` is gone as a verb: it is `edit --name`. Destructive = `delete`, `remove`, `revoke`,
`uninstall`, `leave`, and `members edit --role`. They prompt on a TTY; off a TTY or under `--json`
they need `--yes` or exit 2. `archive` is not destructive (your view only) and keeps accepting
`--yes` as a no-op.
`following delete` and `artifacts delete` are destructive and take the same prompt / `--yes` rule (the server keeps a deleted artifact for a retention window, but the CLI has no restore).

### 3.2 Create

`create` everywhere (§10 B). `add` (todos, scheduled-tasks) and `new` (skills, notes, rooms, topics)
are removed, not aliased (§7). The product's own buttons are split ("Start conversation", "New
topic", "Add a todo"), so there was no product verb to defer to.

### 3.3 Edit

`edit OBJECT [field flags] [--file PATH|-]`. Non-interactive whenever any field flag or `--file` is
given. With neither, it opens `$EDITOR` on the object's body if stdin is a TTY and the noun has a
body (skills, notes, scheduled-task prompt, message text); otherwise exit 2 with a usage line.
`skills create/edit` gain `--file`. `scheduled-tasks destinations` becomes `edit --to/--default/
--silent`. `messages edit MESSAGE TEXT` becomes `messages edit MESSAGE [TEXT|-]`.

### 3.4 Search

`topics search QUERY [--room]` matches topic names (the server's `/groups/topics/search` does no
more); `notes search` matches names and bodies. Each help line states its fields. Message-body
search does not exist on the server and is not proposed here.

## 4. Old → new

Every left-hand spelling stops working in 0.2.0; there are no aliases (§7). `#17`/`#18` = the open
pepper-cli issue that lands there. Every leaf gets `--json` (§5).

| 0.1.0 | 0.2.0 | Notes |
|---|---|---|
| `groups list [--archived]` | `rooms list [--archived]` | |
| `groups show GROUP` | `rooms show ROOM` | room is the object |
| `groups new --name PEOPLE` | `rooms create [--name N] PEOPLE…` | |
| `groups rename GROUP NAME` | `rooms edit ROOM --name NAME` | |
| `groups archive/unarchive GROUP` | `rooms archive/unarchive ROOM` | |
| `groups leave GROUP --yes` | `rooms leave ROOM --yes` | |
| `groups mute/unmute GROUP` | `rooms mute/unmute ROOM` | |
| `groups watch --room --topic` | `rooms watch [--room R]… [--topic T]` | already flag-scoped |
| `groups activity [GROUP] --since` | `rooms activity [--room R] [--since]` | |
| `groups topics GROUP` | `topics list --room R [--archived]` | |
| `groups new-topic GROUP NAME` | `topics create --room R NAME` | |
| `groups rename-topic GROUP TOPIC NAME` | `topics edit --room R TOPIC --name NAME` | |
| `groups archive-topic/unarchive-topic GROUP TOPIC` | `topics archive/unarchive --room R TOPIC` | |
| `groups search QUERY` | `topics search [--room R] QUERY` | §3.4 |
| `groups read GROUP [TOPIC]` | `messages list --room R [--topic T] [--limit --before --since --mark]` | `--topic` optional as today (default topic) |
| `groups post GROUP TOPIC [MSG\|-]` | `messages post --room R --topic T [MSG\|-]` | heredoc form: `pepper messages post --room R --topic T - <<'EOF'` |
| `groups edit GROUP MESSAGE TEXT` | `messages edit --room R MESSAGE [TEXT\|-]` | |
| `groups delete-message GROUP MESSAGE` | `messages delete --room R MESSAGE --yes` | |
| `groups react/unreact GROUP MESSAGE EMOJI` | `messages react/unreact --room R MESSAGE EMOJI` | |
| `groups download GROUP ATTACHMENT` | `attachments download --room R ATTACHMENT [--out]` | |
| `groups roster GROUP` | `members list --room R` | |
| `groups invite GROUP HANDLE` | `members invite --room R HANDLE` | |
| `groups remove GROUP PERSON` | `members remove --room R PERSON --yes` | association, not delete |
| `groups role GROUP PERSON ROLE` | `members edit --room R PERSON --role owner\|member --yes` | |
| `groups apps GROUP` | `apps list --room R` | #18: `apps list` without `--room` = your apps |
| `groups install-app GROUP KEY` | `apps install --room R KEY` | |
| `groups uninstall-app GROUP APP` | `apps uninstall --room R APP --yes` | |
| — | `apps show SLUG [--room R]` | #18 "data", `GET /apps/{slug}/data?principal=` |
| — | `apps run SLUG [ACTION] [--room R] [--param k=v]…` | #18 "action"; no ACTION lists declared actions |
| `groups sharing GROUP` | `sharing show --room R` | splits #9 |
| `groups sharing GROUP --set CAL=LEVEL` | `sharing edit --room R --set CAL=LEVEL…` | |
| `trajectory show ID` | `trajectories show ID` | |
| `trajectory list --limit --offset` | `trajectories list --limit --before` | `--offset` removed; §5.3 |
| `todos add TITLE` | `todos create TITLE` | |
| `todos list/done/dismiss/reopen` | unchanged | |
| `scheduled-tasks add NAME` | `scheduled-tasks create NAME` | |
| `scheduled-tasks add/destinations --topic NAME` | `--deliver-topic NAME` (`--topic` removed on this noun) | #13: `--topic` is reserved for scope |
| `scheduled-tasks destinations TASK` | `scheduled-tasks edit TASK --to/--default/--silent` | |
| `scheduled-tasks list/show/runs/thread/edit/pause/resume` | unchanged names; `--room` per §6 | |
| — | `scheduled-tasks renew TASK [--room R]` | #17; prints the 422 refusal text on refusal |
| — | `scheduled-tasks delete TASK [--room R] --yes` | decided (§10 E); after the server endpoint (§6.1 #3) |
| `skills new NAME` | `skills create NAME [--file]` | |
| `skills list/show/edit/delete/share/revoke` | unchanged; `edit` gains `--file` | |
| `notes new NAME` | `notes create NAME` | |
| `notes rm NOTE` | `notes delete NOTE --yes` | |
| `notes list/search/show/edit` | unchanged; `edit --rename` → `--name` | |
| `agent-skill [--write DIR]` | `agents guide [--write DIR]` | #10 |
| `next --room PREFIX` | `next [--room R] [--source PREFIX]` | `--room` resolves strictly like everywhere else; prefix filtering moves to `--source`. Behavior change on an existing flag, in the 0.2.0 notes |
| `calendar`, `chat`, `login`, `logout`, `whoami`, `takeout` | unchanged | views/session (§3 rule 4) |
| (new) `brief` | a view | a daily read like `next`, and the product has no "briefs" noun to manage: one edition is read, `--date` picks another, `--now` runs one (a view takes a flag, not a verb) |

## 5. The `--json` contract

This is the section implementations are tested against. Invariant 4 (`.claude/guidelines/
invariants.md`) makes stdout and exit codes an interface, so every shape change below is a
deliberate break, made once, in 0.2.0, alongside the renames (§10 D).

### 5.1 Rules

| # | Rule |
|---|---|
| J1 | Every leaf except `chat` with no message (the REPL) and `login` accepts `--json` and honours `PEPPER_JSON` (J10). `logout` emits `{"ok": true}`; `agents guide` without `--write` emits `{"markdown": "..."}`. |
| J2 | In JSON mode (`--json` or `PEPPER_JSON=1`), stdout carries exactly one JSON document (or JSON lines for a stream, J6) and nothing else: no colour, no prose, no progress. Prompts never happen (destructive verbs need `--yes`, else exit 2). |
| J3 | **One list envelope, everywhere.** Every `list`, `search` and `activity` leaf, plus `scheduled-tasks runs/thread`, emits `{"items": [...], "next_before": str\|null}`. `next_before` is always present: `null` on the last page and on unpaged lists. List-level metadata the server returns sits beside them as extra top-level keys, never instead of them: `rooms list` adds `enabled`, `degraded`, `total`, `archived_total`; `notes list/search` add `truncated`. This replaces every 0.1.0 list shape: `groups list` (`groups` key), `groups read` (`messages` key), and the bare arrays of `todos list`, `groups topics/roster/apps/activity/search`. |
| J4 | Views are not lists: `next` and `calendar` emit objects shaped as in 0.1.0 (`calendar --planning` keeps `{"range", "planning"}`). |
| J5 | `show` emits the object. Mutations emit the resulting object as the server returns it; for a 204 route, `{"ok": true, "id": "<id acted on>"}`. `takeout` emits `{"path", "bytes"}`. `agents guide --write` emits `{"path"}`. |
| J6 | Streams emit JSON lines: `rooms watch` (unchanged, incl. `{"kind":"cursor","cursor":N}` frames) and one-shot `chat MESSAGE --json` (one line per streamed event, final line the reply). |
| J7 | Field names are the server's, passed through. Unknown server fields are kept, not dropped (the CLI already tolerates them). |
| J8 | **Errors** in JSON mode write one line to **stderr**: `{"error": {"code": "...", "status": <http int or null>, "message": "..."}}`. `code` ∈ `auth`, `forbidden`, `not_found`, `conflict`, `validation`, `ambiguous`, `usage`, `upgrade_required`, `server`, `network`, `gap`. stdout stays empty. Outside JSON mode, errors stay `pepper: …` text. |
| J9 | Exit codes unchanged: 0 ok, 1 auth/API/network (incl. 426), 2 usage/resolution/refused-without-`--yes`/renamed noun (§7), 3 watch replay gap, 130 interrupt. |
| J10 | **`PEPPER_JSON=1`** turns JSON mode on for every leaf that accepts `--json`. Precedence: `--json` on the command line → on; else `PEPPER_JSON` exactly `1` → on; any other value, or unset → off. There is no `--no-json`; `PEPPER_JSON= pepper …` switches it off for one command. The J1 exemptions ignore it (the REPL and `login` stay interactive). There is no root `pepper --json` (§10 F). |

### 5.2 Mechanism

The CLI stays on argparse. Each common flag lives in one shared parent parser, built once with
`add_help=False` and attached with `parents=[...]` to every leaf. `--json` is per-leaf only; there
is no root `pepper --json` (§10 F). A later move to Click, Typer or cyclopts is out of scope here
and tracked in pepper-cli#21; nothing in this contract depends on argparse.

| Parent | Adds | Attached to |
|---|---|---|
| `_json_parent` | `--json`; JSON mode resolved once by `_json_mode(args)` = `args.json or os.environ.get("PEPPER_JSON") == "1"` (J10) | every leaf except J1's two |
| `_room_parent(required)` | `--room ROOM` (UUID or case-insensitive unique name prefix, resolved by `_resolve_group`) | per §6 |
| `_topic_parent` | `--topic TOPIC` | `messages list/post`, `rooms watch` |
| `_yes_parent` | `--yes` | destructive verbs (§3.1) |
| `_page_parent` | `--limit N`, `--before CURSOR`, `--since CURSOR` | paged lists |

Output goes through one `_emit(obj, args)` that writes JSON or calls the existing `render.py`
human formatter, one `_emit_list(items, args, next_before=None, **meta)` that builds the J3
envelope, and one error path that honours J8. Nothing else reads `args.json`. Bare `print` stays confined to `cli.py`.

### 5.3 Pagination and `--since`

`--limit N`; `--before CURSOR` for older; `--since CURSOR` for newer. A cursor is whatever the same
command printed as `next_before` (or `cursor`, for `watch`) and is opaque. `trajectories` is
offset-paged on the server: the CLI encodes the offset as the cursor; `--offset` is removed. `rooms activity --since` also accepts an ISO-8601 time, because that is what its
route takes; help says so.

| Command | Paged by | `--before` | `--since` |
|---|---|---|---|
| `messages list` | server cursor | yes | yes |
| `trajectories list` | offset, as cursor | yes | no |
| `scheduled-tasks runs/thread` | server `before`/`before_id` | yes | no |
| `rooms watch` | event cursor | no | yes (+ `--cursor-file`) |
| `rooms activity` | time | no | yes (time) |

### 5.4 Fill list (`--json` missing in 0.1.0; all client-only)

`whoami`, `chat MESSAGE`, `trajectories list`, `todos create/done/dismiss/reopen`, all of
`scheduled-tasks`, all of `skills`, `rooms show/create/edit/archive/unarchive/leave/mute/unmute`,
`topics create/edit/archive/unarchive`, `messages edit/delete/react/unreact`, `attachments
download`, `members invite/remove/edit`, `apps install/uninstall/show/run`, `notes create/edit/
delete`, `takeout`, `logout`, `agents guide`.

## 6. Rooms everywhere

Class: **R** room-scoped API already used by the CLI; **C** client-only (the API takes a room
today); **S** needs a server change (or verification first); **N** a room is not meaningful.

| Command | `--room` | Class | Route / note |
|---|---|---|---|
| `rooms list/show/create/edit/archive/unarchive/leave/mute/unmute` | — (room is object) | R | `/groups`, `/groups/{gid}/…` |
| `rooms watch`, `rooms activity` | optional | R | `/groups/events/stream`, `/groups/activity` |
| `topics *`, `messages *`, `attachments download` | required | R | `/groups/{gid}/topics…`, `/messages…`, `/attachments…` |
| `topics search` | optional | C | `/groups/topics/search?q=`; filter by room client-side |
| `members *`, `sharing *` | required | R | `/roster`, `/members…`, `/sharing` |
| `apps list/install/uninstall` | optional / required / required | R | `/apps`, `/groups/{gid}/apps…` |
| `apps show/run` | optional | C | `/apps/{slug}/data\|actions/{a}?principal=` (#18) |
| `todos list/create/done/dismiss/reopen` | optional | R | `/groups/{gid}/todos…` |
| `todos create --details/--priority` with `--room` | optional | S-verify | CLI refuses today; `TodoCreateRequest` has both fields — confirm the room route honours them, then drop the refusal |
| `scheduled-tasks list/create/pause/resume` | optional | R | `/groups/{gid}/scheduled-tasks` GET/POST, PATCH `{active, delivery}` |
| `scheduled-tasks edit --to/--default/--silent` | optional | R | PATCH `delivery` |
| `scheduled-tasks show/runs/thread` | optional | S-verify | does personal `/scheduled-tasks/{id}` (+`/runs`, `/thread`) serve a room task id? Schema doesn't say |
| `scheduled-tasks edit --prompt/--cron/--at/--timezone/--description`, `renew` | optional | S | `RoomTaskUpdateRequest` is `{active, delivery}` only; no room renew |
| `scheduled-tasks create --room --at` | optional | S | `RoomTaskCreateRequest` has no one-off `at` |
| `scheduled-tasks create --room --skill` | optional | S | pepper#4557 |
| `scheduled-tasks delete` | optional | S | decided (§10 E); no endpoint yet, personal or room |
| `skills list/show/share/revoke` | optional / optional / required / required | R | `/groups/{gid}/skills…` |
| `skills create/edit/delete` | — | N | a room holds a grant of someone's skill, not a skill; `share` is the room verb |
| `notes *` | optional | R | `/artifacts…?principal=`, `/notes/{id}…?principal=` |
| `trajectories list/show` | optional | S-verify | `/trajectories` has no `principal`; help claims room-shared ones list — verify, else add `?principal=` |
| `calendar --planning` | optional | C | `/calendars/planning?principal=` |
| `calendar` (range) | optional | S | `/calendars/range` has no `principal`; or defer to `apps show calendar --room` |
| `next` | optional | C | strict resolve, filter by room id (§4) |
| `brief` | — | N | your own brief; no room's brief is read here |
| `feed`, `following *`, `preferences *` | — | N | your own follows and your own account's preferences; a room has neither |
| `artifacts list/create/delete` | optional | R | `/artifacts?principal=`, `/artifacts/upload?principal=`, the `artifacts` app's `delete` (owner-scoped) |
| `artifacts show/download` | — | N | the id carries the room; the viewer route reads any artifact the caller can see |
| `chat`, `login`, `logout`, `whoami`, `takeout`, `agents guide` | — | N | talking to Pepper in a room is `messages post --mention` |

### 6.1 Server needs (not filed; for the maintainer to file or fold in)

| # | Need | Existing issue |
|---|---|---|
| 1 | Room task detail: `GET /groups/{gid}/scheduled-tasks/{id}`, `/runs`, `/thread` — **verify personal routes first** | pepper#4311 (slice 1 shipped; this is beyond it) |
| 2 | Room task full edit (`prompt`, `cron`, `timezone`, `description`), one-off `at` on create, and `renew` | new |
| 3 | Scheduled-task delete, personal and room (`DELETE /scheduled-tasks/{id}`, `DELETE /groups/{gid}/scheduled-tasks/{id}`) — decided 2026-10-14 | new |
| 4 | `GET /trajectories?principal=` and `/{id}` — **verify** room-shared listing first | new |
| 5 | `GET /calendars/range?principal=` | new (or defer to #18) |
| 6 | Room todo `details`/`priority` — **verify** honoured | new if not |
| 7 | Skill-backed room task | pepper#4557 |
| 8 | Join links (would land as `rooms links …`, spelled when the route exists) | pepper#4311 |
| 9 | Typed response models for groups routes — lets the J3 `items` be checked against the contract | pepper#4673 |
| 10 | `whoami` returns 401 for agent tokens, so `whoami --json` cannot be the agent login check | note only |

Order per `CLAUDE.md`: server lands → weekly snapshot refresh of `contracts/api-v1.openapi.json`
(never hand-edited) → CLI.

## 7. Compatibility: a hard break in 0.2.0

No aliases (§10 C). The CLI is pre-1.0 and its main users are agents driven by skills, so the
skills move with the CLI instead of the CLI carrying old spellings.

| Item | Rule |
|---|---|
| Release | All renames, the J3 envelope and the `--json` fill ship together as **0.2.0** (minor bump from 0.1.0). Once the first breaking PR (phase 2a) merges, no release is tagged until 0.2.0 is cut; `main` may be half-renamed in between and nothing ships from it. |
| Old verbs and flags | Under a noun that survives (`todos add`, `notes rm`, `skills new`, `scheduled-tasks destinations`, `--offset`, `--rename`, scheduled-task `--topic`): argparse's normal `invalid choice` / `unrecognized arguments`, exit 2. The `invalid choice` line already lists the new verbs. |
| Old nouns | `groups`, `trajectory` and `agent-skill` stay registered as hidden stubs. Without them the implicit-chat fallback would send `pepper groups read …` to Pepper as a chat message. A stub runs nothing: it prints one line, e.g. `pepper: 'groups' was renamed in 0.2.0 — see 'pepper rooms\|topics\|messages --help'`, and exits 2. They stay registered permanently (cheap; a stale agent must never chat by accident). |
| Lockstep | 0.2.0 is cut the same day these merge: (1) in this repo, bundled `src/pepper_cli/data/agent-skill/SKILL.md` and `README.md`, in the phase PRs that change each command; (2) a vycari workspace PR for `plugins/vycari-ops/skills/{access-trajectory,pepper-groups,room-agent}/SKILL.md`, including room-agent's allow rules (lines 96–102, rewritten to `messages post --room <room-id>:*` etc.); (3) a pepper server PR for `src/pepper/features/help/skills/pepper-guide/reference/cli.md`. (2) and (3) are opened as drafts during phases 2a–2d and merged at release. SKILL.md §8's eight agent commands become `rooms list`, `topics list`, `members list`, `messages list`, `rooms watch`, `messages post`, `messages react`, `messages unreact`; §3–§6's documented JSON shapes move to J3. |
| Running agents | Saved harness allow rules match on prefixes such as `Bash(pepper --profile X groups post <id>:*)` and `groups watch:*`; after an upgrade they match nothing, and the agent stops or prompts. Before 0.2.0 is published, list the deployed agents (the ops-01 "Claude" agent; anything running `pepper groups watch`) and have each one's settings rewritten the same day, or pin `vycari-pepper<0.2` until it is. The release notes carry the full §4 table. |

## 8. Conformance test and how to add a command

`tests/test_cli_conformance.py` tests behaviour, not argparse internals, so it survives a parser
library switch (pepper-cli#21). It enumerates commands from `pepper --help` and `pepper <noun>
--help`, checks each leaf's flags from `pepper <noun> <verb> --help`, and checks parsing and
output by running `main([...])` with a faked API client and captured stdout/stderr. Nothing reads
argparse private attributes.

| Check | Assertion |
|---|---|
| Noun | top-level name ∈ §2 nouns ∪ views/session set ∪ `RENAMED` (the three §7 stubs) |
| Verb | leaf name ∈ §3.1 verbs |
| `--json` | present, unless in `JSON_EXEMPT` (`chat` REPL path, `login`) |
| `PEPPER_JSON` | for every non-exempt leaf, stdout is JSON with `PEPPER_JSON=1` and no flag, human text with `PEPPER_JSON=0` or unset, JSON with `--json` regardless of the variable; exempt leaves are unaffected by it |
| Envelope | for every list leaf (J3), stdout under a faked client parses to an object with `items` (array) and `next_before` keys |
| `--room` | present, unless in `ROOM_NA` (reason string) or `ROOM_PENDING` (server issue link) |
| `--yes` | present on every destructive verb |
| No `GROUP` | no positional dest/metavar `group`; no help string containing "group" or "channel" outside `RENAMED`. `--to` help lists `room`; the wire value `groups_service` is an accepted hidden choice and allowlisted |
| Renamed | each `RENAMED` stub exits 2 with one stderr line, empty stdout, and never reaches chat or the API |

`JSON_EXEMPT` and `ROOM_NA` are fixed lists with reasons. `ROOM_PENDING` and a temporary
`JSON_PENDING` start full and shrink each phase; a phase is done when its rows are gone.

### How to add a command (for Chris)

1. Pick the noun from §2. A new noun needs a §2 row with a product citation in the same PR.
2. Pick the verb from §3.1. A new verb needs a §3.1 row in the same PR.
3. Build the leaf with `parents=[_json_parent, …]`; add `_room_parent` (§6 class), `_yes_parent`
   if destructive, `_page_parent` if paged.
4. Room is a flag unless the room is the object. Canonical order in help/examples: `--room` first.
5. Output through `_emit` / `_emit_list`; the shape follows J3–J5. Changing a shipped shape is a
   breaking change (invariant 4) — separate PR, release note.
6. If the route is new: server PR first, wait for the snapshot refresh, then the CLI.
7. Update `SKILL.md` and `README.md` in the same PR (invariant 5). Run `uv run pytest`; the
   conformance test tells you what you missed.

## 9. Phases

One PR each. 2a–2d are breaking and file-disjoint enough to run in parallel after phase 1; no
release is tagged between the first of them merging and phase 3.

| Phase | PR | Contents | Depends on |
|---|---|---|---|
| 1 | Scaffolding | shared parent parsers, `_json_mode` + `PEPPER_JSON`, `_emit`/`_emit_list`, J8 error line, renamed-noun stub helper, conformance test with full pending lists. No user-visible change beyond `PEPPER_JSON` on today's `--json` leaves; releasable as 0.1.x | — |
| 2a | Agent surface | `rooms`, `topics`, `messages`, `attachments`; `groups` stub; J3 envelope on their lists; SKILL.md + README for them | 1 |
| 2b | Room admin | `members`, `sharing`, `apps list/install/uninstall` (#18 part 1), with envelope | 1 |
| 2c | Personal nouns | `trajectories` (+ `trajectory` stub), `create`/`delete` everywhere, skills `--file`, `scheduled-tasks edit` absorbs `destinations`, `--deliver-topic`, `--to room`, `agents guide` (+ `agent-skill` stub), `next --room/--source`, `renew` (#17) | 1 |
| 2d | `--json` fill | §5.4 list, envelope on `todos`/`notes`/`runs`/`thread`, §5.3 pagination, `apps show/run` (#18 part 2), `calendar --planning --room` | 1 (rebases after 2a–2c) |
| 3 | Release 0.2.0 | version bump, release notes with the §4 table; `JSON_PENDING` empty. Same day: merge the workspace skills PR and server `cli.md` PR, update running agents (§7) | 2a–2d |
| 4 | Server-dependent rooms | `scheduled-tasks delete` and one PR per other §6.1 row, as each server change and snapshot refresh lands; additive 0.2.x/0.3.x | server |

## 10. Decisions (2026-10-14, by Allen)

| | Question | Decision |
|---|---|---|
| A | Flat nouns with `--room` (`topics list --room R`) or nested under rooms (`rooms topics list R`)? | **Flat.** One room rule across every family. |
| B | `create` everywhere, or keep `add`/`new`? | **`create` everywhere**, with no `add`/`new` aliases (see C). |
| C | How long do old spellings live? | **No aliases at all.** "We haven't hit 1.0 and the main user of the CLI is agents so as long as we update the skill we should be fine." Hard break in 0.2.0 with the skills in lockstep (§7). |
| D | Normalize list output to one envelope, and when? | **Now, in 0.2.0**, with the renames: `{"items": [...], "next_before": …}` for every list (J3). |
| E | Should scheduled tasks be deletable? | **Yes**: `scheduled-tasks delete TASK --yes`, once the server endpoint exists (§6.1 #3). |
| F | `--json` at the root, or via an environment variable? | **Per-leaf `--json` everywhere plus `PEPPER_JSON=1`** (J10). No root `pepper --json`. |
