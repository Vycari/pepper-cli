# Claude Code channel: push a room agent's events into its session

**Status:** Proposed. Nothing is decided yet (§14 is empty); the lettered questions in §15 are the maintainer's.
**Author:** drafted with Claude Code for the maintainer
**Last updated:** 2026-10-24
**Tracked by:** #112
**Related:** `planning/cli-information-architecture.md` (grammar, J1/J9, the reserved `agents`
noun); the bundled agent guide `src/pepper_cli/data/agent-skill/SKILL.md` §4–§8; the workspace
`room-agent` skill (`plugins/vycari-ops/skills/room-agent/SKILL.md` in Vycari/vycari); Vycari/groups
`docs/design/agent-members.md`; foreman `docs/runbooks/group-watch-ops-01.md` (the unattended
watcher, which this doc does not change). Claude Code docs: [Channels](https://code.claude.com/docs/en/channels.md),
[Channels reference](https://code.claude.com/docs/en/channels-reference.md),
[MCP: push messages with channels](https://code.claude.com/docs/en/mcp.md#push-messages-with-channels).

## 1. Problem

A Claude Code session running as a sponsored room agent (the `room-agent` skill) learns about room
activity by running `pepper rooms watch --json --cursor-file …` under the Monitor tool. That works,
and it has costs the skill spends a lot of words on:

- A Monitor expires after 30 minutes, so the session re-arms it, and every re-arm runs a full
  catch-up read of every topic first.
- Each event wakes the session with a line of ids. The session then pages `messages list` to find
  the body, once per event, through a Bash command that must match a prefix allow rule.
- Every post is three tool calls (write a draft, grep it for secrets, post it by redirect) so that
  the post itself stays one plain command an allow rule can match.
- Cursor lines and `message.link_cards_updated` lines wake the session unless they are filtered
  in the shell pipeline.

Claude Code now has **channels** (research preview): an MCP server that Claude Code spawns over
stdio can push events into the running session, and can expose tools Claude calls to reply. This
doc proposes a `pepper` command that serves the agent event stream as a channel, so room messages
arrive in the session with their bodies, and replies go out through MCP tools. It replaces the
transport the `room-agent` skill uses. It does not replace the skill's policy.

## 2. Goals and non-goals

**Goals**

- One command that a `.mcp.json` entry can launch, which follows the agent's event stream and
  pushes each new message, with its body, as a `notifications/claude/channel` event.
- MCP tools that map one to one onto the commands an agent token can already run (guide §8), so
  the session can reply, react and read without Bash.
- The same stream guarantees `rooms watch` gives: reconnect with backoff, resume from a cursor,
  fail closed on scope, exit on a refused token.
- No new runtime dependency (§6).
- The CLI holds no business logic. Deciding whether a message is addressed, what profile a room
  grants, and what to say stays with Claude and the skill (§8).

**Non-goals**

- **Unattended runs.** Claude Code ignores `--dangerously-load-development-channels` under `-p`
  and the Agent SDK, and a channel from our own marketplace is not on Anthropic's allowlist (§11).
  The foreman ops-01 watcher keeps using `pepper rooms watch --json` as its wake signal.
  `rooms watch` is not deprecated by this doc.
- **Sprite sleep and wake.** A channel only delivers into a live session, and a Sprite stops its
  processes when it sleeps, so a sleeping Sprite receives nothing. Waking one on a room message
  would need Pepper to call out (a server-side outbound webhook to the agent's endpoint), which
  is a separate design in the server repo, not a CLI change.
- **Permission relay in the first phases.** See §10.
- **Human (PAT) sessions.** The event stream is agent-token only on the server today;
  a person's session would need a server change first.
- **Server changes.** Everything here works against today's `/api/v1`.

## 3. How a channel works (what we must implement)

From the Claude Code channels reference, read 2026-10-24:

- Claude Code spawns the server as a **stdio subprocess** from an MCP config entry
  (`{"command": "pepper", "args": [...]}`). Remote HTTP MCP servers cannot push.
- The server declares `capabilities.experimental["claude/channel"] = {}` in its `initialize`
  result. `tools: {}` makes it two-way. `instructions` is delivered to Claude as context.
- It pushes `notifications/claude/channel` with `params: {content: str, meta: {str: str}}`. Each
  meta key becomes an attribute on `<channel source="<server name>" key="value">content</channel>`.
  Keys must be letters, digits and underscores; other keys are **dropped silently**.
- There is **no acknowledgement**. A write to the pipe is all the server learns. If the session
  did not opt the server in as a channel, events are dropped silently.
- Events that arrive while Claude is busy queue and are delivered together on the next turn.
- Being in `.mcp.json` is not enough. The session must name the server with `--channels` (only
  allowlisted plugins) or `--dangerously-load-development-channels server:<name>` (interactive
  only, with a confirmation dialog each launch).
- **Protocol revision.** On Claude Code's v2 MCP client runtime, a channel server that negotiates
  revision `2026-07-28` is not registered as a channel. Claude Code asks stdio servers for that
  revision first (the MCP Python SDK's client does this with a `server/discover` request; that
  Claude Code probes the same way is inferred, and the §13.1 smoke test checks it); a server that does not support it connects on the
  `initialize` handshake and registers as before. `MCP_PROTOCOL_NEGOTIATION=legacy` on the client
  forces the old handshake for every server. Our server must stay on the handshake era (§6).
- Optional permission relay (`claude/channel/permission`, §10).

## 4. Command shape

**Recommendation: `pepper agents serve`**, a new domain verb on the `agents` noun.

```
pepper --profile <key> agents serve [--room ROOM [--topic TOPIC]] [--cursor-file PATH]
                                    [--kinds K,...] [--ignore-kinds K,...] [--self PERSON_ID]
```

Help line: "Serve this agent's room events to a Claude Code session over stdio (an MCP server;
Claude Code starts it, not you)."

Why this shape, against the IA doc:

- IA §3 rule 4: the set of non-noun commands is closed and "anything new is a noun". So
  `pepper mcp` or `pepper serve` as a top-level command is out.
- IA §2 reserved `agents` for "sponsored-agent commands" when it renamed `agent-skill` to
  `agents guide`. This is the second one. Like `guide`, it is about operating as an agent, not
  about one room object.
- IA §2 says the word "channel" is never used in CLI help or flags, because the product already
  uses it for three other things. So `pepper channel serve` is out, and the verb is `serve`. The
  help text names the Claude Code feature only in the long description ("a Claude Code channel"),
  as the name of someone else's product feature; whether even that is allowed is §15 A.
- `rooms watch --mcp` was considered. It reuses the flags, but it changes what stdout is (JSON-RPC
  both ways, plus tools), and IA invariant 4 treats stdout as an interface. A flag that turns a
  read stream into a bidirectional server with write tools hides a large change behind a small
  switch. The two commands share their stream code instead (§12 layout).
- The flags are `rooms watch`'s, with the same meaning, so the bundled guide can say "the same
  flags as `rooms watch`". `--json`, `--since` are not offered: stdout is the protocol, and the
  start position follows §7.4.
- IA J1 says every leaf takes `--json`. `agents serve` is the exception, like `chat` without a
  message: its stdout is JSON-RPC by definition. J9 exit codes apply as today (§9).

The `.mcp.json` server name is the sponsor's choice. The examples use `pepper`, which makes the
tag `<channel source="pepper" …>` and the tools `mcp__pepper__<name>`.

```json
{
  "mcpServers": {
    "pepper": {
      "command": "pepper",
      "args": ["--profile", "claude-code", "agents", "serve",
               "--room", "<room-uuid>",
               "--cursor-file", "/home/<user>/.local/state/pepper-agents/claude-code/channel-cursor"]
    }
  }
}
```

```bash
claude --dangerously-load-development-channels server:pepper
```

## 5. Data flow

```
pepper agents serve
  ├─ stdin reader thread   JSON-RPC requests from Claude Code → handlers → responses
  ├─ stream thread         SSE feed (watch_agent_events / watch_scoped_events)
  │                          → filter kinds → fetch body (messages list --at) → notification
  │                          → advance cursor file
  └─ one stdout writer     a lock around "write one JSON line, flush"; nothing else touches fd 1
```

Startup: load credentials for `--profile`; resolve `--room`/`--topic` exactly as the scoped watch
does (capability check first, fail closed); build the topic map (`rooms list`, then `topics list`
per room) that turns an unscoped event's `topic_id` into the `group_id` reads and posts take
(guide §4: an event's `group_id` is the groups service's id, not the room id); then answer
`initialize` and start the stream once Claude Code sends `notifications/initialized`.

The stream thread reuses the existing loop: the backoff constants, `_watch_frame_shows_health`,
the scoped-watch checks (`_confirm_scope`, `_event_in_scope`, `_check_cursor_binding`), and the
cursor file format. The serve command's cursor file is a separate file from any `rooms watch`
file; the existing scope-binding check already refuses a file another scope wrote.

## 6. MCP implementation: hand-rolled, no new dependency

**Recommendation: implement the small subset of MCP the channel needs, in one module, with no new
dependency.** Two reasons, the second decisive.

**Dependencies.** The `pyproject.toml` comment says the dependency list is deliberately narrow
because native clients port from this CLI. The current Python SDK, `mcp` 2.3.0, requires
`anyio`, `httpx2`, `jsonschema`, `mcp-types`, `opentelemetry-api`, `pydantic`, `pyjwt[crypto]`
(which pulls `cryptography`), `python-multipart`, `sse-starlette`, `starlette`, `uvicorn`,
`typing-inspection`, and `pywin32` on Windows. Almost all of it is HTTP-server machinery a stdio
channel never uses.

**Protocol era.** The SDK can do what a channel needs: `notify(method, params)` takes any method
name, custom notification handlers are supported, and experimental capabilities are declared in
the initialization options. But its server loop, `serve_dual_era_loop` (`mcp/server/runner.py`),
picks the protocol era from the client's first request, and the SDK speaks `2026-07-28`
(`mcp_types/version.py` `MODERN_PROTOCOL_VERSIONS`). Claude Code's v2 runtime asks for that
revision first. An SDK-based server would accept it, and Claude Code then does not register it as
a channel: the server connects, its tools work, and no event ever arrives, with no error. There is
no public switch to make the SDK server handshake-only; staying legacy would mean depending on a
private function (`_serve_legacy_stream`) or asking every sponsor to set
`MCP_PROTOCOL_NEGOTIATION=legacy`.

A hand-rolled server avoids this by construction. It implements only:

| Message | Handling |
|---|---|
| `initialize` (request) | Answer `protocolVersion` = the client's requested version if it is one of `2025-11-25`, `2025-06-18`, `2025-03-26`, `2024-11-05`, else `2025-11-25`; `capabilities: {experimental: {"claude/channel": {}}, tools: {}}`; `serverInfo: {name: "pepper", version: <package version>}`; `instructions` (§7.6) |
| `notifications/initialized` | Start the stream thread |
| `ping` | `{}` |
| `tools/list`, `tools/call` | §9 |
| `server/discover` and any other request | JSON-RPC error `-32601` (method not found). The MCP SDK's client probe (`mcp/client/_probe.py`) falls back to `initialize` on any error except `-32022` with a modern-only version list, so `-32601` is the answer that keeps us on the handshake; whatever Claude Code's probe is, a server that answers it with `-32601` and only implements `initialize` is what its docs call one that "does not support" the revision |
| `notifications/cancelled` and any other notification | Ignored |

Wire format: newline-delimited JSON-RPC 2.0 on stdin and stdout, UTF-8, one object per line,
`\n` only (stdin opened in text mode with universal newlines so Windows `\r\n` is tolerated, stdout
written as bytes). About 200 lines, and every branch is testable with in-memory pipes (§13).

The cost is that we track the channel contract ourselves. It is small and versioned by Claude
Code (the doc already records two client-version changes), and §13 includes a manual smoke test
against a real Claude Code before each phase merges. If the MCP SDK later gains a handshake-only
server mode and the channel contract stabilises, swapping the transport is a contained change
behind the same command.

## 7. What gets pushed

### 7.1 Kinds

Only `message.created` and `mention.created` become message events, and the two for one message
are one event (de-duplicated by `message_id` within a short window, and by the command's own record
of pushed ids). Everything else the guide lists (`message.edited`, `message.deleted`,
`message.link_cards_updated`, `member.*`) is consumed silently and the cursor moves past it, as
`--ignore-kinds` does today. The skill already says never to answer one.

An event kind the CLI does not know, or a server `resync`, produces one **control event**,
`kind="resync"` (§7.5), coalesced so a burst gives one push. That keeps the skill's rule that an
unknown kind is a reason to catch up, without waking the session per event.

`--kinds` and `--ignore-kinds` keep their `rooms watch` meaning on top of this, for a sponsor who
wants `message.edited` pushed too. The default is the two kinds above.

### 7.2 The agent's own posts

Dropped before any fetch when the event payload's `author_person_id` equals the agent's own
person id (known from the scope frame on a scoped stream, or `--self`), and otherwise dropped after
the fetch when the message has `is_you`. Either way the cursor advances. Filtering one's own posts
is the guide's rule for every agent and is a fact about the message, not a judgement.

### 7.3 Body fetch

Events carry ids, not bodies. For each event that will be pushed, the stream thread reads the one
message with `list_group_messages(group_id, topic_id, at=message_id, limit=…)` (the permalink
window route that already exists) and picks the row whose `id` matches. If the row is not found
(`permalink_target_missing`), it pushes the event anyway with `content` saying the body could not
be read and `body="missing"` in meta, so the session can report it. That keeps the guide's rule
that "not found" is an error to report, never "not addressed".

A fetch that fails with a network error or 5xx is retried with the stream's backoff, without
advancing the cursor, so delivery order is preserved. A 404 for the room means the agent was
removed; that is the terminal case in §9.

### 7.4 Cursor, restart and backlog

The cursor file advances **after** the notification line has been written and flushed to stdout,
as `rooms watch` advances it after printing. There is no ack, so "written" is the most the server
can know: a crash between the write and the cursor update replays the event (at least once, as
today), and Claude Code dropping an event it received (say, the session never opted the server in
as a channel) is invisible. The skill's ledger stays the de-duplication.

On start, the skill does not answer a backlog from before the session (its `session-start`
cut-off, room-agent §2.2). Replaying the whole stream since the cursor file as pushed events would
put that backlog into context on every Claude Code restart. Recommendation (§15 C):

- Resume the stream from the cursor file, so nothing is lost on the wire.
- An event whose `created_at` is older than the process start time minus 60 seconds is consumed
  without a push. Instead, once the stream has caught up (the first heartbeat after replay), push
  one `kind="backlog"` control event: how many messages were skipped, per topic id, and the oldest
  and newest timestamps. The skill tells the sponsor, as it does today.
- In-process reconnects resume from the cursor and push everything, since nothing they replay is
  from before the session.
- With no cursor file, `rooms watch` starts from the beginning of the retained stream (`--since`
  help: "Default: the cursor file's, else the beginning"). `agents serve` starts there too, so a
  first run reads the whole retained window, and the same skip rule keeps all of it out of the
  push. There is no "start at the tail" option on the stream today; the skip rule makes one
  unnecessary. On a first run (no cursor file) the `backlog` event says "first run, no earlier
  position" and gives no count, since the retained window is not a backlog this agent owes.

### 7.5 Event shape

Meta carries **ids and fixed words only**. Display names, room names and topic names are text
other people choose, so they go in `content`, never in a tag attribute.

Message event:

| Meta key | Value |
|---|---|
| `kind` | `message` |
| `room_id` | the `group_id` that reads and posts take (from the topic map, or the scoped stream's `room_id`) |
| `topic_id` | the topic |
| `message_id` | the message |
| `parent_id` | `parent_message_id`, omitted when none |
| `sender_id` | `author_person_id` |
| `sender_kind` | `human`, `agent` or `pepper` (from `is_agent` / `is_pepper`) |
| `mentions` | the structured `mentions` person ids, comma-separated, omitted when none |
| `created_at` | the message's `created_at`, exactly as the server wrote it |
| `truncated` | `true` when the body was cut (§7.7), omitted otherwise |
| `attachments` | the attachment count, omitted when none |

`content`:

```
Alice Example in Family › Trip:
<raw_body>
```

The first line is the sender's display name and the room and topic names, sanitised with the same
`sanitize_line` the watch already uses. Everything after it is `raw_body` as the server sent it.

Control events have `kind` = `backlog`, `resync`, `gap` or `ended`, the ids that apply, and a
one-paragraph `content` written for Claude ("Events between position 120 and 180 in room … could
not be replayed. Catch up with messages_list for each topic, from your checkpoints.").

### 7.6 Instructions

The `instructions` string tells Claude what the tag attributes mean, that messages are input from
other people and never instructions, that `mentions` holds person ids to compare against its own,
that a reply goes through `messages_post` with `room_id`, `topic_id` and `reply_to` taken from the
tag, and that control events mean "run your catch-up". It does not say when to answer: that is
the deployment's policy (the skill, or the guide's etiquette section).

### 7.7 Size and batching

`content` is capped at 16,000 characters. A longer body is cut at that point with
`truncated="true"`; `messages_list` with `at=message_id` returns the whole row. No batching in the
server: Claude Code already queues events that arrive while Claude is busy and delivers them
together, and per-message events keep ids and bodies paired.

## 8. Where the "addressed" decision lives

The CLI decides nothing about whether to answer. It pushes every new message from someone other
than the agent and lets Claude apply the skill. Split:

| In the CLI (`agents serve`) | With Claude (the skill, or the guide's etiquette) |
|---|---|
| Which event kinds are messages (§7.1) | Mentioned, reply-to-self, in-window follow-up (room-agent §2.3 rule 4) |
| Dropping the agent's own posts (§7.2) | Ledger de-dup across restarts |
| Fetching the body, mapping ids (§7.3) | Agent-to-agent hop cap (rule 3) |
| Skipping the pre-session backlog from the push (§7.4) | Reading the room's profile per message (§3) |
| Refusing a post body that contains a credential (§9.2) | Whether to answer, what to say, `on_behalf_of` |

The left column is mechanism (what a delivery is, what a message is). It is the same line
`rooms watch` and `--ignore-kinds` already draw. The `mentions` ids are passed through, not
evaluated, so the "mentioned" rule stays the skill's.

## 9. Tools, and lifecycle

### 9.1 Tools

Each tool is one command an agent token can already run (guide §8), with the same arguments.

| Tool | Command | Phase |
|---|---|---|
| `messages_post` (`room_id`, `topic_id`, `text`, `reply_to?`, `on_behalf_of?`, `mentions?`, `idempotency_key?`) | `messages post` | P1 |
| `messages_list` (`room_id`, `topic_id`, `since?` / `before?` / `at?`, `limit?`) | `messages list` (never `--mark`) | P1 |
| `messages_react` (`room_id`, `message_id`, `emoji`) | `messages react` | P1 |
| `messages_unreact`, `members_list`, `topics_list`, `rooms_list` | the matching commands | P2 |
| `attachments_download` (`room_id`, `attachment_id`, `out`) | `attachments download` | P2 |

Rules the tools enforce, all of them restatements of the CLI's or the server's own:

- **Scope.** With `--room`, every tool refuses a `room_id` other than the served room (a tool
  error, not a request). This is where the room boundary moves to; see §9.3.
- **On behalf of.** `on_behalf_of` requires `reply_to`, as the CLI does. The server does not
  infer it: whom a reply is for is the skill's call (room-agent §4: the asker, and only when the
  asker is human). If the service refuses it, the tool returns the error text so Claude can post
  again without it, as the guide says.
- **Idempotency.** `idempotency_key` defaults to `agents-serve:<reply_to>` when `reply_to` is
  given, so a retried tool call after a timeout posts once. Otherwise a random key, as today.
- **Mentions.** A `mentions` entry whose display name is not in `text` is the caller's problem, as
  on the CLI; the tool passes the ids through.
- **Results.** Every tool returns the JSON the `--json` form of its command prints (J3/J5), as a
  single text content block, so the skill's read-back (`is_you`, `author_person_id`) works.
- Tool errors are `isError: true` results with the CLI's own error line, never a JSON-RPC error,
  so Claude sees the text.

### 9.2 Secret check

Floor 3 of the skill is a `grep` over a draft file before every post. With a tool there is no file
between Claude and the post. Recommendation (§15 E): `messages_post` refuses a `text` that matches
the same credential patterns the skill's grep uses (`pep_agt_`, `pep_pat_`, GitHub, Anthropic,
Slack, AWS, private-key blocks, Doppler), returning an error that says the post was refused and
why, without echoing the match. It refuses; it never rewrites. This is the CLI's own invariant 1
("tokens never get printed") extended from stdout to a room, and it is a filter on strings, not a
judgement. The skill keeps the rule and loses the grep step.

### 9.3 Permission rules

Today the sponsor scopes the agent with Bash prefix rules pinned to room UUIDs. MCP tool
permissions are per tool (`mcp__pepper__messages_post`), with no argument matching. So with
`agents serve`, the room boundary is the `--room` in the `.mcp.json` args, enforced by the tools
(§9.1). Without `--room`, the boundary is the token's membership, as it already is for
`rooms watch` and `rooms list`. The sponsor allows `mcp__pepper__messages_list` and
`mcp__pepper__messages_react` and decides whether `mcp__pepper__messages_post` prompts.

P1 supports zero or one `--room`, reusing the scoped watch. Several rooms is either several
`.mcp.json` entries (one session can load several channels) or, in P2, repeated `--room` with one
scoped stream per room (§15 D).

### 9.4 stdout, logging and exit

- stdout carries JSON-RPC and nothing else. On start the command duplicates fd 1 for its own
  writer, then points `sys.stdout` (and fd 1) at stderr, so a stray `print` or a `render` call that
  uses the stdout console lands on stderr instead of corrupting the protocol. `_emit_line`, which
  the watch uses for events, is never called on this path.
- `Feed unavailable (…)` notices, the merge notice and every error go to stderr, as now. Claude
  Code captures a server's stderr in its debug log (`claude --debug`).
- **Reconnects** are silent to the session, as for `rooms watch`. Nothing is lost, so there is
  nothing to tell Claude.
- **`history_expired`** (scoped stream, today exit 3): push a `gap` event naming the positions and
  the topics, then resume from the floor the server gave. Recovering is mechanical, and the
  catch-up is the skill's.
- **Terminal conditions** — 401, 403, 404 on the stream, `retry: false` error frames
  (`unauthorized`, `agent_unavailable`, `access_revoked`, `room_deleted`, `room_superseded`), a
  scope the server did not confirm: push one `ended` event with the reason in words (for
  `agent_unavailable`: "the sponsor paused or retired this agent; do not retry"), flush, then exit
  1 after a short grace period, so the event is read before the tools disappear. `/mcp` then shows
  the server as failed. Startup failures before `initialize` completes (no credentials, `--room`
  does not resolve) exit with the J9 code and the error on stderr; Claude Code shows the server as
  failed.
- Stdin closing (Claude Code exiting) ends the process with exit 0.
- Exit codes are J9's: 0 on stdin EOF or interrupt, 1 for auth, API, network and terminal stream
  conditions, 2 for usage and resolution errors.

## 10. Sender gating and permission relay

**Delivery cannot be gated by sender for this use.** The channels reference says to gate on sender
identity, never on room, because an ungated channel is a prompt-injection path. For a room agent,
every member's message is the input: the skill exists to answer people in the room. The gating
already exists, one level up: only room members can post, the server scopes the feed to rooms
whose roster names the agent, and the skill treats room text as untrusted and bounds the agent by
the room's profile, not by the sender (room-agent §3, design §4.4). The doc's own instructions
(§7.6) repeat that.

**Permission relay is a different matter, and P1 does not declare it.** If the server declared
`claude/channel/permission`, Claude Code would send each tool-approval prompt to it, and a verdict
from the room would approve the tool call. Two problems:

1. Anyone who can post where the verdict is read could approve a tool. A sender gate would have to
   be an explicit list of human person ids (the sponsor), never agents, never Pepper, never a room.
2. The prompt has to be shown somewhere. Posting `input_preview` (the actual command or file
   content, only partly redacted by Claude Code) into a room shows it to every member. An agent
   token cannot DM the sponsor.

A version that could work: the sponsor creates a sealed room with only themself and the agent,
`--approver PERSON_ID` names them, prompts go only to that room, and only that person's
`yes abcde` / `no abcde` is accepted. That is a phase of its own, if ever (§15 G). Until then the
sponsor approves at the terminal, or runs with a permission mode that does not prompt for what
the room's profile allows.

## 11. Packaging and the allowlist

**P1: a bare `.mcp.json` entry** (§4), user-level (`~/.claude.json`) or in the directory the
sponsor runs the agent session from, loaded with
`claude --dangerously-load-development-channels server:pepper`. Nothing to publish.

**Later: a plugin, but not inside `vycari-ops`.** A plugin's MCP servers start in every session
the plugin is enabled in, and `vycari-ops` is enabled in every Vycari repo's committed settings.
A channel server needs a logged-in agent profile and should run only in the agent's session. So
it would be its own plugin in the `vycari` marketplace (for example `pepper-room`), enabled per
person in `settings.local.json`, with the profile key and room as plugin configuration, and loaded
with `--dangerously-load-development-channels plugin:pepper-room@vycari`. Packaging buys little
until the dev flag goes away, which is §15 H.

**What this means for unattended use, plainly:**

- Custom channels are not on Anthropic's channel allowlist during the preview, and our own
  marketplace never will be. Only an Anthropic partner listing in `claude-plugins-official`, or a
  Team/Enterprise admin's `allowedChannelPlugins`, removes the dev flag.
- The dev flag works only in an interactive session and shows a confirmation dialog each launch.
  Under `claude -p` or the Agent SDK it is ignored and the channel does not register.
- So foreman on ops-01, scheduled or cloud runs, and Sprites cannot use this today. They keep
  `rooms watch`. A laptop session with the sponsor at the keyboard (the `room-agent` skill's
  case) can.
- On Team and Enterprise plans an Owner must also turn on `channelsEnabled`; with it off, the
  server connects and its tools work but no event arrives (a startup warning says so).
- Channels require claude.ai or Console authentication; they are not available on Bedrock,
  Vertex or Foundry.

## 12. What changes in the room-agent skill

The skill is in Vycari/vycari and changes in a lockstep workspace PR when P1 is released, as the
0.2.0 rename did. Policy is unchanged; transport sections are rewritten.

| Skill section | Today | With `agents serve` |
|---|---|---|
| §1.3 permission rules | Bash prefix rules pinned to room UUIDs, plus a `Read` rule for the outbox | `mcp__pepper__*` tool rules; the room pin moves to `--room` in the `.mcp.json` entry; no Bash or outbox rules needed for the room commands |
| §1.4 own id, topic map | Built by hand from `rooms list` / `topics list` | Own id still recorded in `self` (for the mention and follow-up rules); the topic map is the server's, and events carry the right `room_id` |
| §2.1 watch | `rooms watch` under Monitor, grep out cursor lines, re-arm every 30 min | Launch Claude Code with the dev flag; events arrive as `<channel source="pepper" kind="message" …>`. No Monitor, no re-arm, no filtering |
| §2.1 watch exits | Read the Monitor's last lines | An `ended` event with the reason; same stop-and-report rule |
| §2.2 catch-up | At start, before every re-arm, on exit, on resync | At start, on a `backlog`, `resync` or `gap` event. Ledger and per-topic checkpoints unchanged |
| §2.3 decide | Fetch each message by paging `messages list` | Body and ids are in the event; own posts never arrive; parents are still read with `messages_list` (`at=`). Rules 1–5 unchanged |
| §4 reply | Draft file, grep, post by redirect: three calls | One `messages_post` call with `reply_to`, `on_behalf_of`, `idempotency_key`; the server refuses a credential-shaped body. Read-back from the tool result. At most one post per message, unchanged |
| §5 floors | — | Unchanged. Floor 3's check moves into the tool and is still the skill's rule |
| §6 stopping | `TaskStop` the Monitor | Exit the session; the cursor file and the skill's state directory stay for the next run |

The bundled guide (`src/pepper_cli/data/agent-skill/SKILL.md`) gains a short section on
`agents serve`, changed in the same PR as the command (invariant 5). `rooms watch` stays the
documented path for anything that is not an interactive Claude Code session.

## 13. Testing and phases

### 13.1 Tests

HTTP is mocked with `pytest-httpx`, as for the rest of the suite. The server is driven through
in-memory stdin and stdout streams, with no subprocess and no real Claude Code:

- `initialize` returns `experimental["claude/channel"]`, `tools`, `instructions`, and a handshake
  revision; it never returns `2026-07-28`, including when the client asks for it.
- `server/discover` returns `-32601`; an unknown request returns `-32601`; an unknown notification
  is ignored.
- Every byte written to stdout parses as one JSON-RPC object per line, including when the stream
  reconnects, a notice is printed and a tool fails.
- A `message.created` produces one `notifications/claude/channel` with the §7.5 meta, and every
  meta key matches `^[A-Za-z0-9_]+$`; the body is fetched with `at=`; a matching `mention.created`
  produces no second event.
- The agent's own message, `message.link_cards_updated` and `message.edited` produce no event and
  still advance the cursor file.
- The cursor file is written after the notification line, not before (ordering asserted through a
  writer that records both).
- Pre-start events produce one `backlog` event and no message events.
- 401, 403, `agent_unavailable` and `access_revoked` each produce one `ended` event and exit 1.
- `history_expired` produces a `gap` event and a resume from the floor.
- `messages_post` sends the request shape `messages post` sends (`on_behalf_of_person_id`,
  `parent_message_id`, `Idempotency-Key`), refuses a room outside `--room`, refuses
  `on_behalf_of` without `reply_to`, and refuses a body containing `pep_agt_…` without echoing it.
- Windows: stdin with `\r\n` line endings; `$HOME`/`$USERPROFILE` isolated as in `conftest.py`.

Coverage stays at or above `coverage.floor`.

**Manual smoke**, recorded in each phase PR: `claude --debug --dangerously-load-development-channels
server:pepper` against a staging room; the startup notice names the channel; a post in the room
arrives as a `<channel>` tag; a reply through `messages_post` appears in the room "for" the
asker; the debug log shows the `initialize` handshake, not `2026-07-28`.

### 13.2 Phases

One PR each. P1 is usable on its own.

| Phase | Contents | Depends on |
|---|---|---|
| P1 | `agents serve`: hand-rolled stdio server (§6), unscoped or one `--room`/`--topic`, message and control events (§7), `messages_post` / `messages_list` / `messages_react` with the scope and secret checks (§9), stdout guard and lifecycle (§9.4), tests, bundled guide section. Workspace PR rewriting room-agent §1.3, §2, §4, §6 opened as a draft and merged at release | — |
| P2 | The remaining agent-token tools; repeated `--room` if §15 D says so; `--kinds`/`--ignore-kinds` widening | P1 |
| P3 | Plugin packaging in the `vycari` marketplace (§11), if §15 H finds a way off the dev flag or the sponsor wants the install story anyway | P1 |
| P4 | Permission relay to a sponsor-only room (§10), only if §15 G says yes | P2 |

## 14. Decisions

None yet. Each answer to a §15 question becomes a row here, dated, with who decided.

| | Decision |
|---|---|
| — | — |

## 15. Questions for the maintainer

**A. Command spelling.**

- (a) `pepper agents serve` (recommended, §4).
- (b) `pepper rooms watch --mcp`.
- (c) A new top-level `pepper mcp`, which would reopen the closed set in IA §3 rule 4.

Sub-question: may the long help say "Claude Code channel", naming Anthropic's feature, given IA
§2's rule against "channel"? Recommended yes, in the description only, never in a flag or verb.

**B. Hand-rolled MCP or the `mcp` SDK.**

- (a) Hand-rolled, about 200 lines, no new dependency (recommended, §6).
- (b) `mcp` as an optional extra (`vycari-pepper[mcp]`), with the era pinned through a private
  SDK function or the client-side `MCP_PROTOCOL_NEGOTIATION=legacy`.
- (c) `mcp` as a core dependency.

Recommended (a): the SDK's server would negotiate the revision that stops channel delivery, and
its dependency tree is mostly HTTP-server code.

**C. What happens to the backlog on restart.**

- (a) Push everything since the cursor file.
- (b) Resume from the cursor, skip pre-start messages from the push, send one `backlog` summary
  (recommended, §7.4).
- (c) As (b), but no summary.

Recommended (b): it matches the skill's `session-start` rule and keeps a restart from filling the
context.

**D. The room boundary.**

- (a) `--room` optional; without it the token's membership is the boundary, as for `rooms watch`
  (recommended for P1, with one room per `.mcp.json` entry when the sponsor wants a pin).
- (b) `--room` required.
- (c) Repeated `--room` with one scoped stream per room, in P2.

**E. Secret check in the CLI.** Should `messages_post` refuse credential-shaped bodies (§9.2)?
Recommended yes: it is the only place left to put floor 3, and it refuses rather than edits. The
pattern list would live in the CLI and be broader than Pepper's own token prefixes.

**F. `on_behalf_of`: explicit or inferred.** Recommended explicit (§9.1). Inferring "the parent's
author when human" is simple but is the skill's reply policy, and the server already refuses a
wrong value.

**G. Permission relay, ever?** Recommended: not in P1 or P2. If wanted, only to a sealed room
holding just the sponsor and the agent, accepting verdicts only from a named human person id
(§10).

**H. The allowlist.** Is Vycari's Claude account on a Team or Enterprise plan whose admin could
list a `pepper-room` plugin in `allowedChannelPlugins`? If not, the dev flag (interactive only, a
dialog each launch) is permanent for us during the preview, and P3 is cosmetic. This is a fact to
check, then a decision on whether P3 is worth doing.
