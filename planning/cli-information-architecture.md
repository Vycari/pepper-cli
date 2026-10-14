# CLI information architecture — one grammar, rooms everywhere, `--json` everywhere

**Status:** Proposed. Awaiting the maintainer's calls on §10 (A–F). The `groups` → `rooms` rename
is decided and is not an open question.
**Author:** drafted with Claude Code for the maintainer
**Last updated:** 2026-10-14
**Tracked by:** the "Implement planning/cli-information-architecture.md" issue linked from this
doc's PR
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
| 3 | The product says "room", the CLI says `groups`; the room is a positional `GROUP` in `groups` and a `--room` flag everywhere else | `groups *` vs `todos`/`notes`/`skills`/`scheduled-tasks` | §2, §3.1 |
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
| "channel" (help: "Room/channel name") | three meanings: SMS/email/web delivery (`reference/channels.md`); open vs sealed room (pepper#4556 "Channel (open) vs Room (sealed)"); "rooms/channels" in CLI help | as cited | **never used** in CLI help or flags. Open/sealed rooms say "open room"/"sealed room", as `_group_info_body.html` does |
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
| `notes` (kind `inline_text`) | "Notes" (sidebar, room "Notes board"); "Artifacts" on the settings page | `_sidebar.html`, `reference/artifacts.md`, `settings/_artifacts.html` | `notes`. Never "artifacts" in the CLI: the CLI only touches the note kind |
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
   `GROUP` survives. `--room` is required where the noun only exists inside a room (`topics`,
   `messages`, `attachments`, `members`, `sharing`) and optional elsewhere (omitted = yours).
3. **Canonical order puts `--room` right after the verb.** Harness allow rules are prefix matches
   (`Bash(pepper --profile claude-code messages post --room <room-id>:*)`), so the room must come
   before anything variable. Other orders still parse; they just don't match a pinned rule and get
   prompted, which fails safe. Pin by UUID, not name.
4. **Views and session commands are not noun–verb**: `calendar [WINDOW]`, `next`, `chat`, `login`,
   `logout`, `whoami`, `takeout`. That set is closed; anything new is a noun.

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

### 3.2 Create

`create` everywhere. `add` (todos, scheduled-tasks) and `new` (skills, notes, rooms, topics) become
hidden aliases (§7). The product's own buttons are split ("Start conversation", "New topic", "Add a
todo"), so there is no product verb to defer to; see §10 B.

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

`(A)` = hidden alias kept until removal (§7). `#17`/`#18` = the open pepper-cli issue that lands
there. Every new leaf gets `--json` (§5).

| 0.1.0 | New | Notes |
|---|---|---|
| `groups list [--archived]` | `rooms list [--archived]` (A) | |
| `groups show GROUP` | `rooms show ROOM` (A) | room is the object |
| `groups new --name PEOPLE` | `rooms create [--name N] PEOPLE…` (A) | |
| `groups rename GROUP NAME` | `rooms edit ROOM --name NAME` (A) | |
| `groups archive/unarchive GROUP` | `rooms archive/unarchive ROOM` (A) | |
| `groups leave GROUP --yes` | `rooms leave ROOM --yes` (A) | |
| `groups mute/unmute GROUP` | `rooms mute/unmute ROOM` (A) | |
| `groups watch --room --topic` | `rooms watch [--room R]… [--topic T]` (A) | already flag-scoped |
| `groups activity [GROUP] --since` | `rooms activity [--room R] [--since]` (A) | |
| `groups topics GROUP` | `topics list --room R [--archived]` (A) | |
| `groups new-topic GROUP NAME` | `topics create --room R NAME` (A) | |
| `groups rename-topic GROUP TOPIC NAME` | `topics edit --room R TOPIC --name NAME` (A) | |
| `groups archive-topic/unarchive-topic GROUP TOPIC` | `topics archive/unarchive --room R TOPIC` (A) | |
| `groups search QUERY` | `topics search [--room R] QUERY` (A) | §3.4 |
| `groups read GROUP [TOPIC]` | `messages list --room R [--topic T] [--limit --before --since --mark]` (A) | `--topic` optional as today (default topic) |
| `groups post GROUP TOPIC [MSG\|-]` | `messages post --room R --topic T [MSG\|-]` (A) | heredoc form: `pepper messages post --room R --topic T - <<'EOF'` |
| `groups edit GROUP MESSAGE TEXT` | `messages edit --room R MESSAGE [TEXT\|-]` (A) | |
| `groups delete-message GROUP MESSAGE` | `messages delete --room R MESSAGE --yes` (A) | |
| `groups react/unreact GROUP MESSAGE EMOJI` | `messages react/unreact --room R MESSAGE EMOJI` (A) | |
| `groups download GROUP ATTACHMENT` | `attachments download --room R ATTACHMENT [--out]` (A) | |
| `groups roster GROUP` | `members list --room R` (A) | |
| `groups invite GROUP HANDLE` | `members invite --room R HANDLE` (A) | |
| `groups remove GROUP PERSON` | `members remove --room R PERSON --yes` (A) | association, not delete |
| `groups role GROUP PERSON ROLE` | `members edit --room R PERSON --role owner\|member --yes` (A) | |
| `groups apps GROUP` | `apps list --room R` (A) | #18: `apps list` without `--room` = your apps |
| `groups install-app GROUP KEY` | `apps install --room R KEY` (A) | |
| `groups uninstall-app GROUP APP` | `apps uninstall --room R APP --yes` (A) | |
| — | `apps show SLUG [--room R]` | #18 "data", `GET /apps/{slug}/data?principal=` |
| — | `apps run SLUG [ACTION] [--room R] [--param k=v]…` | #18 "action"; no ACTION lists declared actions |
| `groups sharing GROUP` | `sharing show --room R` (A) | splits #9 |
| `groups sharing GROUP --set CAL=LEVEL` | `sharing edit --room R --set CAL=LEVEL…` (A) | |
| `trajectory show ID` | `trajectories show ID` (A) | |
| `trajectory list --limit --offset` | `trajectories list --limit --before` (A); `--offset` hidden | §5.3 |
| `todos add TITLE` | `todos create TITLE` (A) | |
| `todos list/done/dismiss/reopen` | unchanged | |
| `scheduled-tasks add NAME` | `scheduled-tasks create NAME` (A) | |
| `scheduled-tasks add/destinations --topic NAME` | `--deliver-topic NAME` (`--topic` hidden on this noun) | #13: `--topic` is reserved for scope |
| `scheduled-tasks destinations TASK` | `scheduled-tasks edit TASK --to/--default/--silent` (A) | |
| `scheduled-tasks list/show/runs/thread/edit/pause/resume` | unchanged names; `--room` per §6 | |
| — | `scheduled-tasks renew TASK [--room R]` | #17; prints the 422 refusal text on refusal |
| `skills new NAME` | `skills create NAME [--file]` (A) | |
| `skills list/show/edit/delete/share/revoke` | unchanged; `edit` gains `--file` | |
| `notes new NAME` | `notes create NAME` (A) | |
| `notes rm NOTE` | `notes delete NOTE --yes` (A) | |
| `notes list/search/show/edit` | unchanged; `edit --rename` → `--name` (A) | |
| `agent-skill [--write DIR]` | `agents guide [--write DIR]` (A) | #10 |
| `next --room PREFIX` | `next [--room R] [--source PREFIX]` | `--room` resolves strictly like everywhere else; prefix filtering moves to `--source`. Behavior change on an existing flag: release-noted |
| `calendar`, `chat`, `login`, `logout`, `whoami`, `takeout` | unchanged | views/session (§3 rule 4) |

## 5. The `--json` contract

This is the section implementations are tested against. Invariant 4 (`.claude/guidelines/
invariants.md`) makes stdout and exit codes an interface; this contract adds to it and changes
none of the shapes already shipped.

### 5.1 Rules

| # | Rule |
|---|---|
| J1 | Every leaf except `chat` with no message (the REPL) and `login` accepts `--json`. `logout` emits `{"ok": true}`. |
| J2 | Under `--json`, stdout carries exactly one JSON document (or JSON lines for a stream, J6) and nothing else: no colour, no prose, no progress. Prompts never happen (destructive verbs need `--yes`, else exit 2). |
| J3 | **Shapes shipped in 0.1.0 are frozen.** `rooms list` emits the `groups list` object `{enabled, degraded, total, archived_total, groups}` — the key stays `groups`. `messages list` emits `{messages, next_before}`. `messages post` emits the message plus `topic_id`. `notes list/search` emit `{items, truncated}`. `todos list`, `topics list`, `members list`, `apps list --room`, `rooms activity`, `topics search` emit bare arrays. An alias and its new spelling emit byte-identical stdout. Normalizing these is a 1.0 question (§10 D), never a quiet change. |
| J4 | **New list leaves** (no `--json` in 0.1.0): unpaged lists emit a bare array of objects (matching the 0.1 majority); paged lists emit `{"<noun>": [...], "next_before": str\|null}` (matching `messages list`). `trajectories list` → `{"trajectories": [...], "next_before": …}`; `scheduled-tasks runs/thread` likewise. |
| J5 | `show` emits the object. Mutations emit the resulting object as the server returns it; for a 204 route, `{"ok": true, "id": "<id acted on>"}`. `takeout` emits `{"path", "bytes"}`. `agents guide --write` emits `{"path"}`. |
| J6 | Streams emit JSON lines: `rooms watch` (unchanged, incl. `{"kind":"cursor","cursor":N}` frames) and one-shot `chat MESSAGE --json` (one line per streamed event, final line the reply). |
| J7 | Field names are the server's, passed through. Unknown server fields are kept, not dropped (the CLI already tolerates them). |
| J8 | **Errors** under `--json` write one line to **stderr**: `{"error": {"code": "...", "status": <http int or null>, "message": "..."}}`. `code` ∈ `auth`, `forbidden`, `not_found`, `conflict`, `validation`, `ambiguous`, `usage`, `upgrade_required`, `server`, `network`, `gap`. stdout stays empty. Without `--json`, errors stay `pepper: …` text. |
| J9 | Exit codes unchanged: 0 ok, 1 auth/API/network (incl. 426), 2 usage/resolution/refused-without-`--yes`, 3 watch replay gap, 130 interrupt. |

### 5.2 Mechanism

argparse does not propagate a root optional into subparsers, so `pepper --json rooms list` and
`pepper rooms list --json` are different parsers. Per-leaf it is: one shared parent parser per
common flag, built once with `add_help=False` and attached with `parents=[...]` to every leaf.

| Parent | Adds | Attached to |
|---|---|---|
| `_json_parent` | `--json` | every leaf except J1's two |
| `_room_parent(required)` | `--room ROOM` (UUID or case-insensitive unique name prefix, resolved by `_resolve_group`) | per §6 |
| `_topic_parent` | `--topic TOPIC` | `messages list/post`, `rooms watch` |
| `_yes_parent` | `--yes` | destructive verbs (§3.1) |
| `_page_parent` | `--limit N`, `--before CURSOR`, `--since CURSOR` | paged lists |

Output goes through one `_emit(obj, args)` that writes JSON or calls the existing `render.py`
human formatter, and one error path that honours J8. Bare `print` stays confined to `cli.py`.

### 5.3 Pagination and `--since`

`--limit N`; `--before CURSOR` for older; `--since CURSOR` for newer. A cursor is whatever the same
command printed as `next_before` (or `cursor`, for `watch`) and is opaque. `trajectories` is
offset-paged on the server: the CLI encodes the offset as the cursor and keeps `--offset` as a
hidden alias. `rooms activity --since` also accepts an ISO-8601 time, because that is what its
route takes; help says so.

| Command | Paged by | `--before` | `--since` |
|---|---|---|---|
| `messages list` | server cursor | yes | yes |
| `trajectories list` | offset, as cursor | yes | no |
| `scheduled-tasks runs/thread` | server `before`/`before_id` | yes | no |
| `rooms watch` | event cursor | no | yes (+ `--cursor-file`) |
| `rooms activity` | time | no | yes (time) |

### 5.4 Fill list (`--json` missing in 0.1.0)

`whoami`, `chat MESSAGE`, `trajectories list`, `todos create/done/dismiss/reopen`, all of
`scheduled-tasks`, all of `skills`, `rooms show/create/edit/archive/unarchive/leave/mute/unmute`,
`topics create/edit/archive/unarchive`, `messages edit/delete/react/unreact`, `attachments
download`, `members invite/remove/edit`, `apps install/uninstall/show/run`, `notes create/edit/
delete`, `takeout`, `logout`, `agents guide`. All client-only.

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
| `scheduled-tasks delete` | optional | S | no endpoint, personal or room (§10 E) |
| `skills list/show/share/revoke` | optional / optional / required / required | R | `/groups/{gid}/skills…` |
| `skills create/edit/delete` | — | N | a room holds a grant of someone's skill, not a skill; `share` is the room verb |
| `notes *` | optional | R | `/artifacts…?principal=`, `/notes/{id}…?principal=` |
| `trajectories list/show` | optional | S-verify | `/trajectories` has no `principal`; help claims room-shared ones list — verify, else add `?principal=` |
| `calendar --planning` | optional | C | `/calendars/planning?principal=` |
| `calendar` (range) | optional | S | `/calendars/range` has no `principal`; or defer to `apps show calendar --room` |
| `next` | optional | C | strict resolve, filter by room id (§4) |
| `chat`, `login`, `logout`, `whoami`, `takeout`, `agents guide` | — | N | talking to Pepper in a room is `messages post --mention` |

### 6.1 Server needs (not filed; for the maintainer to file or fold in)

| # | Need | Existing issue |
|---|---|---|
| 1 | Room task detail: `GET /groups/{gid}/scheduled-tasks/{id}`, `/runs`, `/thread` — **verify personal routes first** | pepper#4311 (slice 1 shipped; this is beyond it) |
| 2 | Room task full edit (`prompt`, `cron`, `timezone`, `description`), one-off `at` on create, and `renew` | new |
| 3 | Scheduled-task delete, personal and room — product decision first | new |
| 4 | `GET /trajectories?principal=` and `/{id}` — **verify** room-shared listing first | new |
| 5 | `GET /calendars/range?principal=` | new (or defer to #18) |
| 6 | Room todo `details`/`priority` — **verify** honoured | new if not |
| 7 | Skill-backed room task | pepper#4557 |
| 8 | Join links (would land as `rooms links …`, spelled when the route exists) | pepper#4311 |
| 9 | Typed response models for groups routes — lets J3/J4 shapes be checked against the contract | pepper#4673 |
| 10 | `whoami` returns 401 for agent tokens, so `whoami --json` cannot be the agent login check | note only |

Order per `CLAUDE.md`: server lands → weekly snapshot refresh of `contracts/api-v1.openapi.json`
(never hand-edited) → CLI.

## 7. Compatibility

| Item | Rule |
|---|---|
| Aliases | Every 0.1.0 spelling in §4 marked (A) stays registered as a hidden subparser with its **old signature**, mapping positional `GROUP` onto `--room`, and dispatches to the new handler. Stdout is byte-identical to the new spelling (J3). |
| Implicit chat | Old and new nouns are all in the set `_build_parser()` returns, so `groups`, `trajectory`, `agent-skill` never fall through to chat, during or after the window. After removal they exit 2 with "renamed to …", not chat. |
| Notice | One stderr line per invocation: `pepper: 'groups read' is deprecated; use 'pepper messages list --room …' (removed in 1.0.0)`. Never on stdout, never inside `--json`. `PEPPER_NO_DEPRECATION=1` silences it. |
| Window | Through 0.x; removed in **1.0.0**, and not before three minor releases have shipped with the new spellings (§10 C). The real blast radius is harness allow rules like `Bash(pepper --profile X groups post <id>:*)`, which stop matching the moment an alias goes. |
| Lockstep | The renaming PR updates, together: bundled `src/pepper_cli/data/agent-skill/SKILL.md` (§2–§10, incl. §8's eight agent commands → `rooms list`, `topics list`, `members list`, `messages list`, `rooms watch`, `messages post`, `messages react`, `messages unreact`, and §10 listing the deprecation line as expected stderr); `README.md`. Same day, separate repos: workspace `plugins/vycari-ops/skills/{access-trajectory,pepper-groups,room-agent}/SKILL.md` (incl. room-agent allow rules lines 96–102, rewritten to `messages post --room <room-id>:*` etc.), and server `src/pepper/features/help/skills/pepper-guide/reference/cli.md`. |

## 8. Conformance test and how to add a command

`tests/test_cli_conformance.py` builds the real parser and walks every subparser recursively
(`parser._subparsers._group_actions[*].choices`, then each leaf's `_option_string_actions`).

| Check | Assertion |
|---|---|
| Noun | top-level name ∈ §2 nouns ∪ views/session set ∪ `ALIASES` |
| Verb | leaf name ∈ §3.1 verbs |
| `--json` | present, unless in `JSON_EXEMPT` (`chat` REPL path, `login`) |
| `--room` | present, unless in `ROOM_NA` (reason string) or `ROOM_PENDING` (server issue link) |
| `--yes` | present on every destructive verb |
| No `GROUP` | no positional dest/metavar `group`; no help string containing "group" or "channel" outside `ALIASES` |
| Aliases | every 0.1.0 path still parses, prints the notice on stderr, and dispatches to the same handler as its new spelling |
| Byte identity | for each alias with `--json`, stdout under a faked client equals the new spelling's |

`JSON_EXEMPT` and `ROOM_NA` are fixed lists with reasons. `ROOM_PENDING` and a temporary
`JSON_PENDING` start full and shrink each phase; a phase is done when its rows are gone.

### How to add a command (for Chris)

1. Pick the noun from §2. A new noun needs a §2 row with a product citation in the same PR.
2. Pick the verb from §3.1. A new verb needs a §3.1 row in the same PR.
3. Build the leaf with `parents=[_json_parent, …]`; add `_room_parent` (§6 class), `_yes_parent`
   if destructive, `_page_parent` if paged.
4. Room is a flag unless the room is the object. Canonical order in help/examples: `--room` first.
5. Output through `_emit`; pick the shape from J3–J5. A new shape for an existing command is a
   breaking change (invariant 4) — separate PR, release note.
6. If the route is new: server PR first, wait for the snapshot refresh, then the CLI.
7. Update `SKILL.md` and `README.md` in the same PR (invariant 5). Run `uv run pytest`; the
   conformance test tells you what you missed.

## 9. Phases

One PR each. Phases 2a/2b/3 are file-disjoint enough to run in parallel after phase 1.

| Phase | PR | Contents | Depends on |
|---|---|---|---|
| 1 | Scaffolding | shared parent parsers, `_emit` + J8 error line, alias registry + notice, conformance test with full pending lists. No user-visible rename | — |
| 2a | Agent surface | `rooms`, `topics`, `messages`, `attachments` + aliases; SKILL.md, README; then workspace skills and server `cli.md` PRs | 1 |
| 2b | Room admin | `members`, `sharing`, `apps list/install/uninstall` (#18 part 1) + aliases | 1 |
| 3 | Personal nouns | `trajectories`, `create`/`delete` aliases, skills `--file`, `scheduled-tasks edit` absorbs `destinations`, `--deliver-topic`, `--to room`, `agents guide`, `next --room/--source`, `renew` (#17) | 1 |
| 4 | `--json` fill | §5.4 list, §5.3 pagination flags, `apps show/run` (#18 part 2), `calendar --planning --room` | 2a, 2b, 3 |
| 5 | Server-dependent rooms | one PR per §6.1 row as each server change and snapshot refresh lands | server |
| 6 | 1.0.0 | remove aliases; §10 D outcome | ≥3 minors after 2a |

## 10. Open questions

| | Question | Recommendation |
|---|---|---|
| A | Flat nouns with `--room` (`topics list --room R`) or nested under rooms (`rooms topics list R`)? | **Flat.** One room rule across every family; six of seven families already scope with `--room`; nesting keeps a positional room and two rules. |
| B | `create` everywhere, or keep `add`/`new` where they are? | **`create`**, with `add`/`new` as aliases to 1.0. The product's buttons are split (Start/New/Add), so there is nothing to match; one verb is what Chris can apply without asking. |
| C | Alias removal: when? | **1.0.0, and no sooner than three minor releases after phase 2a**, and only once the workspace skills and server `cli.md` ship the new spellings. |
| D | At 1.0, normalize every list to one envelope (`{"items": [...], "next_before": …}`), or freeze J3/J4 forever? | **Normalize at 1.0**, in the same release as alias removal, so agents pay one breaking change, not two. |
| E | Should scheduled tasks be deletable (today only pausable; "cancel" = pause)? | **Yes**, `scheduled-tasks delete TASK --yes`, after a server endpoint (§6.1 #3). Product call first. |
| F | Accept `--json` at the root too (`pepper --json …`) or via `PEPPER_JSON=1`? | **No, per-leaf only** for now. Root acceptance needs a pre-parse like `--profile`; revisit if agents ask. |
