# Changelog

All notable changes to `pepper` (distribution `vycari-pepper`). Newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/) (while 0.x, a minor bump may change flags or output).

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
