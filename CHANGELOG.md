# Changelog

All notable changes to `pepper` (distribution `vycari-pepper`). Newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/) (while 0.x, a minor bump may change flags or output).

## Unreleased

Part of the groups simplification (vycari/groups#249, phase 5).

### Breaking

`pepper next` is rebuilt from the todo list (Vycari/pepper-cli#50). Since pepper#4820 the schedule
plan's `plans` come back empty, so `next` showed an empty Do next, only email deadlines under Due
soon, a Later of 0, and ignored `--first`. Scripts that parse `pepper next --json` must be updated.

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
  IANA zone name (null when unreadable) and every time is shown in it. `now` is one event object or null (it was a
  list), `plan`, `do_next`, `due_soon` and `later` are gone, and `source` is always `"todos"` (it
  was `"schedule-plan"` or `"fallback"`). Each todo item is `{id, title, due, due_is_date_only,
  remind_at, priority, source}`, a subset of the server's todo fields with values unchanged
  (`due` is the server's `due_date`), in the server's order; the README has the details.

### Changed

- `pepper rooms create` now requires `--name`. Without one (or with a blank one) it fails locally,
  exit 2, with a message saying so, and sends nothing. The service answers an unnamed create with
  422 once groups phase 3 lands; this reports it first.
- `pepper rooms show` labels a room whose wire `kind` is `sealed` as `email/text thread`
  (it was `room`). The CLI still parses `sealed`; only the wording changed. The apps list these under
  "Other conversations".
- Help and README no longer describe an email or text thread as a "sealed room"; no flag or JSON
  field changed.

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
