# Research projects from the terminal — `research-projects` and `research-items`

**Status:** Proposed. The command shape and decisions 1–7 in §1.2 were taken by the maintainer
(Allen) on 2026-10-20; the questions in §13 are open.
**Author:** drafted with Claude Code for the maintainer
**Last updated:** 2026-10-20
**Tracked by:** #85
**Related:** `planning/cli-information-architecture.md` (the grammar; this doc amends §2, §3, §3.1,
§5, §6 and §8 there, in the same PR), `planning/cli-core-coverage.md` (§4.2 numbering, §6 rules);
server: pepper `planning/research-projects-api.md` (in progress, the routes this waits on), pepper
`planning/iterative-research.md`, pepper `planning/manual-research-projects.md`, pepper#3585
(reconsider), pepper#4720 (Dash research cards). Server paths below are relative to `src/pepper/`
in Vycari/pepper at `origin/main`; CLI paths are relative to `src/pepper_cli/`.

## 1. Scope

### 1.1 What a research project is (server, today)

A research project (`platform/db/models/research.py` `ResearchProject`) has a `name` (unique per
owner; the chat tool asks for "short, lowercase and hyphenated"), a `brief`, a `skill_name`
(non-nullable), a `status` (`active`, `paused`, `stopped`), a `delivery_policy` (`drip` or
`digest` in the chat tool; the DB constraint also allows `alert`), `filters` (a list, replaced
whole), a report (an `inline_text` artifact owned by the project's principal, named
`research:{name}:report`, sections Summary / Current recommendation / Shortlist / What's new /
Open questions / Sources, `agent/research/report.py`), an optional schedule (the scheduled task
`research:{name}`; a `NULL` pointer is the manual-only state) and an optional `source_url`.
`owner_id` is a principal, so a room-owned project needs no schema change.

Ledger items (`ResearchItem`) have a state (`candidate`, `accepted`, `rejected`, `delivered`,
`taken`, `superseded`, `retracted`) and a `seq`. **`seq` is a globally monotonic identity column,
not a per-project counter** (the model's docstring: it mirrors `FeedItem.ingest_seq`), which
matters for item references (§5.2). The owner can mark a delivered item taken and bring a rejected
item back (`reconsider`, with a reason). Manual runs are asynchronous receipts (`queued`,
`running`, `succeeded`, `error`, `interrupted`); each iteration is a row (`ResearchIteration`).
Steering is queued text, optionally answering an open question `q1`–`q3`, which the next
iteration reads.

Today the only interface is the chat tool (`agent/tools/research_projects.py`, actions `create`,
`run_manual`, `enable_schedule`, `steer`, `set_filters`, `reconsider`, `mark_taken`, `pause`,
`resume`). There are no HTTP routes. The personal feature is gated by the deployment setting
`research_projects_enabled`; room projects by `research_group_projects_enabled` (ready, off).

### 1.2 Decisions already taken (Allen, 2026-10-20)

| # | Decision | Where |
|---|---|---|
| 1 | Noun `research-projects` (product word, plural, like `scheduled-tasks`); ledger items are their own noun `research-items` with a new `--project` container flag, written as a general IA rule | §3, IA §3 rule 2 |
| 2 | `run` returns the receipt at once. `--wait` is a new, general IA convention | §6, IA §3.5 |
| 3 | A run started over the API delivers to the project only; the CLI reads back with `show`, `show --report` and `runs` | §6.4 |
| 4 | Enabling a schedule (first `edit --cron`) prompts y/N on a terminal and needs `--yes` otherwise (exit 2), because it commits to recurring spend. Whether a change to an existing schedule goes through `scheduled-tasks edit` is decided here | §7 |
| 5 | Feature switched off → J8 `forbidden`, with a clear message | §8 |
| 6 | No `delete` or `stop` in v1 | §3 |
| 7 | `show --report` reads the report artifact (precedent: `feed show --transcript`) | §4.2 |
| — | Rooms are in scope: every verb on both nouns takes `--room ROOM` | §3, §9 |

One correction to the brief: decision 2 cites J4 for the receipt. J4 is the rule for views
(`next`, `calendar`); a receipt is what a mutation returns, which is **J5** ("mutations emit the
resulting object as the server returns it"). The design follows J5.

### 1.3 Ordering

Server first. Every route, field and status code below is a requirement on pepper
`planning/research-projects-api.md`, marked **(pending the server doc)** where this doc needs a
specific answer from it. Nothing here invents a route path beyond the two prefixes that doc has
announced (`/api/v1/research-projects/...` and `/api/v1/groups/{gid}/research-projects/...`).
The CLI phases (§12) start after the routes are deployed and the weekly refresh has put them in
`contracts/api-v1.openapi.json` (`CLAUDE.md`: server, snapshot, then CLI).

## 2. Nomenclature

| Word | Product evidence | CLI |
|---|---|---|
| "research project" | The help guide's own section "Research projects" (`features/help/skills/pepper-guide/reference/skills.md`: "A research project keeps a brief, a ledger of what Pepper found, a report, and your steering notes across one or more runs"); Dash: "A research project gets one card whenever anything in it is new" (`reference/dash.md`); the tool title "Create, run, steer, pause or resume a research project" | **`research-projects`** |
| ledger item | "a ledger of what Pepper found", "a per-item ledger" (`reference/skills.md`); the tool's `item` parameter ("the rejected item", "the delivered item the person says they had") | **`research-items`**. "item" alone would be too generic for a top-level noun; the prefix ties it to its parent the way `scheduled-tasks` is qualified |
| run, receipt | "Ask Pepper to run it … it gives you a receipt and works asynchronously" (`reference/skills.md`) | `run`, `runs` |
| steer | "your steering notes" (`reference/skills.md`); the tool action `steer` | `steer` |
| taken | the tool action `mark_taken` ("records that item as taken"); state `taken` | `take` (the verb of "taken") |
| reconsider | "it goes back on the list to be considered again" (`reference/skills.md`); the tool action `reconsider` | `reconsider` |
| report, What's new, Open questions | report section headings (`agent/research/report.py`) | `show --report`; "What's new" printed after `run --wait` |

No web template or iOS screen names research projects yet (they are chat-only), so the citations
are the help guide and the tool schema, which are the product text a person reads today. If a
screen lands with different words, the screen wins and this table changes.

## 3. Commands

Canonical order is the IA's: `--room` right after the verb (rule 3), then `--project` (the new
rule 2 amendment, §3.1), then the positional object, then flags.

```
pepper research-projects list      [--room R] [--status active|paused|all]
pepper research-projects show      [--room R] PROJECT [--report]
pepper research-projects create    [--room R] NAME [--file BRIEF|-] [--skill S] [--policy drip|digest] [--source-url URL] [--yes]
pepper research-projects edit      [--room R] PROJECT [--file BRIEF|-] [--filter F]… [--clear filters] [--cron C [--timezone TZ]] [--yes]
pepper research-projects run       [--room R] PROJECT [--wait [--timeout SECONDS]] [--yes]
pepper research-projects steer     [--room R] PROJECT [TEXT…|-] [--question QN] [--yes]
pepper research-projects pause     [--room R] PROJECT
pepper research-projects resume    [--room R] PROJECT
pepper research-projects runs      [--room R] PROJECT [--limit N] [--before CURSOR]

pepper research-items list        [--room R] --project P [--state S]… [--limit N] [--before CURSOR]
pepper research-items show        [--room R] --project P ITEM
pepper research-items take        [--room R] --project P ITEM
pepper research-items reconsider  [--room R] --project P ITEM [--reason TEXT]
```

Every leaf takes `--json` (J1) and honours `PEPPER_JSON` (J10). Changes from the decided shape,
each forced by an IA rule or a server fact:

| Change | Why |
|---|---|
| `--room R` on every leaf | rooms are in scope; IA rule 3 order |
| `create … --yes` | only for `--room` (§9.2): creating a room project shows it to every member |
| `edit --file BRIEF\|-` (adds `-`) | IA §3.3: every `--file` takes `-` for stdin |
| `edit` with no field flag opens `$EDITOR` on the brief | IA §3.3: the brief is the noun's body; with no terminal it is exit 2 |
| `--timezone` only with `--cron` | it has no meaning on its own; usage error otherwise |
| `run --timeout` | §6.1: `--wait` needs a bound |
| `steer --yes`, `run --yes` | room confirmations (§9.2); accepted and ignored on a personal project |
| `research-items show/take/reconsider` take `ITEM`, not `N` | `ITEM` is a number from the last listing **or** an item id (§5.2); the brief's `N` is the common case of it |
| `reconsider --reason TEXT` | the server's `reconsider` takes a reason ("why it deserves another look"); optional and sent as given, like `todos dismiss --reason` |

`--policy` choices are fixed at `drip|digest` in argparse to give a usage error before a round
trip. If the server doc exposes `alert` (the DB allows it), the choice list grows then.

Not in v1 (decision 6): `delete`, `stop`. No `research-projects search`: `list` is short.

### 3.1 The container flag (`--project`)

Today `--room` (and `--topic` inside a room) are the only containers. `research-items` needs a
second one: an item exists only inside a project, the server's routes are project-scoped, and an
item reference (a list number, §5.2) means nothing without its project. The IA amendment
(IA §3 rule 2) is written as a general rule, not a one-off:

- **When.** A non-room container flag is allowed only when the object is addressable solely
  through a parent that is itself a §2 noun. The flag is spelled `--<parent noun, singular>`
  (`--project`), takes what the parent noun's own positional takes (name, unique prefix, UUID),
  and is resolved by the parent noun's own resolver.
- **Required.** It is required on every verb of the child noun. There is no "yours" default for a
  container: a ledger with no project is not a thing.
- **Order.** Canonical order is `<noun> <verb> [--room R] [--project P] [OBJECT…] [flags]`.
  `--room` scopes the container (it says whose project), never the item directly; it stays first
  because harness allow rules pin the room. Other orders still parse (rule 3: they just do not
  match a pinned prefix rule).
- **With `--room`.** `--project` is resolved inside the room `--room` names (a room's project
  names are its own namespace, as a room's task names are, `_resolve_task_id`). Without `--room`
  it is resolved among your own projects. A project UUID passes straight through either way, and
  the server refuses one that is not in that scope.
- **Help and conformance.** Help lines and examples print `--room` before `--project`; the
  conformance test checks the usage string's order (§11).

Nested sub-nouns (`research-projects items list P`) were rejected for the same reason the IA
rejected `rooms topics list` (IA §10 A): one flat grammar, every child its own noun.

## 4. Output

Human output sketches below; exact column widths are the implementation's. All JSON is the
server's fields passed through (J7), plus the CLI's `n` on numbered lists (§5.2).

### 4.1 `research-projects list`

```
NAME              STATUS   POLICY  SCHEDULE            LAST RUN                ITEMS
robot-vacuums     active   digest  Sun 09:00 PT        2026-10-19 succeeded    4 new · 12 accepted
horn-walk         active   drip    manual              2026-10-12 error        0 new · 3 delivered
monitor-research  paused   digest  manual              2026-09-30 succeeded    —
```

JSON (J3): `{"items": [<project>, …], "next_before": null}`; unpaged unless the server pages
(pending the server doc). `--status` is sent to the server (default `active`, as
`scheduled-tasks list` hides paused tasks without `--all`; `all` includes `paused` and `stopped`).
The CLI does not filter (`cli-core-coverage.md` §6).

### 4.2 `research-projects show`

```
robot-vacuums   active · digest · skill product-research
Brief       Robot vacuums under $500 that handle pet hair and map multiple floors.
Filters     1. nothing over $500   2. no subscription-only models
Schedule    every Sunday 09:00 (America/Los_Angeles) — research:robot-vacuums
Source      (none)
Last run    2026-10-19 09:00 succeeded (4 new candidates)
Ledger      2 candidate · 12 accepted · 5 rejected · 3 delivered · 1 taken
Steering    1 note queued for the next run
Open        q1  Is a self-emptying base a must-have?
            q2  Should refurbished units count?
Report      pepper research-projects show robot-vacuums --report
```

JSON (J5): the project object. `show --report` prints the report's markdown body raw to stdout,
as `notes show` does, and under `--json` emits the artifact detail object `artifacts show --json`
emits. The open questions and ledger counts come from the server (pending the server doc, §10
R4); the CLI does not parse them out of the report.

### 4.3 `research-projects runs`

```
STARTED            TRIGGER    STATUS       NEW  NOTE
2026-10-19 09:00   scheduled  succeeded     4
2026-10-15 14:02   manual     error         0   page fetch failed (source_url 503)
2026-10-12 08:30   manual     succeeded     2
```

JSON (J3): `{"items": [<run>, …], "next_before": <cursor|null>}`, paged backwards with
`--before` like `scheduled-tasks runs` (IA §5.3). What a row is (receipt, iteration, or one merged
record) is decision F.

### 4.4 `research-items list`

```
robot-vacuums — 22 items (states: all)
 1  candidate  Roborock Q7 Max+            roborock.com          first seen 2026-10-19
 2  candidate  Eufy X10 Pro Omni           eufy.com              first seen 2026-10-19
 3  accepted   Coway Airmega 150 …
 …
```

The numbers are the row's place in the list as printed, in the server's order (§5.2). JSON (J3):
`{"items": [{"n": 1, …item}, …], "next_before": <cursor|null>}`, the same `n` shape `artifacts list`
emits (`numbered_view.number`).

### 4.5 `research-items show`, `take`, `reconsider`

`show` prints title, state and `state_reason`, link, summary, citations, first seen, state
changed, delivered (`published_at`). JSON (J5): the item object. `take` and `reconsider` print one
line (`Marked 'Coway Airmega 150' taken.`, `'Roborock Q7' is a candidate again.`) and emit the
item as the server returns it (J5); a refused transition (an item not `delivered`, or not
`rejected`) is the server's 409/422 with its own detail, printed as is.

## 5. Resolving references

### 5.1 Projects

`PROJECT` (and `--project`) is a name, a case-insensitive unique name prefix, or a UUID, the
contract `_resolve_task_id` and `_resolve_group` already follow, using `_match_by_name` (exact
name wins outright; else a unique prefix; else `ambiguous` or `not_found`, exit 2). A UUID passes
straight through. The lookup lists the scope's projects with every status (as `_resolve_task_id`
includes paused tasks, because `resume` is the verb most often reached by name). With `--room` the
lookup is that room's projects only. Names are unique per owner on the server, so an exact match
is never ambiguous; prefixes can be.

### 5.2 Items: numbered refs, not `seq`

`ITEM` is a number from the last `research-items list`, or an item id or a unique id prefix of an
item that listing held: `numbered_view.NumberedView` exactly as `artifacts` and `booking-pages`
use it, state file `research-items.json` beside the credentials.

**Why not the server's `seq`.** The brief assumed a per-project `seq`; on the server it is global
(§1.1). It would be stable and shared between people, which is what makes it attractive, but it
grows past four digits within weeks across the fleet, at which point `numbered_view`'s rule
("longer than four digits, or not all digits, is an id") reads it as an id prefix, and a person
typing `show 48213` for a project with 20 items is a poor interface. A per-project ordinal from
the server would fix both; it is decision A.

**Scoping a saved listing.** `save()` records `meta = {"project_id", "room_id"}` (room id `null`
for personal). `show/take/reconsider` resolve `--room` and `--project` first and refuse a number
whose saved meta names a different project or room: `pepper: the last 'research-items list' was
of another project — list this one first`, J8 `usage`, exit 2. A number never resolves against
the wrong ledger. An id or id prefix is accepted without that check (the server refuses one that is
not in the project). `take` and `reconsider` act on a number only after the check, which is the
same safeguard `dash do` gets from a confirmation (`cli-core-coverage.md` §6), without a prompt:
both are reversible by the owner and reach no one.

`logout` and `login` forget the file (`numbered_view.forget`), as they do for the other numbered
nouns.

## 6. `run` and the `--wait` convention

### 6.1 `--wait` (general; IA §3.5)

`--wait` is a new IA convention for any verb that starts asynchronous server work and gets back a
receipt. `research-projects run` is the first; `brief --now` could adopt it later (it queues a
run today and does not wait; it would need a receipt to poll first). The rules:

| Aspect | Rule |
|---|---|
| Without `--wait` | Print the receipt and exit 0 as soon as the server accepts (J5 under `--json`) |
| Polling | `GET` the receipt (pending the server doc) at 2 s, growing ×1.5 per poll to a 15 s cap; reset to 2 s on a status change. A `Retry-After` from the server overrides the interval |
| Bound | `--timeout SECONDS`, default 900 (15 min), `0` = no bound. On expiry the command stops waiting; the work goes on |
| Progress | Human mode: one status line on stderr (`queued` → `running`, elapsed time), rewritten in place on a terminal, one line per change off a terminal. JSON mode: nothing at all until the end (J2), and never JSON lines: `--wait` is not a stream (J6) |
| End: succeeded | exit 0. stdout: the final receipt (JSON), or in human mode the receipt line and then the run's "What's new" lines (§6.3) |
| End: run failed (`error`, `interrupted`) | stdout: the final receipt, the same document as on success (it is the answer). stderr: a J8 line, code `run_failed`. exit 1 |
| End: timeout | stdout: the last receipt seen (status `queued`/`running`). stderr: a J8 line, code `timeout`, message naming the receipt id and the `runs` command. exit 4 |
| Ctrl-C | Stops waiting, never cancels the run. stderr: `pepper: still running — check 'pepper research-projects runs P'` (human mode) or a J8 line, code `timeout` (JSON mode). stdout empty. exit 130 (J9) |
| Poll errors | A network error or 5xx while polling is retried within the bound; a 401/403/404 ends the wait through the leaf's normal error path (exit per J9) |

The J8 `code` list is closed today (`_ERROR_CODES` raises on an unknown one), and J9 has no "gave
up waiting" exit code. So this adds two J8 codes and one J9 exit code, as an IA amendment. Writing
a receipt to stdout on a failed run departs from J8's "stdout stays empty": the receipt is the
result the caller asked for, and the J8 line still tells an agent that the run failed. Decision B
lists the alternative (no new codes).

### 6.2 `run` without `--wait`

```
$ pepper research-projects run robot-vacuums
Queued run 7f3a… for robot-vacuums. Check it with: pepper research-projects runs robot-vacuums
```

The server refuses a second run while one is unverified ("Pepper will not start another run while
it cannot verify that an earlier worker stopped", `reference/skills.md`); that refusal (409, its
own detail) prints as is, J8 `conflict`, exit 1.

### 6.3 Reading back (decision 3)

A run started over the API delivers to the project only: its ledger and report change, nothing is
posted to a conversation. So the CLI's read-back is `show` (status, counts, last run), `show
--report` (the document) and `runs` (history). After `run --wait` succeeds the human output ends
with the "What's new" lines that run wrote:

```
Run 7f3a… succeeded in 3m12s.
What's new
  2026-10-20  Two new candidates under $450; the Eufy X10 dropped to $499.
  2026-10-20  Coway Airmega 150 rejected: subscription-only filters.
```

These come from the server as structured data on the finished receipt (pending the server doc,
§10 R6). The CLI does not cut the `## What's new` section out of the report's markdown: that is
parsing a server-owned document format into meaning, which belongs to the server
(`cli-core-coverage.md` §6). If the receipt carries no such field the CLI prints `Read the report:
pepper research-projects show P --report` instead.

## 7. Schedules

`research-projects edit P --cron C [--timezone TZ]` sets the project's schedule. `--timezone`
defaults to the server's default (the person's profile timezone in the tool today).

**Enabling** (the project is manual-only: no schedule yet) commits to recurring spend, so it
prompts on a terminal (`Run robot-vacuums every time '0 9 * * 0' matches (America/Los_Angeles)?
Each run uses Pepper on your account. [y/N]`) and needs `--yes` otherwise (exit 2, J8 `usage`,
J2: never a prompt under `--json`). Whether the project has a schedule comes from the server, not
from the CLI's memory, and the server should enforce consent itself (§10 R5): the enable request
carries an explicit confirmation field, the CLI sets it only after the prompt or `--yes`, and the
server refuses an enable without it. Then a stale read or another client cannot skip the
question. The chat path's 15-minute confirmation token is for a model in a conversation and is
not a fit for a PAT client; the server doc decides whether the API path reuses it.

**Changing an existing schedule** also goes through `research-projects edit --cron`, not
`scheduled-tasks edit research:NAME`. Decided here (decision 4), for three reasons:

1. The `research:` prefix is reserved (`agent/scheduled_tasks/reserved_names.py`): the task is how
   the project finds its own row, and the project owns it. A client that edits the feature's task
   behind the feature's back is the collision that file exists to stop.
2. The schedule rules are the project's: the tool requires a skill that knows what to do each run,
   and the project's `pause`/`resume` and the task's are different switches. One door keeps them
   consistent.
3. An agent learns one place for a project's settings.

`scheduled-tasks list/show/runs` still show `research:NAME` (it is a task), and `scheduled-tasks
edit` on it keeps doing whatever the server allows today; refusing it is the server doc's call,
not the CLI's. Changing an existing personal cadence does not prompt: the commitment to recurring
spend was made at enable time. (In a room every schedule change prompts, §9.2.)

**Turning a schedule off** is not in the decided shape. Today it is `scheduled-tasks delete
research:NAME --yes`, which makes the project manual-only (`api/scheduled_tasks.py`, the delete
route's docstring) and discards the task's run history. Decision C.

## 8. Errors

| Situation | J8 `code` | Exit | Message (human mode) |
|---|---|---|---|
| Research projects switched off on the server (`research_projects_enabled`) | `forbidden` | 1 | `Research projects are not enabled on this Pepper server.` |
| Room research projects switched off (`research_group_projects_enabled`), any `--room` leaf | `forbidden` | 1 | `Research projects in rooms are not enabled on this Pepper server.` |
| Rooms not enabled for the account | `forbidden` | 1 | the existing `Rooms are not enabled for your account.` (`_GroupsDisabledError`) |
| Not allowed in that room (role) | `forbidden` | 1 | `_report_room_error`: `not allowed: <server detail>` |
| No such room/project/item, or not a member (`--room`, server 404) | `not_found` | 2 | `_report_room_error`: `not found, or you are not a member of that room` |
| Personal server 404 | `not_found` | 1 | `_report_api_error`, as `scheduled-tasks` does |
| Name or number does not resolve | `not_found` / `ambiguous` / `usage` | 2 | §5 |
| Prompt needed and no terminal or `--json` | `usage` | 2 | `pass --yes to …` |
| Refused transition, run already in flight, taken name | `conflict` / `validation` | 1 | the server's own detail |

The flag-off messages say "on this Pepper server", not "for your account":
`research_projects_enabled` is a deployment setting, not per user (the tool's own docstring), so
"your account" would be wrong. The server must answer flag-off in a shape the CLI can tell apart
from an ordinary 403 or 404 (§10 R7), or the CLI cannot print these lines and falls back to the
generic ones. A single reporter, `_report_research_error`, recognises the flag-off shape first and
then delegates to `_report_room_error` with `--room`, else `_report_api_error`, chosen by
`args.room` as `_task_report` does. The words "group" and "channel" never appear in a message or
help line, including the server's flag name (IA §8 "No `GROUP`").

## 9. Rooms

### 9.1 Scope

Every leaf on both nouns takes `--room ROOM` through the shared `_room_parent(required=False,
omitted=…)`, not hand-added flags (the hand-added `--room` on `scheduled-tasks show/runs/…` is the
pattern not to copy). Without `--room` every verb acts on your own projects only, which is what
`notes`, `todos` and `scheduled-tasks` do: `list` without `--room` is personal, never a sweep
across rooms. (The one cross-room read in the CLI is `todos list --mine`, which exists because an
ask is addressed to you; nothing about a research project is.) A room project's name is resolved
among that room's projects (§5.1), and `research-items … --room R --project P` resolves P in R.

### 9.2 Confirmations

`cli-core-coverage.md` §6: commands that reach another person prompt on a terminal and need
`--yes` otherwise, even when nothing is destroyed. On a room project:

| Leaf with `--room` | Prompts | Why |
|---|---|---|
| `create` | yes | every member sees the new project, and a room's runs spend the room's budget |
| `run` | yes | changes the room's shared report and ledger, may raise Dash cards for members, spends the room's budget |
| `steer` | yes | the text is stored verbatim, attributed to you, and read by the run that writes the report every member sees |
| `edit --cron` (enable or change) | yes | recurring spend and recurring changes others see; in a room even a change prompts |
| `edit --file/--filter/--clear` | no | an edit to settings, like `notes edit --room`, which does not prompt |
| `pause`, `resume` | no | reversible, reaches no one (`resume` never runs a project: "Pausing or resuming a project never runs it", `reference/skills.md`) |
| `take`, `reconsider` | no | reversible ledger marks; §5.2's scope check guards the number |
| reads | no | — |

The prompt uses `_confirm`'s wording for room writes (`this changes the room for everyone; pass
--yes …`). Argparse cannot make a flag conditional, so `--yes` is accepted on every leaf that may
prompt (`create`, `edit`, `run`, `steer`) and is only *required* in the cases above; on a personal
`run` or `steer` it is a no-op, as `archive --yes` is (IA §3.1). Decision G asks whether the "no"
rows should prompt too.

### 9.3 Server dependencies for rooms

Who may do what in a room (owner only, or any member, per verb) is the server doc's call; the CLI
only maps the 403 (§8). It needs the answer per route to write help text (`Only a room owner can
…`, as `scheduled-tasks renew` does). Also needed: whether a room project may use a skill shared
with the room (`skills share`) and which built-ins are allowed (the guide says Product research
"isn't available in a group conversation"), and whose timezone a room schedule defaults to.

## 10. What the CLI needs from the server (all pending the server doc)

| # | Requirement | For |
|---|---|---|
| R1 | Routes for list (with a `status` filter), show, create, edit, run, steer, pause, resume, runs; items list (with `state` filter and a backward cursor), show, take, reconsider; each with a room twin | every leaf |
| R2 | A receipt endpoint (`GET` one run by id) with `status` ∈ `queued`, `running`, `succeeded`, `error`, `interrupted` and a reason on failure | `run --wait` |
| R3 | `report_artifact_id` on the project detail (the existing `/artifacts/{id}/detail` and `/raw` then serve it, any room member who can see it) — or a report route of its own | `show --report` |
| R4 | On project detail: ledger counts by state, queued steering count, open questions with their `qN` ids, the schedule (cron, timezone, task id) or `null`, last run | `show`; `steer --question` |
| R5 | Enabling a schedule needs an explicit consent field in the request; the server refuses without it; the API path does not depend on chat's confirmation token | §7 |
| R6 | The "What's new" entries a run wrote, structured, on the finished receipt (or on `runs`) | `run --wait` |
| R7 | Flag-off answers distinguishable from ordinary 403/404 (a stable `detail` code), personal and room | §8 |
| R8 | Filters replace the whole list (`[]` clears); the CLI sends exactly the `--filter` values given and never reads, appends and writes back | `edit --filter`, `--clear filters` |
| R9 | Whether `brief` is editable after create | `edit --file` |
| R10 | A default skill on create, or a rule that `skill` is required (`skill_name` is non-nullable) | decision E |
| R11 | What a `runs` row is (decision F) and its cursor | `runs` |
| R12 | Per-route room permissions, the room skill rule, the room schedule timezone (§9.3) | rooms |
| R13 | Typed response models, so `# MIRROR:` dataclasses can be checked by `tests/test_api_contract.py` | contract test |
| R14 | Optionally a per-project item ordinal (decision A) | item refs |

## 11. Tests, README, SKILL.md

**`tests/test_cli_conformance.py`:**

- `NOUNS` gains `research-projects`, `research-items`.
- `VERBS` gains `steer`, `take`, `reconsider`, `runs`. Adding `runs` takes `scheduled-tasks runs`
  out of `VERB_PENDING` (it is there today only because `runs` was never a §3.1 verb);
  `scheduled-tasks thread` stays pending.
- A new `CONTAINER = {"research-items": "--project"}` map: every leaf of the noun has the flag,
  marked required in its usage line, and the usage string prints `--room` before `--project`.
- A `CONFIRMS` set (`research-projects create/edit/run/steer`): each has `--yes`, and with
  `--room`, no terminal and no `--yes` each exits 2 with a J8 `usage` line and makes no write.
- `--room`: none of the new leaves goes in `ROOM_NA` or `ROOM_PENDING`.
- Envelope: `research-projects list`, `research-projects runs`, `research-items list` emit J3.
- `--wait`: a faked client that walks `queued → running → succeeded` exits 0 with one JSON document;
  `→ error` exits 1 with the receipt on stdout and a `run_failed` line on stderr; a `--timeout 1` run
  under a fake clock exits 4; `KeyboardInterrupt` mid-poll exits 130 with stdout empty and makes no cancel call.
- Wording: no help or message under the new nouns contains "group" or "channel".
- Feature off: a faked flag-off answer gives `forbidden`, exit 1, each message in §8.

Unit tests beside them: §5.2's meta check (a number from another project's listing is refused),
`--filter` + `--clear filters` together is a usage error, `--timezone` without `--cron` is a usage
error, `edit` with no flags and no terminal exits 2.

**README** (`## 4. Commands` table): one row per leaf in §3, room behaviour per row, `--wait` and
its exit codes in the `run` row, and a line under "Every command" that exit 4 means "stopped
waiting; the work goes on".

**Bundled `SKILL.md`** (the agent-member guide): no new command for agent tokens. §9 ("What an
agent token cannot run") already covers these nouns ("every command outside those nouns that talks
to the server"); if the server lets agent tokens read room projects, that becomes a §8 change then.
The §10 failure list gains exit 4. The vycari workspace `pepper-groups` skill needs no change.

**Server help:** pepper `features/help/skills/pepper-guide/reference/cli.md` gets the two nouns in
the same release (IA §7 lockstep (3)).

## 12. Phases

One PR each, cut fresh from `main`. All wait on the server routes being deployed and the snapshot
refresh. Phases 2 and 3 touch the same parser block in `cli.py` and run in sequence.

| Phase | PR | Contents | Depends on |
|---|---|---|---|
| 0 | This doc | design + IA amendments | — |
| 1 | Projects, personal | `research-projects list/show/create/edit/run/steer/pause/resume/runs` without `--room`; `show --report`; `--wait`; J8 `timeout`/`run_failed` + exit 4; `_report_research_error`; wire models with `# MIRROR:`; README rows; conformance | personal routes (R1–R11, R13) + snapshot |
| 2 | Items, personal | `research-items` with `--project`, `numbered_view` state, §5.2 check; `CONTAINER` conformance | phase 1 |
| 3 | Rooms | `--room` on both nouns via `_room_parent`, §9.2 confirmations, room flag-off message | room routes (R12), `research_group_projects_enabled` on in staging |
| 4 | Later | `brief --now --wait` if the brief gets a receipt; schedule off (decision C); item ordinal (decision A (b)) | each its own server change |

## 13. Open decisions (for Allen)

**A. Item references.** `seq` is global, not per project (§1.1).
(a) `numbered_view` list positions, scoped by the saved project and room (§5.2): no server
change, same as `artifacts` and `dash`; numbers are per profile, not shared between people.
(b) The server adds a per-project ordinal and the CLI uses it directly: stable and shareable ("item
7" means the same to everyone in a room), at the cost of a server field.
(c) The global `seq` as is: stable, but five-plus digits and it collides with the id-prefix rule.
**Recommendation: (a) for v1**, and (b) if the server doc is willing; the CLI change from (a) to
(b) is small and additive.

**B. `--wait` failure signalling.** (a) Two new J8 codes (`timeout`, `run_failed`) and a new J9 exit
4 for timeout, receipt on stdout in both (§6.1). (b) No new codes: a failed run and a timeout both
exit 1 with `server` and differ only in the message and the receipt's status. **Recommendation:
(a).** An agent has to tell "the run failed" (look at why) from "still going" (check later) without
parsing prose, and exit 4 is free.

**C. Turning a schedule off.** (a) `edit --clear schedule` (needs a server route that removes the
task and keeps the project; whether run history goes with it is the server's call). (b) Leave it
to `scheduled-tasks delete research:NAME --yes`, which works today and drops the task's history.
**Recommendation: (a)**, so the project's one door (§7) covers off as well as on; (b) remains
available meanwhile.

**D. Re-prompting on a schedule change.** (a) Personal: prompt only when enabling (decision 4 as
written); room: every change. (b) Every `--cron` prompts, personal or room. **Recommendation:
(a).** Telling a more expensive cadence from a cheaper one would be cron interpretation in the
CLI, and prompting on every change trains people to pass `--yes` by reflex.

**E. `--skill` on create.** `skill_name` is non-nullable. (a) Optional; the server picks a default
when it is omitted (the server doc says which). (b) Required in the CLI. **Recommendation: (a)** if
the server doc defines a default, else (b); choosing a skill from the brief would be business logic
the CLI must not carry.

**F. What `runs` lists.** (a) One server list of runs with a `trigger` (`manual`/`scheduled`)
column, merging receipts and iterations. (b) Iterations only. (c) Manual receipts only.
**Recommendation: (a)**, built by the server; a person asking "what ran" does not care which door
started it. The CLI renders whatever the server doc picks.

**G. Which room writes prompt.** (a) As §9.2: `create`, `run`, `steer` and schedule changes.
(b) Every room write on both nouns. **Recommendation: (a)**: it matches how `notes` and `todos`
room writes behave today (no prompt) and keeps the prompt for the actions that spend or speak.
