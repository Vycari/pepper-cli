# Research projects from the terminal — `research-projects` and `research-items`

**Status:** Accepted 2026-10-20. Allen took decisions 1–7 (§1.2) and §13 A–G on 2026-10-20. The
server routes are pepper `planning/research-projects-api.md` (pepper#5028), and its room and
general decisions taken the same day are recorded in §1.4.
**Author:** drafted with Claude Code for the maintainer
**Last updated:** 2026-10-20
**Tracked by:** #85
**Related:** `planning/cli-information-architecture.md` (the grammar; this doc amends §2, §3, §3.1,
§5, §6 and §8 there, in the same PR), `planning/cli-core-coverage.md` (§4.2 numbering, §6 rules);
server: pepper `planning/research-projects-api.md` (pepper#5028, the routes this waits on), pepper
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
| 3 | A run started over the API delivers to the project only; the CLI reads back with `show`, `show --report` and `runs` | §6.3 |
| 4 | Enabling a schedule (first `edit --cron`) prompts y/N on a terminal and needs `--yes` otherwise (exit 2), because it commits to recurring spend. A change to an existing schedule also goes through `research-projects edit` (§7) | §7 |
| 5 | Feature switched off → J8 `forbidden`, with a clear message | §8 |
| 6 | No `delete` or `stop` in v1 | §3 |
| 7 | `show --report` reads the report artifact (precedent: `feed show --transcript`) | §4.2 |
| — | Rooms are in scope: every verb on both nouns takes `--room ROOM` | §3, §9 |

One correction to the brief: decision 2 cites J4 for the receipt. J4 is the rule for views
(`next`, `calendar`); a receipt is what a mutation returns, which is **J5** ("mutations emit the
resulting object as the server returns it"). The design follows J5.

### 1.3 Ordering

Server first. Routes are named as pepper `planning/research-projects-api.md` (pepper#5028)
defines them: personal under `/api/v1/research-projects` (its §5.2) and the same paths under
`/api/v1/groups/{gid}/research-projects` for a room (its §5.7). Below, `…/{pid}` means either prefix.
Where that doc does not yet answer something this one needs, the requirement is marked **(open on
the server)** in §10. The CLI phases (§12) start only after the routes are deployed and the weekly
refresh has put them in `contracts/api-v1.openapi.json` (`CLAUDE.md`: server, then snapshot, then CLI).

### 1.4 Server decisions this doc builds on (pepper#5028, taken 2026-10-20)

| Server decision | Effect on the CLI |
|---|---|
| R1 (a): in a room, any member may read, `steer`, `take` and `reconsider`. The owner role is needed for `create`, editing the brief or filters, `run`, `pause`/`resume`, and enabling or clearing a schedule | §8 maps the 403 to "Only a room owner can …", and help text names the role per leaf (§9.3) |
| R2 (b): a room's manual run executes as, and is billed to, the room's account owner (`Group.owner_user_id`). The member who pressed run is kept in the run's metadata | the room `run` prompt says who pays (§9.2), and `runs` shows the member who pressed run (§4.3) |
| G1 (a): one queued or running run per project. A second `POST …/runs` is a 409 whose body carries the existing `run_id` | `run` attaches to that run instead of failing (§6.2) |
| G2 (a): `PATCH …/{pid}` `{brief}` ships in v1, and the edit is recorded in the report's changelog | `edit --file` is in v1 |
| Decision 5: flag off is a 403 with a `code` (`research_projects_disabled`, `research_group_projects_disabled`) | J8 `forbidden` with the §8 messages |
| Decision 4: enabling is one route (`POST …/{pid}/schedule`). The client confirms, there is no confirmation token, and changes to an existing schedule stay on the scheduled-tasks routes | §7 |
| Agent tokens are 401 on every route | no `SKILL.md` command change (§11) |

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
pepper research-projects edit      [--room R] PROJECT [--file BRIEF|-] [--filter F]… [--clear filters|schedule] [--cron C [--timezone TZ] [--topic T]] [--yes]
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
| `--topic T` only with `--cron --room` | the room schedule route takes `topic_id?`, the topic the scheduled runs post to (server R4 (a)). It is resolved by `_resolve_topic` as on `messages post`, and is a usage error without `--room` |
| `--clear schedule` | decision C: turns the schedule off and keeps the project and its run history |
| `run --timeout` | §6.1: `--wait` needs a bound |
| `steer --yes`, `run --yes` | room confirmations (§9.2); accepted and ignored on a personal project |
| `research-items show/take/reconsider` take `ITEM`, not `N` | `ITEM` is a number from the last listing **or** an item id (§5.2); the brief's `N` is the common case of it |
| `create --skill` optional | decision E: the server's default is `web-research` |
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

JSON (J3): `{"items": [<project>, …], "next_before": null}`. The server's `next_before` is always
null in v1 (pepper#5028 §4.3), so the list is one page. `--status` is sent to the server (default `active`, as
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

JSON (J5): the server's `ProjectDetail`. When a manual run is queued or running
(`inflight_run_id` is set), `show` adds a `Run` line, `queued 7f3a… (pepper research-projects show
[--room R] P)` (decision F: queued receipts appear here, not in `runs`). `show --report` reads `GET
…/{pid}/report` and prints its `body` raw to stdout, as `notes show` does. Under `--json` it emits
the `Report` object as the server returns it. The open questions (`qN` ids), ledger counts and
pending steering count are fields of `ProjectDetail` and `Report`, and the CLI never parses them
out of the markdown.

### 4.3 `research-projects runs`

```
STARTED            TRIGGER              STATUS      NOTE
2026-10-19 09:00   scheduled            succeeded
2026-10-15 14:02   manual · Chris       error       page fetch failed (source_url 503)
2026-10-12 08:30   manual · you         succeeded
```

Decision F: a row is an **iteration**, meaning what actually executed, from `GET
…/{pid}/iterations`. The TRIGGER column is the iteration's `kind`. A manual row also names the
member who pressed run, "you" when that was the caller, which matters in a room where the run is
billed to the account owner (server R2 (b)). A queued manual receipt has no iteration yet, so it
shows in `show` and not here. JSON (J3): `{"items": [<iteration>, …], "next_before":
<cursor|null>}`, paged backwards with `--before` like `scheduled-tasks runs` (IA §5.3), using the
server's opaque cursor. The server's separate `GET …/{pid}/runs` (request rows) has no command of
its own in v1. `--wait` reads one run by id (§6.1).

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

Decision A (a). `ITEM` is a number from the last `research-items list`, or an item id or a
unique id prefix of an item that listing held. This is `numbered_view.NumberedView` exactly as
`artifacts` and `booking-pages` use it, with the state file `research-items.json` beside the
credentials. The resolved id goes to `GET/POST …/{pid}/items/{iid}[/take|/reconsider]`.

**Why not the server's `seq`.** The brief assumed a per-project `seq`; on the server it is global
(§1.1). It would be stable and shared between people, which is what makes it attractive, but it
grows past four digits within weeks across the fleet, at which point `numbered_view`'s rule
("longer than four digits, or not all digits, is an id") reads it as an id prefix, and a person
typing `show 48213` for a project with 20 items is a poor interface. A per-project ordinal from
the server would fix both. Allen chose list positions for v1 (decision A), and an ordinal stays a
later, additive change.

**Scoping a saved listing.** `save()` records `meta = {"project_id", "room_id"}` (room id `null`
for personal). `show/take/reconsider` resolve `--room` and `--project` first and refuse a number
whose saved meta names a different project or room: `pepper: the last 'research-items list' was
of another project — list this one first`, J8 `usage`, exit 2. A number never resolves against
the wrong ledger. An id or id prefix is accepted without that check (the server refuses one that is
not in the project). `take` and `reconsider` act on a number only after the check, which is the
same safeguard `dash do` gets from a confirmation (`cli-core-coverage.md` §6), without a prompt:
both are reversible and spend nothing (in a room any member may do either, server R1 (a)).

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
| Polling | `GET …/{pid}/runs/{rid}` (the server's `RunDetail`, terminal at `succeeded`, `error`, `interrupted`) at 2 s, growing ×1.5 per poll to a 15 s cap; reset to 2 s on a status change. A `Retry-After` from the server overrides the interval |
| Bound | `--timeout SECONDS`, default 900 (15 min), `0` = no bound. On expiry the command stops waiting; the work goes on |
| Progress | Human mode: one status line on stderr (`queued` → `running`, elapsed time), rewritten in place on a terminal, one line per change off a terminal. JSON mode: nothing at all until the end (J2), and never JSON lines: `--wait` is not a stream (J6) |
| End: succeeded | exit 0. stdout: the final receipt (JSON), or in human mode the receipt line and then the run's "What's new" lines (§6.3) |
| End: run failed (`error`, `interrupted`) | stdout: the final receipt, the same document as on success (it is the answer). stderr: a J8 line, code `run_failed`. exit 1 |
| End: timeout | stdout: the last receipt seen (status `queued`/`running`). stderr: a J8 line, code `timeout`, message naming the receipt id and `pepper research-projects show [--room R] P` (the run is still queued or running, so it is in `show`, not yet in `runs`, §4.3). exit 4 |
| Ctrl-C | Stops waiting, never cancels the run. stderr: `pepper: still running — check 'pepper research-projects show [--room R] P'` (human mode) or a J8 line, code `timeout` (JSON mode). stdout empty. exit 130 (J9) |
| Hints | Every recovery hint names `show`, never `runs`, while the run is unfinished, and repeats the `--room R` the command was given, so it can be pasted as is |
| Poll errors | A network error or 5xx while polling is retried within the bound; a 401/403/404 ends the wait through the leaf's normal error path (exit per J9) |

The J8 `code` list is closed today (`_ERROR_CODES` raises on an unknown one), and J9 has no "gave
up waiting" exit code. So this adds two J8 codes and one J9 exit code, as an IA amendment. Writing
a receipt to stdout on a failed run departs from J8's "stdout stays empty": the receipt is the
result the caller asked for, and the J8 line still tells an agent that the run failed. Decision B
B chose these codes over the alternative of adding none.

### 6.2 `run` without `--wait`, and a run already in flight

```
$ pepper research-projects run robot-vacuums
Queued run 7f3a… for robot-vacuums. Check it with: pepper research-projects show robot-vacuums
```

`POST …/{pid}/runs` answers 202 with a `RunReceipt` (J5 under `--json`).

**A run already queued or running** (server G1 (a)). The server answers 409, and its body carries
the existing `run_id`. The CLI treats that as the answer, not a failure:

- With `--wait`, it attaches to the existing run and waits on it under the §6.1 rules. It says so
  on stderr in human mode (`A run of robot-vacuums is already running (7f3a…); waiting on it
  instead of starting another.`), and the exit code is the waited run's.
- Without `--wait`, it fetches `GET …/{pid}/runs/{rid}` and prints `A run of robot-vacuums is
  already queued (7f3a…); not starting another.` In JSON mode it emits that `RunDetail` on stdout
  and exits **0**.

Why exit 0 and not 1 `conflict`: the caller asked for a run to happen, and one is happening. The
server designed G1 as retry semantics ("gives a retrying client the run to poll"). An agent that
retries after a dropped connection must not read its own first request as a failure. The run's
id is on stdout as data, so nobody has to parse a J8 message for it. A caller that has to know
whether this request started the run compares `requested_at`, or reads the human stderr line.
J9's exit 1 stays for requests that did not get what was asked. A 409 **without** a `run_id`
(paused or stopped project, or "Pepper will not start another run while it cannot verify that an
earlier worker stopped", `reference/skills.md`) is such a request: it prints the server's detail,
J8 `conflict`, exit 1.

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

These come from the server as data: the `changelog` entries of `GET …/{pid}/report` that the
finished run's iteration wrote. Tying an entry to its iteration needs an iteration id on each
entry, which is open on the server (§10 R6). The CLI does not cut the `## What's new` section out
of the report's markdown, because parsing a server-owned document format into meaning belongs to
the server (`cli-core-coverage.md` §6). Until entries carry the id, the CLI prints `Read the report:
pepper research-projects show P --report` instead.

## 7. Schedules

`research-projects edit P --cron C [--timezone TZ]` sets the project's schedule. `--timezone`
defaults to the server's default (the person's profile timezone in the tool today). In a room,
`--topic T` picks the topic that scheduled runs post to.

**Enabling** (the project is manual-only, `scheduled_task_id` is null) is `POST …/{pid}/schedule`
`{cron, timezone?}` (room: plus `topic_id?`). It commits to recurring spend, so on a terminal it
prompts (`Run robot-vacuums every time '0 9 * * 0' matches (America/Los_Angeles)? Each run uses
Pepper on your account. [y/N]`) and otherwise needs `--yes` (exit 2, J8 `usage`; J2: never a
prompt under `--json`). In a room the prompt names the room's account owner as the one who pays.
Whether the project has a schedule is read from the server's `ProjectDetail`, never from the CLI's
memory. Server decision 4 makes the client the confirmer: the route has no consent field and no
token, because the PAT is the human. The prompt is therefore the only guard, and a second client
that reads stale state could enable without asking. That is accepted, and it is recorded here so a
later reader does not assume the server re-checks.

**Changing an existing schedule** goes through the same leaf, `research-projects edit --cron`.
Under it the CLI sends `PATCH /api/v1/scheduled-tasks/{scheduled_task_id}` (personal) or the room
twin, with the `scheduled_task_id` from `ProjectDetail`, because server decision 4 keeps schedule
changes on the scheduled-tasks routes. The person and the agent learn one door, and the server
keeps one route for task edits. A person can still reach that task directly
(`scheduled-tasks edit research:NAME`), and whether to allow it is the server's call, not the
CLI's. The reasons for one CLI door:

1. The schedule rules belong to the project. The tool requires a skill that knows what to do each
   run, and the project's `pause`/`resume` is a different switch from the task's.
2. An agent learns one place for a project's settings.

Decision D (a): a personal project prompts only at enable, since the commitment to recurring spend
was made then. In a room every `--cron` change prompts (§9.2).

**Turning a schedule off** is `edit --clear schedule` (decision C (a)). It calls the server's
schedule-detach route, which removes the task, keeps the project manual-only and **keeps the run
history** (the route is being added to pepper#5028; its path is open on the server, §10 R5).
`scheduled-tasks delete research:NAME --yes` still works and still drops the history. This is the
softer door. It does not prompt on a personal project: it stops spend and can be undone by
enabling again. In a room it needs the owner role, like enabling.

## 8. Errors

| Situation | J8 `code` | Exit | Message (human mode) |
|---|---|---|---|
| Research projects switched off on the server (403, `code: research_projects_disabled`) | `forbidden` | 1 | `Research projects are not enabled on this Pepper server.` |
| Room research projects switched off (403, `code: research_group_projects_disabled`; only a member gets this, a non-member gets the room 404) | `forbidden` | 1 | `Research projects in rooms are not enabled on this Pepper server.` |
| Rooms not enabled for the account | `forbidden` | 1 | the existing `Rooms are not enabled for your account.` (`_GroupsDisabledError`) |
| Member without the owner role, on `create`, `edit`, `run`, `pause`, `resume`, enable or clear schedule (403 from `require_manage_authority`) | `forbidden` | 1 | `Only a room owner can <verb> a room's research project.`, then the server's detail |
| No such room, or not a member, or room agency off (`--room`, the uniform room 404) | `not_found` | 2 | `_report_room_error`: `not found, or you are not a member of that room` |
| No such project in that scope (`no such research project`: unknown id, someone else's, or a meeting-prep project) | `not_found` | 1 | `_report_api_error`, as `scheduled-tasks` does |
| Run already in flight (409 with `run_id`) | — | 0 | §6.2: attach or report it, not an error |
| Name or number does not resolve | `not_found` / `ambiguous` / `usage` | 2 | §5 |
| Prompt needed and no terminal or `--json` | `usage` | 2 | `pass --yes to …` |
| Refused transition (409), taken name (409), bad input (422) | `conflict` / `validation` | 1 | the server's own refusal sentence, the same text chat shows |

The flag-off messages say "on this Pepper server", not "for your account":
`research_projects_enabled` is a deployment setting, not per user (the tool's own docstring), so
"your account" would be wrong. The server keys flag-off with a 403 `code`. The server doc says the
CLI "keys on status", but a role 403 is also a 403, so the CLI keys on `code` and falls back to
the role message when it is absent. A single reporter, `_report_research_error`, recognises the
flag-off codes first and
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
| `create` | yes | every member sees the new project |
| `run` | yes | changes the room's shared report and ledger, may raise Dash cards for members, and is billed to the room's account owner (server R2 (b)); the prompt names them |
| `steer` | yes | the text is stored verbatim, attributed to you, and read by the run that writes the report every member sees |
| `edit --cron` (enable or change) | yes | recurring spend and recurring changes others see; in a room even a change prompts (decision D) |
| `edit --clear schedule` | no | stops spend; enabling again restores it |
| `edit --file/--filter/--clear` | no | an edit to settings, like `notes edit --room`, which does not prompt |
| `pause`, `resume` | no | reversible, reaches no one (`resume` never runs a project: "Pausing or resuming a project never runs it", `reference/skills.md`) |
| `take`, `reconsider` | no | reversible ledger marks; §5.2's scope check guards the number |
| reads | no | — |

The prompt uses `_confirm`'s wording for room writes (`this changes the room for everyone; pass
--yes …`). Argparse cannot make a flag conditional, so `--yes` is accepted on every leaf that may
prompt (`create`, `edit`, `run`, `steer`) and is only *required* in the cases above; on a personal
`run` or `steer` it is a no-op, as `archive --yes` is (IA §3.1). Decision G (a) keeps the "no"
rows unprompted.

**As built (phase 3).** Three details differ from the text above, each forced by a server fact:

- The `run` prompt says the run is billed to "the room's account owner" but cannot name them: no
  response the CLI reads carries `Group.owner_user_id`. Naming the person needs a server field.
- The prompts for `create`, `run`, `steer` and `edit --cron` in a room come before any network
  call (as `scheduled-tasks` room writes do), so a refusal without a terminal or `--yes` sends
  nothing, not even the room lookup. The room-schedule prompt therefore does not say whether it
  enables or changes the schedule; it is the same question either way (decision D).
- `edit --topic T` resolves `T` by name or UUID in the room before the first write. On a schedule
  that already exists, a topic goes with the cadence on the room's
  `PATCH /api/v1/groups/{gid}/scheduled-tasks/{tid}` as a `delivery` object
  (`{destinations: ["groups_service"], topic}`), because that route has no other way to name one.

### 9.3 Roles in a room

Server R1 (a). Help text names the role on each leaf, as `scheduled-tasks renew` does:

| Any member | Owner role |
|---|---|
| `list`, `show`, `show --report`, `runs`, `research-items list/show`, `steer`, `take`, `reconsider` | `create`, `edit` (brief, filters, schedule on/change/off), `run`, `pause`, `resume` |

A room's steering is attributed to its author and every member sees it (server R5). Still open on
the server: which skills a room project may use (the skill must be granted to the room with the
`research-project` toolset, so the `web-research` default must be granted too, §10 R10), and whose
timezone a room schedule defaults to.

## 10. What the CLI needs from the server (pepper#5028)

Routes are relative to `/api/v1/research-projects`, and each has the room twin under
`/api/v1/groups/{gid}/research-projects` (server §5.7).

| # | Requirement | Server answer | For |
|---|---|---|---|
| R1 | List, show, create, edit, run, steer, pause, resume, runs; items list, show, take, reconsider; room twins | `GET`/`POST /`, `GET`/`PATCH /{pid}`, `PUT /{pid}/filters`, `POST /{pid}/runs`, `GET /{pid}/iterations`, `POST /{pid}/steering`, `POST /{pid}/pause`, `/resume`, `GET /{pid}/items` (`state*`, `seq` cursor), `GET /{pid}/items/{iid}`, `POST …/take`, `POST …/reconsider` `{reason}` | every leaf |
| R2 | One run by id, terminal statuses | `GET /{pid}/runs/{rid}` → `RunDetail` (`succeeded`, `error`, `interrupted`; `error`) | `run --wait` |
| R3 | The report | `GET /{pid}/report` → `Report` (body, open questions, changelog) | `show --report` |
| R4 | Detail fields | `ProjectDetail`: `item_counts`, `pending_steering_count`, `open_questions`, `schedule`, `scheduled_task_id`, `last_iteration`, `inflight_run_id` | `show`; `steer --question` |
| R5 | Enable and turn off a schedule | enable: `POST /{pid}/schedule` `{cron, timezone?, topic_id?}`, client confirms (no consent field, §7). Off: a detach route that keeps run history, **(open on the server: being added to pepper#5028, path not yet in the doc)**. Change: `PATCH /api/v1/scheduled-tasks/{scheduled_task_id}` | §7 |
| R6 | "What's new" for one run as data | `Report.changelog` exists. **(Open on the server:** an iteration id on each entry, or the entries on `RunDetail.iteration`) | `run --wait` |
| R7 | Flag-off distinguishable | 403 with `code` `research_projects_disabled` / `research_group_projects_disabled` | §8 |
| R8 | Filters replace the whole list | `PUT /{pid}/filters` `{filters: [{text}]}` (`[]` clears) | `edit --filter`, `--clear filters` |
| R9 | Brief editable | `PATCH /{pid}` `{brief}`, recorded in the report changelog (server G2 (a)) | `edit --file` |
| R10 | Default skill | Allen: `web-research` when `skill` is omitted. **(Open on the server:** the doc's create body still lists `skill` as required) | decision E |
| R11 | `runs` rows and cursor | `GET /{pid}/iterations` with opaque cursor and `kind`. **(Open on the server:** the iteration needs the member who pressed run, which the doc reads back only on `GET /{pid}/runs`) | `runs` |
| R12 | Room roles, skill rule, schedule timezone | roles: server R1 (a), §9.3. Skill: granted to the room with the `research-project` toolset. **(Open on the server:** the default timezone for a room schedule) | rooms |
| R13 | Typed response models | §5.3 shapes, with the snapshot regenerated per phase | `tests/test_api_contract.py` |
| R14 | Per-project item ordinal | not in v1 (decision A (a)) | — |

## 11. Tests, README, SKILL.md

**`tests/test_cli_conformance.py`:**

- `NOUNS` gains `research-projects`, `research-items`.
- `VERBS` gains `steer`, `take`, `reconsider`, `runs`. Adding `runs` takes `scheduled-tasks runs`
  out of `VERB_PENDING` (it is there today only because `runs` was never a §3.1 verb);
  `scheduled-tasks thread` stays pending.
- A new `CONTAINER = {"research-items": "--project"}` map: every leaf of the noun has the flag,
  marked required in its usage line, and the usage string prints `--room` before `--project`.
- A `CONFIRMS` set of the prompting cases (`research-projects create`, `run`, `steer` and
  `edit --cron`): each leaf has `--yes`, and with `--room`, no terminal and no `--yes` each case
  exits 2 with a J8 `usage` line and makes no write.
- The non-prompting room edits (§9.2): `edit --room R P` with `--file`, `--filter`,
  `--clear filters` or `--clear schedule`, with no terminal and no `--yes`, each succeeds (exit 0)
  and sends its write.
- `--room`: none of the new leaves goes in `ROOM_NA` or `ROOM_PENDING`.
- Envelope: `research-projects list`, `research-projects runs`, `research-items list` emit J3.
- `--wait`: a faked client that walks `queued → running → succeeded` exits 0 with one JSON document;
  `→ error` exits 1 with the receipt on stdout and a `run_failed` line on stderr; a `--timeout 1` run
  under a fake clock exits 4; `KeyboardInterrupt` mid-poll exits 130 with stdout empty and makes no cancel call.
- In-flight run: a faked 409 carrying `run_id` makes `run` exit 0 with that `RunDetail` on stdout,
  and makes `run --wait` poll that id (it never sends a second POST). A 409 without `run_id` exits 1
  with `conflict`.
- Room roles: a faked role 403 without a flag `code` prints the "Only a room owner can …" message,
  `forbidden`, exit 1.
- Wording: no help or message under the new nouns contains "group" or "channel".
- Feature off: a faked flag-off answer gives `forbidden`, exit 1, each message in §8.

Unit tests beside them: §5.2's meta check (a number from another project's listing is refused),
`--filter` + `--clear filters` together is a usage error, `--timezone` without `--cron` is a usage
error, `--topic` without `--room` and `--cron` is a usage error, `--cron` with `--clear schedule` is
a usage error, `edit` with no flags and no terminal exits 2.

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
| 1 | Projects, personal | `research-projects list/show/create/edit/run/steer/pause/resume/runs` without `--room`; `show --report`; `--wait` with attach-on-409; `--clear schedule`; J8 `timeout`/`run_failed` + exit 4; `_report_research_error`; wire models with `# MIRROR:`; README rows; conformance | server phases 2–4 (personal routes and schedule) + snapshot |
| 2 | Items, personal | `research-items` with `--project`, `numbered_view` state, §5.2 check; `CONTAINER` conformance | phase 1 |
| 3 | Rooms (built, pepper-cli#95) | `--room` on both nouns via `_room_parent`, `edit --topic`, §9.2 confirmations, role messages, room flag-off message | server phase 5 (room twins, pepper#5113), `research_group_projects_enabled` on in staging |
| 4 | Later | `brief --now --wait` if the brief gets a receipt; item ordinal (decision A (b)) | each its own server change |

## 13. Decisions (taken by Allen, 2026-10-20)

Each entry keeps the options and the reasoning that were put to the maintainer.

**A. Item references.** **Taken 2026-10-20: (a)**, `numbered_view` refs scoped to project and room. `seq` is global, not per project (§1.1).
(a) `numbered_view` list positions, scoped by the saved project and room (§5.2): no server
change, same as `artifacts` and `dash`; numbers are per profile, not shared between people.
(b) The server adds a per-project ordinal and the CLI uses it directly: stable and shareable ("item
7" means the same to everyone in a room), at the cost of a server field.
(c) The global `seq` as is: stable, but five-plus digits and it collides with the id-prefix rule.
Recommended at the time: (a) for v1, and (b) if the server doc is willing; the CLI change from (a) to
(b) is small and additive.

**B. `--wait` failure signalling.** **Taken 2026-10-20: (a)**, `timeout` and `run_failed`, plus exit 4. (a) Two new J8 codes (`timeout`, `run_failed`) and a new J9 exit
4 for timeout, receipt on stdout in both (§6.1). (b) No new codes: a failed run and a timeout both
exit 1 with `server` and differ only in the message and the receipt's status. Recommended at the time:
(a).** An agent has to tell "the run failed" (look at why) from "still going" (check later) without
parsing prose, and exit 4 is free.

**C. Turning a schedule off.** **Taken 2026-10-20: (a)**, `edit --clear schedule`. The server adds a detach route that keeps the run history (pepper#5028). (a) `edit --clear schedule` (needs a server route that removes the
task and keeps the project; whether run history goes with it is the server's call). (b) Leave it
to `scheduled-tasks delete research:NAME --yes`, which works today and drops the task's history.
Recommended at the time: (a), so the project's one door (§7) covers off as well as on; (b) remains
available meanwhile.

**D. Re-prompting on a schedule change.** **Taken 2026-10-20: (a)**. A personal project asks once, at enable. A room asks on every cron change. (a) Personal: prompt only when enabling (decision 4 as
written); room: every change. (b) Every `--cron` prompts, personal or room. Recommended at the time:
(a).** Telling a more expensive cadence from a cheaper one would be cron interpretation in the
CLI, and prompting on every change trains people to pass `--yes` by reflex.

**E. `--skill` on create.** **Taken 2026-10-20: (a)**, `--skill` optional, with server default `web-research`. `skill_name` is non-nullable. (a) Optional; the server picks a default
when it is omitted (the server doc says which). (b) Required in the CLI. Recommended at the time: (a) if
the server doc defines a default, else (b); choosing a skill from the brief would be business logic
the CLI must not carry.

**F. What `runs` lists.** **Taken 2026-10-20: iterations** (a variant of (b)), from the server's iterations route, with a trigger column: manual, naming the member who pressed run, or scheduled. Queued manual receipts appear in `show`, and `--wait` polls the manual-run receipt route (§4.3, §6.1). (a) One server list of runs with a `trigger` (`manual`/`scheduled`)
column, merging receipts and iterations. (b) Iterations only. (c) Manual receipts only.
Recommended at the time: (a), built by the server; a person asking "what ran" does not care which door
started it. The CLI renders whatever the server doc picks.

**G. Which room writes prompt.** **Taken 2026-10-20: (a)**: room `create`, `run`, `steer` and schedule-on/change prompt. (a) As §9.2: `create`, `run`, `steer` and schedule changes.
(b) Every room write on both nouns. Recommended at the time: (a): it matches how `notes` and `todos`
room writes behave today (no prompt) and keeps the prompt for the actions that spend or speak.
