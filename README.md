# `pepper` — the Pepper command-line client

A small terminal client for talking to Pepper over the `/api/v1` HTTP API:
one-shot messages, an interactive REPL, and read-only trajectory inspection.
It authenticates with a **personal access token (PAT)** scoped to your own
user — it never sees your Google credentials and never acts as anyone else.

It depends only on `httpx`, `rich`, and `prompt_toolkit` (no dependency on
the Pepper server package).

The package is published as `vycari-pepper`; the command it installs is `pepper`.
It is licensed under Apache-2.0 (see `LICENSE`).

> [!NOTE]
> The `/api/v1` API this client talks to is supported for Vycari's own clients.
> Third-party clients that use a PAT against it do so at their own risk: there is
> no stability promise beyond what this client needs.

---

## 1. Install `uv` (prerequisite)

The CLI is distributed and run through [`uv`](https://docs.astral.sh/uv/),
Astral's Python package/tool manager. If you don't already have it:

- **macOS / Linux:** `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **macOS (Homebrew):** `brew install uv`
- **Windows (PowerShell):** `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`

Full instructions and other options: <https://docs.astral.sh/uv/getting-started/installation/>

Verify with `uv --version`.

## 2. Install the CLI

There are two ways to get a `pepper` command, depending on what you're doing.

### Daily use — install it on your `PATH`

```bash
uv tool install --from git+https://github.com/Vycari/pepper-cli vycari-pepper
```

This puts a `pepper` executable on your `PATH` (typically `~/.local/bin/pepper`).
Installing from PyPI will replace the git URL once the package is published there.

> [!NOTE]
> **Upgrading from the old `pepper-cli` install.** Earlier versions were installed
> from a subdirectory of the main Pepper repository under the name `pepper-cli`.
> Remove that install once, then install as above:
> ```bash
> uv tool uninstall pepper-cli
> ```
> Your credentials file (`~/.config/pepper/credentials.toml`) is unchanged and is
> shared by the new install.

Keep in mind:

> [!IMPORTANT]
> **This is a *snapshot* pinned to the commit it was built from — it does not
> auto-update.** After the CLI changes upstream (e.g. a new subcommand lands),
> refresh it:
> ```bash
> uv tool upgrade vycari-pepper
> ```
> If you skip this, the old binary won't have the new subcommand — and because
> bare `pepper <args>` is shorthand for `pepper chat`, an unrecognized command
> like `pepper trajectory show <id>` is silently sent to your agent **as a chat
> message** instead of erroring. If a command seems to "talk to Pepper" instead
> of doing what you asked, upgrade first.

**Typo guard.** If the first word of an implicit-chat message is a close match
to a command (e.g. `pepper notse`), or a quoted string whose first word is a
command (`pepper "todos add"`, which is what zsh passes for an unsplit `$var`),
`pepper` prints a "Did you mean ...?" hint to stderr and exits 2 instead of
chatting. Trade-off: a one-word chat message that happens to be near a command
name (`pepper note`) gets the hint too; send it with `pepper chat note`. Agents
should set `PEPPER_NO_IMPLICIT_CHAT=1`, which turns implicit chat off entirely:
any first word that isn't a command exits 2. `pepper chat ...` and bare `pepper`
(the REPL) still work.

### Working in this repo — always run the source

To run the version in your current checkout (no install, no staleness), use
`uv run`:

```bash
uv run pepper --help
```

This builds from the working tree every time and shares the same credentials
file as the installed copy, so it's the right choice when developing the CLI
or when you need a subcommand that hasn't been published yet.

## 3. Log in

```bash
pepper login                       # defaults to https://pepper.vycari.ai
pepper login --server https://your-host
```

By default this opens your browser for a device-authorization login: the CLI
asks the server to start a login, prints a short code (`XXXX-XXXX`) and a
verification URL, opens that URL in your browser, and polls in the background
until you approve the request there. Once approved, the server has minted a
personal access token (PAT) for this device — the CLI validates it and saves
it for you. No copy-pasting a token required.

If the browser can't reach the server (a self-hosted instance not exposed to
your machine, a headless box you're SSH'd into) or the flow is denied/expires,
just run `pepper login` again. Pass `--no-browser` to get the code and URL
without the CLI trying to auto-open a browser (still handy over SSH with port
forwarding, or to open the link on a different device).

The manual path — minting a token yourself at **`<server>/settings#tokens`**
(shown exactly once — copy it somewhere safe) and pasting it in — still works,
and is the right choice for scripting, CI, or any headless environment with no
browser at all:

```bash
pepper login --manual                                      # prompts for the token (no echo)
pepper login --server https://your-host --token pat_xxx     # fully non-interactive
```

Passing `--token` always skips the browser flow, whether or not `--manual` is
also given.

Credentials are written to `$XDG_CONFIG_HOME/pepper/credentials.toml`
(`~/.config/pepper/credentials.toml` by default) with strict `0600`
permissions — the CLI refuses to read a world- or group-readable file.

To hold a second login next to your own — an agent member's `pep_agt_` token,
say — give it a profile. `--profile NAME` goes **before** the subcommand and
stores that login at `$XDG_CONFIG_HOME/pepper/profiles/NAME/credentials.toml`
(same `0600` rule); your default login is never touched:

```bash
pepper --profile claude-code login --manual
pepper --profile claude-code messages post --room <room> --topic <topic> -
pepper --profile claude-code logout
```

`NAME` may use letters, digits, `.`, `_` and `-` (not starting with `-`, and
not `.` or `..`). `--profile` is honored only as the first argument, once, and
spelled in full: an abbreviation (`--prof`), a second `--profile`, or one after
the subcommand is an error (exit 2), never silently ignored. Without
`--profile` everything works exactly as before. `pepper login` with a
`pep_agt_` token refuses unless `--profile` is given, so an agent token can't
overwrite your own login.

Confirm it worked:

```bash
pepper whoami
```

## 4. Commands

Commands that take `--json` also switch to JSON output when the environment variable `PEPPER_JSON` is exactly `1`, so an agent can export it once; `--json` on the command line always wins, any other value (or unset) leaves the human output, and `login` and the interactive REPL ignore it.

| Command | What it does |
| --- | --- |
| `pepper` | Open the interactive REPL (shorthand for `pepper chat`). |
| `pepper <message>` | Send a one-shot message — multi-word, no quotes needed. |
| `pepper chat ["msg"]` | One-shot with a message, or the REPL with none. |
| `pepper chat --root PATH ["msg"]` | Same, but let this session read files under `PATH` (repeatable). |
| `pepper whoami` | Print the user behind the current credentials. |
| `pepper trajectory show <uuid> [--json]` | Print one trajectory's events; `--json` for full untruncated payloads. |
| `pepper trajectory list [--limit N] [--offset N]` | List your recent trajectories (default 20, max 200). |
| `pepper scheduled-tasks list [--all] [--room R]` | List your scheduled tasks, or a room's with `--room`; `--all` includes paused ones and spent one-offs. |
| `pepper scheduled-tasks show\|runs\|thread <task>` | One task's config and last run, its fire history, or its own conversation. |
| `pepper scheduled-tasks add <name> --prompt P (--cron C \| --at T) [--room R]` | Schedule a recurring or one-off task. With `--room`: on the room, `--cron` only. |
| `pepper scheduled-tasks edit <task> [--prompt …] […]` | Change a task; only the flags you pass are written. |
| `pepper scheduled-tasks pause\|resume <task> [--room R]` | Take a task off the schedule (history kept) and put it back. |
| `pepper scheduled-tasks destinations <task> (--to D… \| --default \| --silent) [--room R] [--topic NAME]` | Set where the task announces its results. |
| `pepper skills list\|show <name> [--room R]` | Your skill library (yours, then the built-ins), or the skills shared with a room with `--room`. `show` prints SKILL.md. |
| `pepper skills new\|edit\|delete <name>` | Author one of your own skills in `$EDITOR`, or delete it. Never takes `--room`. |
| `pepper skills share\|revoke <name> --room R` | Share one of your skills with a room, or stop sharing one there. |
| `pepper next [--room R] [--first urgent\|important\|fast] [--all] [--json]` | What to do next: Now, a numbered Do next, Due soon (48h), Later. GETs only. |
| `pepper todos list [--status S] [--room R] [--json]` | List your todos, or a room's with `--room`. |
| `pepper todos add <title…> [--due D] [--room R]` | Add a todo. With `--room`: title and a `YYYY-MM-DD` due date only. |
| `pepper todos done\|reopen <id> [--room R]` | Tick a todo as Schedule does (closes it in your or the room's list and releases the rest of its held time), or put it back. |
| `pepper todos dismiss <id> [--room R]` | Close a todo without completing it. |
| `pepper rooms list [--archived] [--json]` | List your rooms. `--json` is `{"items": [...], "next_before": null, "enabled", "degraded", "total", "archived_total"}`. |
| `pepper rooms show ROOM` | One room's info: kind, members + roles, topics, installed apps. |
| `pepper rooms create [--name N] PERSON…` | Create a room. `PERSON` is an email, a phone, or a Contacts name. Prints the id. |
| `pepper rooms edit ROOM --name NAME` | Rename a room. |
| `pepper rooms archive\|unarchive ROOM` | Hide a room from your list (your view only), or restore it. |
| `pepper rooms leave ROOM [--yes]` | Leave an open room; asks on a terminal unless `--yes`. |
| `pepper rooms mute\|unmute ROOM` | Silence or restore a room's push notifications on your devices. |
| `pepper rooms activity [--room ROOM] [--since TIME]` | The newest message from someone else in each recently active topic (default: last 24 hours). |
| `pepper rooms watch [--room ROOM [--topic TOPIC]] [--since CURSOR] [--cursor-file PATH] [--json]` | Agent members: follow the event stream, one line per event (ids only; fetch bodies with `messages list`). Reconnects by itself and resumes from the cursor file. `--room`/`--topic` have the server narrow the stream to one room or topic, and refuse (never widen) on a server that cannot; use one cursor file per scope. Exit 3 when the server can no longer replay from the cursor without a gap. |
| `pepper topics list --room ROOM [--archived] [--json]` | List a room's topics (folded to unarchived by default, except the default topic, which always stays). `--json` marks the room's default topic with `is_default`. |
| `pepper topics create --room ROOM NAME…` / `topics edit --room ROOM TOPIC --name NAME` | Start or rename a topic. |
| `pepper topics archive\|unarchive --room ROOM TOPIC` | Archive or restore a topic. |
| `pepper topics search [--room ROOM] QUERY… [--json]` | Find topics by name across your unarchived rooms, or in one room. |
| `pepper messages list --room ROOM [--topic TOPIC] [--limit N] [--before C \| --since S] [--mark] [--json]` | Read a topic's messages, oldest to newest. Omit `--topic` for the room's default one. `--mark` then marks the topic read through its newest message. |
| `pepper messages post --room ROOM --topic TOPIC [MESSAGE \| -] [--attach PATH ...] [--reply-to MESSAGE_ID] [--mention PERSON_ID ...] [--idempotency-key KEY]` | Post a message as yourself; `-` reads the body from stdin. `--reply-to` threads it onto a message, `--mention` structurally mentions a person id, and `--idempotency-key` makes a re-run of the same command unable to post twice. |
| `pepper messages edit --room ROOM MESSAGE TEXT…` | Edit one of your messages. Message ids come from `messages list --json`. |
| `pepper messages delete --room ROOM MESSAGE [--yes]` | Delete one of your messages. |
| `pepper messages react\|unreact --room ROOM MESSAGE EMOJI` | Add or take back your reaction. |
| `pepper attachments download --room ROOM ATTACHMENT --out PATH` | Save an attachment (`-` for stdout; not with `--json`). |
| `pepper groups` | Retired: exits 2 and names the commands above. |
| `pepper agent-skill [--write DIR [--force]]` | Print the agent-usage guide (`SKILL.md`) shipped inside this binary: how an agent member logs in, watches, reads and posts in rooms. `--write DIR` writes `DIR/SKILL.md` and refuses to overwrite without `--force`. Needs no login and no network. |
| `pepper notes list [--room <group>] [--label L] [--json]` | List your notes, or a room's shared ones with `--room`. |
| `pepper notes search "<query>" [--room <group>] [--label L] [--json]` | Case-insensitive substring search over note names and bodies. |
| `pepper notes show <note> [--room <group>] [--json]` | Print one note's raw markdown body to stdout; `--json` for the full detail (labels, `updated_at`, `etag`, `shared`). |
| `pepper notes new "<name>" [--room <group>] [--file PATH \| -]` | Create a note. Body from `$EDITOR`, or `--file PATH`/`--file -` for stdin. |
| `pepper notes edit <note> [--room <group>] [--file PATH \| -] [--rename NAME] [--if-match ETAG \| --force]` | Edit a note; sends nothing when the text is unchanged. |
| `pepper notes rm <note> [--room <group>] [--yes]` | Delete a note; asks for confirmation unless `--yes`. |
| `pepper login` / `pepper logout` | Store / delete credentials. |

**For agents.** `pepper agent-skill` prints a skill document (frontmatter plus
body, in the standard `SKILL.md` format) written for an agent that takes part in
rooms with a `pep_agt_` token: the `--profile` login, the `rooms watch` event
stream, catching up with `messages list`, and posting. It is packaged inside the
CLI, so it always matches the installed version; `pepper agent-skill --write
~/.claude/skills/pepper-agent` drops it into a skills directory. It covers the
mechanics only; any house policy for what an agent may do belongs in a separate
layer on top. Note this is unrelated to `pepper skills`, which manages your
account's own skills. A test holds the document to the real parser: every command in it
must be a full `pepper …` invocation whose subcommand and flags exist, every
`--flag` it mentions anywhere must exist, and outside its "cannot run" section
it may only show the `rooms`, `topics`, `messages` and `attachments` commands the server accepts an agent token for.
A renamed flag, a shortened example or a personal-token-only command fails CI
until the document is updated.

Notes:

- **`<task>` is a name or a UUID.** Names are what you and Pepper call a task
  (`morning-weather`), and `scheduled-tasks list` prints both.
- **`ROOM` and `TOPIC` are a name or a UUID too**, matched
  case-insensitively and by unique prefix (`pepper messages list --room trip --topic flights`
  works if "Trip planning" and "Flights" are unambiguous). An ambiguous or
  unknown name lists the candidates and exits nonzero rather than guessing.
  A room's "kind" — `channel` for a named, persistent room, `room` for one
  that's exactly its member set — only shows up on `rooms show`; the list
  endpoint doesn't carry it.
- **Room commands write as you, to the real room.** The ones that affect
  other people or can't be undone (`messages delete`, `rooms leave`)
  ask first on a terminal; `--yes` skips
  the question, and without a terminal and without `--yes` they refuse. A
  room you can't see or aren't in answers "not found, or you are not a member".
- **`<note>` is a name or a UUID**, same matching rules as `<group>`/`<topic>`.
  Note names are not unique, so an exact match against more than one note (or
  an ambiguous prefix) lists the candidates and exits nonzero rather than
  guessing. Omit `--room` for your own notes; pass it to read a room's shared
  ones instead — `<room>` itself follows the same name-or-UUID rule. A
  missing room, a missing note, and a room you're not a member of all answer
  the same "no such room or note, or you are not a member" rather than
  confirming which one it was.
- **`notes new`/`notes edit`** take the body from `$EDITOR` by default, or
  from `--file PATH` (or `--file -` for stdin) read up front instead. `edit`
  sends nothing when the result is byte-identical to what was there (a
  trailing newline `$EDITOR` alone adds doesn't count as a change; `--file`
  and stdin are compared byte for byte) and defaults `If-Match` to the etag
  of the note it just read — `--if-match ETAG` pins an older one, `--force`
  sends none. A stale `If-Match` saves your text to
  `./<note-slug>.<short-etag>.mine.md` (never overwriting an earlier copy:
  a counter is added instead, and the system temp directory is used if the
  current one isn't writable), prints a diff from the server's current body
  to yours, and — on a TTY — offers to re-open the editor, overwrite, or
  quit; off a TTY it exits 1 with the saved path already printed. A
  rename-only edit that conflicts offers only overwrite (the rename alone,
  never the body) or quit. `notes rm` is a soft delete of a note only — an
  id naming an image or link is refused; any room member may delete a
  room's shared note.
- **Delivery has three states, and two of them are not the same.** `--default`
  follows your notification preference and moves when that does; `--silent`
  means announce nowhere, permanently, and a later change to your default
  won't un-silence it. Either way the result still lands in the task's own
  thread, which is what `scheduled-tasks thread` shows.
- **`skills --room <room>` works on the skills shared with a room.** A room
  never owns a skill: sharing puts a live reference to *your* skill in the
  room, so your later edits show there too, and `new`, `edit` and `delete`
  refuse `--room` (edit your own skill and it updates everywhere it is
  shared). Any member can `list`, `show` and `share`; `revoke` is for the
  skill's owner or a room owner, and anyone else is told "only the skill's
  owner or a room owner can revoke it." `list --room` says who shared each
  skill, marks the ones you can revoke with `*`, and says when a room can't
  run one: `inactive` names the toolset a room can't run, `shadowed` means an
  earlier skill of the same name shared there wins. Two members can share
  skills with the same name, so every line ends with a grant id; when a name
  is ambiguous `show` and `revoke` refuse and list the ids, and either takes a
  grant id in place of the name. Only a skill you wrote, with no toolset a
  room can't run, can be shared, and only into a named channel: sharing into
  a group text or an email thread isn't allowed yet.
- **`scheduled-tasks --room <room>` works on a room's tasks instead of yours**
  (`list`, `add`, `pause`, `resume`, `destinations`; `show`, `runs`, `thread`
  and `edit` are yours only). Any member can `list` them. Changing them takes
  the owner role in an open room: anyone else is told "only a room owner can
  change its scheduled tasks." and nothing is written. A room's task is
  recurring (`--cron`, no `--at`, no `--description`) and announces to the
  room rather than to a person: `--to groups_service` (one of the room's
  topics — `--topic NAME` says which) and/or
  `--to resend_email` (every member's inbox). There is no `--default` for a
  room's task — say `--to` or `--silent`. `<task>` is looked up
  among that room's tasks. A server that hasn't switched room tasks on
  answers 404 to all of it.

- **Output adapts to the pipe.** Replies render as rich Markdown when stdout is
  a TTY and as plain text when piped, so `pepper chat 'list my events' | jq`
  works cleanly. One-shot chat waits for the final reply over a stream, allowing
  longer research turns without the ordinary 60-second request timeout. It
  prints only the final reply. If the connection times out or closes before
  that reply, check chat history before resending; the turn may have completed.
- **The REPL streams** model thoughts, tool calls, and tool results as they
  happen over SSE. Slash commands: `/quit`, `/exit`, `/clear`, `/help`, plus one
  `/<skill-name>` entry per skill you own (Tab-completes). Ctrl-D exits.
- **The REPL sets your terminal's window title** to `Pepper` (or `Pepper
  (host)` when `--server` isn't the default — hostname and port only, never
  any credentials in the URL) while it's open, and asks the terminal to
  restore the previous title on exit via the xterm title stack. Restoring is
  best-effort: a terminal that honours the title but not the stack (some
  don't) keeps `Pepper` until your shell prompt or another program sets a
  new one. Only when stdout is a TTY — never for a one-shot
  `pepper chat MESSAGE` or piped/redirected output.
- **The process itself shows as `pepper`** in process lists and in
  tab-naming tools that key off `argv[0]` (a console-script install's
  `argv[0]` is otherwise the `python3` interpreter it's shebanged to).
- **`--root` shares a folder for the length of one session.** `pepper chat
  --root ~/notes "what did I write about the migration?"` declares three
  read-only tools — `fs_list`, `fs_read`, `fs_grep` — that Pepper can call
  back into while the turn runs. They run *here*, on your machine: the server
  sends a tool name and arguments and never a path, and this CLI resolves
  every argument against the roots you named and refuses anything outside them
  (`..`, a symlink pointing out, an absolute path). Search runs locally too, so
  only the matching lines cross the wire — not the file. Repeat `--root` for
  more than one folder. Without it no filesystem manifest is sent and the
  agent receives no local file tools.

  Two things to expect. One-shot chat always uses the streaming path; `--root`
  adds local tool calls on that stream while the final-only output stays the
  same. The server also has to have client tools turned on
  (`client_tools_enabled`); with the flag off your manifest is ignored and the
  turn runs exactly as it would have. `--tools-root` is an accepted alias.

- **`trajectory` is PAT-scoped** — you can read trajectories you own (or that
  are shared with you via a group). Someone else's trajectory returns `404`.

## `pepper next`

Assembles what Pepper already knows into one list: **Now** (the next event, countdown, leave-by),
**Do next** (today's Schedule plan, in its order, with reasons and estimates; when nothing is left
it shows the plan summary then what was left out), **Due soon** (due in 48 hours and not already
above, plus Planning's email deadlines) and **Later** (a count and the first five titles; `--all`
lists them). The CLI never ranks. `--room R` keeps items whose room or source starts with `R`
(case-insensitive) or whose room id is `R`; Now is never filtered. A room item carries its room id
as `principal` (shown as `room <id>` in the text view), so `pepper todos done <todo_id> --room
<principal>` closes it.

On a server without `GET /api/v1/calendars/schedule-plan` it falls back to the open todos list in
the server's order, labelled unranked. "Today" and the 48-hour window are in your account's
timezone, never the machine's.

`--first urgent|important|fast` shows Do next from that one of Pepper's plans (following `same_as`)
instead of Pepper's choice, names it, and adds one line on how the other two differ (or Pepper's
`agree_note` when all agree). It never chooses a plan. On the fallback it prints a stderr note
and the usual list. It never calls a write endpoint: every request is a GET, and it never calls
`schedule-now`. Your token has no scopes, so the server doesn't enforce this, and a few of those
GETs update server-side caches (the requests document, a Google token refresh).

`--json` prints `{"generated_at", "source": "schedule-plan"|"fallback", "now": [item],
"plan": {"first", "name", "subtitle", "chosen_by_pepper", "agree_note", "others": [{"first",
"name", "same_as", "summary", "first_block", "blocks", "differs"}]} (null on the fallback),
"do_next": {"note", "items"}, "due_soon": [item], "later": {"count", "items"}}`. Items keep the
server's own fields; `note` is the plan summary, or the unranked notice on the fallback.

## Troubleshooting

- **A command seems to get sent to Pepper as chat** → your installed `pepper`
  is stale and doesn't know the subcommand. Run `uv tool upgrade vycari-pepper`
  (or use `uv run pepper …` from a checkout).
- **`401` / "log in again"** → token revoked or missing; re-run `pepper login`.
- **"couldn't reach" / transport error** → check the `--server` URL and your
  connection.
- **"refusing to read credentials" permissions error** → `chmod 600
  ~/.config/pepper/credentials.toml`.

## Versioning

`pepper` follows [SemVer](https://semver.org/) starting at 0.1.0. While the version is 0.x, a
minor bump may change flags or output. Within a minor version, fields in `--json` output are
added, never removed. `pepper --version` prints the installed version, and every request sends
it as `User-Agent: pepper-cli/<version>`; a server that no longer supports an old release answers
`426 Upgrade Required`, and `pepper` prints the server's upgrade instruction and exits non-zero.

## Developing the CLI

```bash
uv sync --extra dev
uv run pytest
uv run ruff format --check
uv run ruff check
uv run ty check src/pepper_cli/
```

The CLI mirrors the server's `/api/v1` response shapes (see the `# MIRROR:` comments
in `src/pepper_cli/api_client.py`) and tolerates unknown fields and stream frames.
`CLAUDE.md` describes the layout for contributors and coding agents.

### API contract

`contracts/api-v1.openapi.json` is a vendored copy of the server's published `/api/v1` schema
(see `contracts/README.md`; do not hand-edit it). `tests/test_api_contract.py` checks every
`# MIRROR:`-annotated dataclass against it: each field must be a property of the named server
schema. Adding a field to a mirrored dataclass therefore needs the field in the snapshot first
-- the server change lands, the snapshot is refreshed (a weekly workflow opens the PR), then the
CLI change follows. A mirror the test cannot check must be listed, with a reason, in the test's
allowlists.
