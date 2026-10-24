# Claude Code channel: push a room agent's events into its session

**Status:** Proposed; design decided. The maintainer answered every first-round question on
2026-10-24 (§14). §15 lists what is still to verify.
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
The draft MCP Events extension ([SEP-3415](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/3415),
[modelcontextprotocol/experimental-ext-triggers-events](https://github.com/modelcontextprotocol/experimental-ext-triggers-events))
and [ChatGPT's support for it](https://developers.openai.com/plugins/build/mcp-events) (§2.1).

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
doc proposes `pepper agents claude-channel`, a command that serves the agent event stream as a
channel, so room messages arrive in the session with their bodies, and replies go out through MCP
tools. It is an adapter for Claude Code only: `claude/channel` is a vendor-prefixed experimental
capability, not part of MCP, and the command's name says so (§4). It replaces the
transport the `room-agent` skill uses. It does not replace the skill's policy.

## 2. Goals and non-goals

**Goals**

- One command that a `.mcp.json` entry can launch, which follows the agent's event stream and
  pushes each new message, with its body, as a `notifications/claude/channel` event.
- MCP tools that map one to one onto the commands an agent token can already run (guide §8), so
  the session can reply, react and read without Bash.
- The same stream guarantees `rooms watch` gives: reconnect with backoff, resume from a cursor,
  fail closed on scope, exit on a refused token.
- Built on the `mcp` Python SDK, held to the handshake protocol era (§6).
- The CLI holds no business logic. Deciding whether a message is addressed, what profile a room
  grants, and what to say stays with Claude and the skill (§8).

**Non-goals**

- **Unattended runs in P1.** Claude Code ignores `--dangerously-load-development-channels` under
  `-p` and the Agent SDK. An unattended channel would have to be an allowlisted plugin under a
  Team account (§11), which is P3 at the earliest. The foreman ops-01 watcher keeps using
  `pepper rooms watch --json` as its wake signal. `rooms watch` is not deprecated by this doc.
- **Sprite sleep and wake.** A channel only delivers into a live session, and a Sprite stops its
  processes when it sleeps, so a sleeping Sprite receives nothing. Waking one on a room message
  would need Pepper to call out to the agent. MCP Events (§2.1) is that shape, and it belongs on
  the Pepper server, not in the CLI.
- **MCP Events.** Tracked, not designed here (§2.1, §14 A).
- **Permission relay in the first phases.** See §10.
- **Human (PAT) sessions.** The event stream is agent-token only on the server today;
  a person's session would need a server change first.
- **Server changes.** Everything here works against today's `/api/v1`.

### 2.1 Related work: MCP Events

The draft standard for this kind of push is **MCP Events**, from the MCP Triggers & Events Working
Group: [SEP-3415](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/3415) (open as
of 2026-10-24), incubated in
[modelcontextprotocol/experimental-ext-triggers-events](https://github.com/modelcontextprotocol/experimental-ext-triggers-events).
OpenAI's ChatGPT supports it ([MCP Events](https://developers.openai.com/plugins/build/mcp-events)).
It is a different model from a Claude Code channel:

- It runs on a **remote, authenticated MCP endpoint**, the same one that serves the tools. It is
  not a stdio child of the client.
- The server advertises the `events` capability (in its `server/discover` result) and serves
  `events/list`, `events/subscribe` and `events/unsubscribe`.
- Delivery is an HTTPS POST to a callback URL the subscriber registers, one event per request,
  signed with Standard Webhooks (`webhook-id`, `webhook-timestamp`, `webhook-signature`). ChatGPT
  supports webhook delivery only, and not the draft's `gap` and `terminated` control notifications.
- It requires protocol revision `2026-07-28`, which is the revision Claude Code will not register
  a channel on (§3).

So MCP Events belongs on the Pepper server's hosted MCP endpoint, with subscriptions stored
server-side, and not in this CLI. **Decision (§14 A): track it, and defer the Pepper server design
until the draft settles.** It also fits a case channels cannot: a webhook can wake a sleeping
Sprite on a room message, where a channel needs a live session. If Claude Code adopts MCP Events,
`agents claude-channel` can retire in favour of the server endpoint.

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
  Claude Code probes the same way is inferred, and the §13.1 smoke test checks it). A server that
  does not support it connects on the
  `initialize` handshake and registers as before. `MCP_PROTOCOL_NEGOTIATION=legacy` on the client
  forces the old handshake for every server. Our server must stay on the handshake era; §6 says
  how it does with the SDK.
- Optional permission relay (`claude/channel/permission`, §10).

## 4. Command shape

**Decided (§14 A): `pepper agents claude-channel`**, a new domain verb on the `agents` noun.

```
pepper --profile <key> agents claude-channel [--room ROOM [--topic TOPIC]]
                                             [--cursor-file PATH] [--kinds K,...]
                                             [--ignore-kinds K,...] [--self PERSON_ID]
```

Help line: "Serve this agent's room events to a Claude Code session as a Claude Code channel, over
stdio (an MCP server; Claude Code starts it, not you)."

Why this shape, against the IA doc:

- IA §3 rule 4: the set of non-noun commands is closed and "anything new is a noun". So
  `pepper mcp` or `pepper serve` as a top-level command is out.
- IA §2 reserved `agents` for "sponsored-agent commands" when it renamed `agent-skill` to
  `agents guide`. This is the second one. Like `guide`, it is about operating as an agent, not
  about one room object.
- IA §2 says the word "channel" is never used in CLI help or flags, because the product already
  uses it for three other things. **`claude-channel` is a deliberate, named exception to that
  rule (§14 A).** "Channel" here is Anthropic's product term for the Claude Code feature, and the
  `claude-` prefix keeps it from reading as a Pepper concept. The command is an adapter for one
  vendor's experimental capability (`claude/channel`), so the name says which vendor until a
  standard exists (§2.1). The exception covers this verb and its help text only. The IA doc is not
  changed, and no other command or flag may use the word.
- `rooms watch --mcp` was considered. It reuses the flags, but it changes what stdout is (JSON-RPC
  both ways, plus tools), and IA invariant 4 treats stdout as an interface. A flag that turns a
  read stream into a bidirectional server with write tools hides a large change behind a small
  switch. The two commands share their stream code instead (§5).
- The flags are `rooms watch`'s, with the same meaning, so the bundled guide can say "the same
  flags as `rooms watch`". `--json`, `--since` are not offered: stdout is the protocol, and the
  start position follows §7.4.
- IA J1 says every leaf takes `--json`. `agents claude-channel` is an exception, like `chat` without a
  message: its stdout is JSON-RPC by definition. J9 exit codes apply as today (§9).

The `.mcp.json` server name is the sponsor's choice. The examples use `pepper`, which makes the
tag `<channel source="pepper" …>` and the tools `mcp__pepper__<name>`.

```json
{
  "mcpServers": {
    "pepper": {
      "command": "pepper",
      "args": ["--profile", "claude-code", "agents", "claude-channel",
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
pepper agents claude-channel
  ├─ event loop (anyio)    mcp stdio_server + serve_loop: initialize, ping, tools/* (§6)
  │                          tool calls → api_client in a worker thread
  ├─ stream worker thread  SSE feed (watch_agent_events / watch_scoped_events)
  │                          → filter kinds → fetch body (messages list --at)
  │                          → notification on the write stream → advance cursor file
  └─ stdout                owned by stdio_server; one JSON object per line
```

Startup: load credentials for `--profile`; resolve `--room`/`--topic` exactly as the scoped watch
does (capability check first, fail closed); build the topic map (`rooms list`, then `topics list`
per room) that turns an unscoped event's `topic_id` into the `group_id` reads and posts take
(guide §4: an event's `group_id` is the groups service's id, not the room id); then answer
`initialize` and start the stream worker from the `notifications/initialized` handler
(`Server.add_notification_handler`).

The stream thread reuses the existing loop: the backoff constants, `_watch_frame_shows_health`,
the scoped-watch checks (`_confirm_scope`, `_event_in_scope`, `_check_cursor_binding`), and the
cursor file format. The `claude-channel` cursor file is a separate file from any `rooms watch`
file; the existing scope-binding check already refuses a file another scope wrote.

## 6. MCP implementation: the `mcp` SDK, held to the handshake era

**Decided (§14 B): the command is built on the `mcp` Python SDK, as a core dependency.** The
earlier draft recommended a hand-rolled JSON-RPC server to avoid the dependency; the maintainer
chose the SDK. This section says how the SDK is used for the three things a channel needs that a
plain MCP server does not.

**Dependency.** `pyproject.toml` gains `mcp>=2.3,<3` in `dependencies`, with a comment in the
style of the `textual` one: what it is for (`pepper agents claude-channel`, this doc), why it is
core and not an extra (§14 B), and that it is imported only from the `claude-channel` command path,
so no other command pays its import time. The PR that adds it gives that reason, as the
`pyproject.toml` comment asks of every new runtime dependency. `mcp` 2.3.0 pins `mcp-types` to the
same version, and pulls in `anyio`, `httpx2`, `jsonschema`, `opentelemetry-api`, `pydantic`,
`pyjwt[crypto]` (and so `cryptography`), `python-multipart`, `sse-starlette`, `starlette`,
`uvicorn`, `typing-extensions`, `typing-inspection`, and `pywin32` on Windows. Most of that is
HTTP-server code a stdio channel never runs. That size is a known cost the maintainer accepted:
the narrow-dependency comment in `pyproject.toml` exists because native clients port from this
CLI, and this command is Claude Code-specific, so no native client ports it. Its `requires-python`
(`>=3.10`) is no constraint on ours (`>=3.13`).

All file and symbol references below are to the `mcp` 2.3.0 wheel, read 2026-10-24. Each of the
three mechanisms is read from source, not run, and **the P1 smoke test (§13.1) against a real
Claude Code is what verifies them**. P1 does not merge until it passes.

**1. Declaring `claude/channel`.** The low-level `mcp.server.lowlevel.Server` builds the
`initialize` result from `create_initialization_options(experimental_capabilities=…)`. P1 passes
`{"claude/channel": {}}` there. Registering the tool handlers (`tools/list`, `tools/call`) makes
the SDK add `tools` to the capabilities itself, and `instructions` (§7.6) is a `Server`
constructor argument. `serverInfo.name` comes from the `Server` name, `pepper`.

**2. Sending `notifications/claude/channel`.** The typed `ServerSession.send_notification` takes
the SDK's closed union of spec notifications, so it is the wrong door for a vendor method. The
command opens the transport itself with `mcp.server.stdio.stdio_server()`, which yields the read
and write streams, and keeps a clone of the write stream. The stream worker pushes an event by
sending `SessionMessage(JSONRPCNotification(jsonrpc="2.0", method="notifications/claude/channel",
params={"content": …, "meta": {…}}))` on that stream, the same object type the SDK's own replies
travel as. `stdio_server` serialises one JSON object per line and owns stdout. The worker is the
existing synchronous watch loop running in a thread (`anyio.to_thread.run_sync`), and it hands
each notification to the event loop with `anyio.from_thread.run`. Tool calls reuse the existing
synchronous `api_client` the same way, through `to_thread`.

**3. Staying on a revision Claude Code registers channels on.** This is the one that matters
most, because getting it wrong fails silently: the server connects, its tools work, and no event
ever arrives. `Server.run()` drives `serve_dual_era_loop` (`mcp/server/runner.py`), which lets
the client's first request pick the era. A request carrying the `2026-07-28` `_meta` envelope,
such as a `server/discover` probe, opens a modern connection, and Claude Code then does not
register the server as a channel. So **the command does not call `Server.run()`.** It runs the
same steps `run()` does (enter `server.lifespan(server)`, then drive a loop over the stdio
streams), but drives `mcp.server.runner.serve_loop` instead of `serve_dual_era_loop`.
`serve_loop` is public (it is in the module's `__all__`), is documented as "handshake-only loop
mode", and is what the SDK's own streamable-HTTP manager calls. On that loop:

- `initialize` negotiates the client's requested version if it is a handshake version
  (`2025-11-25`, `2025-06-18`, `2025-03-26`, `2024-11-05`), else `LATEST_HANDSHAKE_VERSION`
  (`2025-11-25`). It never answers `2026-07-28` (`ServerRunner._negotiate_initialize`).
- A `server/discover` before `initialize` is checked against the connection's seeded version,
  `2025-11-25`. That method is not defined there, so the request gets `-32601` (method not
  found). The SDK's own client probe (`mcp/client/_probe.py`) treats any error other than
  `-32022` with a modern-only version list as "fall back to `initialize`". That Claude Code's
  client behaves the same way is inferred, not observed. The smoke test checks it.

Two fallbacks if a later SDK release changes `serve_loop`. They are not plans. The first is
`MCP_PROTOCOL_NEGOTIATION=legacy` set in the `.mcp.json` `env` of the sponsor's Claude Code
session, which forces the handshake for every server in that session. The second is a thin filter
on the read stream that answers an enveloped first request with `-32601` before the SDK sees it.
The `<3` upper bound on the dependency, and a test that asserts the negotiated version is never
`2026-07-28` (§13.1), are what catch a change.

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

The cursor file advances **after** the notification has been handed to the SDK's stdout writer,
as `rooms watch` advances it after printing. With the SDK, that hand-off is a send on a
zero-buffer memory stream: it returns when `stdio_server`'s writer task has taken the message,
just before it writes and flushes. A crash in that window could lose one event. That is narrower
than any Claude Code-side loss below and accepted; the skill's catch-up covers it. There is no ack, so "written" is the most the server
can know: a crash between the write and the cursor update replays the event (at least once, as
today), and Claude Code dropping an event it received (say, the session never opted the server in
as a channel) is invisible. The skill's ledger stays the de-duplication.

On start, the skill does not answer a backlog from before the session (its `session-start`
cut-off, room-agent §2.2). Replaying the whole stream since the cursor file as pushed events would
put that backlog into context on every Claude Code restart. Decided (§14 C):

- Resume the stream from the cursor file, so nothing is lost on the wire.
- An event whose `created_at` is older than the process start time minus 60 seconds is consumed
  without a push. Instead, once the stream has caught up (the first heartbeat after replay), push
  one `kind="backlog"` control event: how many messages were skipped, per topic id, and the oldest
  and newest timestamps. The skill tells the sponsor, as it does today.
- In-process reconnects resume from the cursor and push everything, since nothing they replay is
  from before the session.
- With no cursor file, `rooms watch` starts from the beginning of the retained stream (`--since`
  help: "Default: the cursor file's, else the beginning"). `agents claude-channel` starts there too, so a
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

| In the CLI (`agents claude-channel`) | With Claude (the skill, or the guide's etiquette) |
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
- **On behalf of (§14 F).** The caller passes `on_behalf_of` explicitly; it requires `reply_to`,
  as the CLI does. The command does not
  infer it: whom a reply is for is the skill's call (room-agent §4: the asker, and only when the
  asker is human). If the service refuses it, the tool returns the error text so Claude can post
  again without it, as the guide says.
- **Idempotency.** `idempotency_key` defaults to `claude-channel:<reply_to>` when `reply_to` is
  given, so a retried tool call after a timeout posts once. Otherwise a random key, as today.
- **Mentions.** A `mentions` entry whose display name is not in `text` is the caller's problem, as
  on the CLI; the tool passes the ids through.
- **Results.** Every tool returns the JSON the `--json` form of its command prints (J3/J5), as a
  single text content block, so the skill's read-back (`is_you`, `author_person_id`) works.
- Tool errors are `isError: true` results with the CLI's own error line, never a JSON-RPC error,
  so Claude sees the text.

### 9.2 Secret check

Floor 3 of the skill is a `grep` over a draft file before every post. With a tool there is no file
between Claude and the post. Decided (§14 E): `messages_post` refuses a `text` that matches
the same credential patterns the skill's grep uses (`pep_agt_`, `pep_pat_`, GitHub, Anthropic,
Slack, AWS, private-key blocks, Doppler), returning an error that says the post was refused and
why, without echoing the match. It refuses; it never rewrites. This is the CLI's own invariant 1
("tokens never get printed") extended from stdout to a room, and it is a filter on strings, not a
judgement. The skill keeps the rule and loses the grep step.

### 9.3 Permission rules

Today the sponsor scopes the agent with Bash prefix rules pinned to room UUIDs. MCP tool
permissions are per tool (`mcp__pepper__messages_post`), with no argument matching. So with
`agents claude-channel`, the room boundary is the `--room` in the `.mcp.json` args, enforced by the tools
(§9.1). Without `--room`, the boundary is the token's membership, as it already is for
`rooms watch` and `rooms list`. The sponsor allows `mcp__pepper__messages_list` and
`mcp__pepper__messages_react` and decides whether `mcp__pepper__messages_post` prompts.

Decided (§14 D): in P1 `--room` is optional, and zero or one is accepted, reusing the scoped
watch. A sponsor who wants several pinned rooms uses several `.mcp.json` entries (one session can
load several channels) until P2 adds repeated `--room`, with one scoped stream per room.

### 9.4 stdout, logging and exit

- stdout carries JSON-RPC and nothing else. `stdio_server()` claims fd 1 for the protocol and
  points the process's fd 1 at stderr while it serves (and fd 0 at the null device), so a stray
  `print` or a `render` call on the stdout console lands on stderr instead of corrupting the
  protocol. `_emit_line`, which the watch uses for events, is never called on this path. A test
  holds this (§13.1), since it depends on SDK behaviour.
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
`yes abcde` / `no abcde` is accepted. Decided (§14 G): no relay in P1 or P2, and if it is ever
built, only in this sealed-room, sponsor-person-id form. Until then the
sponsor approves at the terminal, or runs with a permission mode that does not prompt for what
the room's profile allows.

## 11. Packaging and the allowlist

**P1: a bare `.mcp.json` entry** (§4), user-level (`~/.claude.json`) or in the directory the
sponsor runs the agent session from, loaded with
`claude --dangerously-load-development-channels server:pepper`. Nothing to publish.

**P3: a plugin, but not inside `vycari-ops`.** Decided (§14 H): worth doing. A plugin's MCP
servers start in every session the plugin is enabled in, and `vycari-ops` is enabled in every
Vycari repo's committed settings. A channel server needs a logged-in agent profile and should run
only in the agent's session. So it is its own plugin in the `vycari` marketplace (for example
`pepper-room`), enabled per person in `settings.local.json`, with the profile key and room as
plugin configuration.

**Accounts.** Vycari uses a mix of Team and Max accounts, and the two load the plugin differently.
The documented behaviour (Claude Code channels docs, "Enterprise controls" and "Research
preview", read 2026-10-24):

- **Team.** An Owner turns on `channelsEnabled`, and a Team admin can list the plugin in
  `allowedChannelPlugins` (`{"marketplace": "vycari", "plugin": "pepper-room"}`), which replaces
  Anthropic's default list for the organisation. Then `claude --channels plugin:pepper-room@vycari`
  loads it with no development flag and no dialog. With `channelsEnabled` off, the server connects
  and its tools work but no event arrives, and a startup warning says so. Both settings are
  organisation settings that only a human admin changes. This doc and its phases change none.
- **Max** (no organisation). Org policy checks are skipped, but `--channels` still accepts only
  Anthropic's default allowlist, which our marketplace is not on. So a Max sponsor keeps
  `--dangerously-load-development-channels plugin:pepper-room@vycari`: interactive only, with a
  confirmation dialog each launch.
- Channels require claude.ai or Console authentication. They are not available on Bedrock,
  Vertex or Foundry.

**Unattended use.** If unattended channel use is wanted, it has to run under a Team account with
the plugin allowlisted. The development flag is documented as ignored under `-p` and the Agent
SDK. For an allowlisted `--channels` plugin under `-p`, the docs do not say so in so many words,
but they describe it as working: "When you run channels in non-interactive mode with `-p`, tools
that need terminal input … are disabled so the session never stalls". That is documented, not
tested by us. Before anyone builds on it, it is checked with a Team account once P3 exists (§15).
Until then, foreman on ops-01, scheduled or cloud runs, and Sprites keep `rooms watch`. A laptop
session with the sponsor at the keyboard (the `room-agent` skill's case) uses the channel from P1.

## 12. What changes in the room-agent skill

The skill is in Vycari/vycari and changes in a lockstep workspace PR when P1 is released, as the
0.2.0 rename did. Policy is unchanged; transport sections are rewritten.

| Skill section | Today | With `agents claude-channel` |
|---|---|---|
| §1.3 permission rules | Bash prefix rules pinned to room UUIDs, plus a `Read` rule for the outbox | `mcp__pepper__*` tool rules; the room pin moves to `--room` in the `.mcp.json` entry; no Bash or outbox rules needed for the room commands |
| §1.4 own id, topic map | Built by hand from `rooms list` / `topics list` | Own id still recorded in `self` (for the mention and follow-up rules); the topic map is the server's, and events carry the right `room_id` |
| §2.1 watch | `rooms watch` under Monitor, grep out cursor lines, re-arm every 30 min | Launch Claude Code with the dev flag (P1, or a Max account) or `--channels plugin:pepper-room@vycari` (Team, after P3); events arrive as `<channel source="pepper" kind="message" …>`. No Monitor, no re-arm, no filtering |
| §2.1 watch exits | Read the Monitor's last lines | An `ended` event with the reason; same stop-and-report rule |
| §2.2 catch-up | At start, before every re-arm, on exit, on resync | At start, on a `backlog`, `resync` or `gap` event. Ledger and per-topic checkpoints unchanged |
| §2.3 decide | Fetch each message by paging `messages list` | Body and ids are in the event; own posts never arrive; parents are still read with `messages_list` (`at=`). Rules 1–5 unchanged |
| §4 reply | Draft file, grep, post by redirect: three calls | One `messages_post` call with `reply_to`, `on_behalf_of`, `idempotency_key`; the server refuses a credential-shaped body. Read-back from the tool result. At most one post per message, unchanged |
| §5 floors | — | Unchanged. Floor 3's check moves into the tool and is still the skill's rule |
| §6 stopping | `TaskStop` the Monitor | Exit the session; the cursor file and the skill's state directory stay for the next run |

The bundled guide (`src/pepper_cli/data/agent-skill/SKILL.md`) gains a short section on
`agents claude-channel`, changed in the same PR as the command (invariant 5). `rooms watch` stays the
documented path for anything that is not an interactive Claude Code session.

## 13. Testing and phases

### 13.1 Tests

HTTP is mocked with `pytest-httpx`, as for the rest of the suite. The server is driven through
`stdio_server(stdin=…, stdout=…)` with in-memory text streams (explicit streams skip the fd claim),
with no subprocess and no real Claude Code:

- `initialize` returns `experimental["claude/channel"]`, `tools`, `instructions`, and a handshake
  revision; it never returns `2026-07-28`, including when the client asks for it.
- `server/discover` before `initialize` returns `-32601`, including with the `2026-07-28` `_meta`
  envelope; the connection then accepts `initialize` and negotiates a handshake revision. This is
  the test that catches an SDK upgrade changing `serve_loop` (§6).
- `initialize` declares `experimental["claude/channel"]`, and a pushed event goes out as a
  `notifications/claude/channel` line with the `content`/`meta` params unchanged.
- Every byte written to stdout parses as one JSON-RPC object per line, including when the stream
  reconnects, a notice is printed, a stray `print` runs, and a tool fails.
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
asker; the debug log shows the `initialize` handshake, not `2026-07-28`. This smoke test is what
verifies the three SDK mechanisms in §6 (the capability, the custom notification, and the
handshake era) against real Claude Code. P1 does not merge without it.

### 13.2 Phases

One PR each. P1 is usable on its own.

| Phase | Contents | Depends on |
|---|---|---|
| P1 | `agents claude-channel`: `mcp` SDK server on `serve_loop` (§6), the `mcp` dependency, unscoped or one `--room`/`--topic`, message and control events (§7), `messages_post` / `messages_list` / `messages_react` with the scope and secret checks (§9), stdout guard and lifecycle (§9.4), tests, bundled guide section. Workspace PR rewriting room-agent §1.3, §2, §4, §6 opened as a draft and merged at release | — |
| P2 | The remaining agent-token tools; repeated `--room` (§14 D); `--kinds`/`--ignore-kinds` widening | P1 |
| P3 | Plugin packaging in the `vycari` marketplace (§11, §14 H). Allowlisting it is a Team admin's step, outside the PR | P1 |
| P4 | Not planned. Permission relay to a sealed sponsor-only room (§10) only, if it is ever wanted (§14 G) | P2 |

## 14. Decisions

All decided 2026-10-24 by Allen.

| | Decision |
|---|---|
| A | The command is `pepper agents claude-channel`. It is a Claude Code-only adapter, since `claude/channel` is a vendor-prefixed experimental capability, so the name says so until a standard exists. "Channel" in the verb is a deliberate, named exception to IA §2's ban, because it is Anthropic's product term. The IA doc is otherwise unchanged (§4). MCP Events (SEP-3415) is the draft standard: track it, and defer a Pepper server design until the draft settles. If Claude Code adopts it, this adapter can retire (§2.1) |
| B | Use the `mcp` Python SDK as a core dependency, not a hand-rolled server. The dependency's size is a known, accepted cost. The implementation keeps the handshake era by driving `serve_loop` rather than `Server.run()`, verified in the P1 smoke test (§6) |
| C | On restart, resume from the cursor, skip pre-session messages from the push, and send one `backlog` summary event (§7.4) |
| D | `--room` is optional in P1 (zero or one); repeated `--room` comes in P2 (§9.3) |
| E | `messages_post` refuses credential-shaped bodies, never echoes the match, and never rewrites (§9.2) |
| F | `on_behalf_of` is passed explicitly by the caller, never inferred (§9.1) |
| G | No permission relay in P1 or P2. If ever, only to a sealed room with the sponsor's person id as the sole approver (§10) |
| H | Vycari uses a mix of Team and Max accounts, so P3 (a `pepper-room` plugin in the `vycari` marketplace) is worth doing: a Team admin can allowlist it for `--channels`. Max accounts keep the dev flag. Unattended channel use, if wanted, runs under a Team account (§11). No org settings change as part of this work |

## 15. Still to verify

No design questions are open. These are facts to check, each with its owner:

- **The three SDK mechanisms in §6** (capability, custom notification, `serve_loop` keeping the
  handshake era, and Claude Code falling back on `-32601`) are read from the SDK's source and not
  run against real Claude Code yet. The P1 smoke test checks them. If the era check fails, the
  fallbacks in §6 apply, and the choice between them comes back to the maintainer.
- **An allowlisted `--channels` plugin under `-p`.** Documented as working (§11), not tested.
  Checked with a Team account after P3, by whoever first wants an unattended channel.
- **The Team settings.** Turning on `channelsEnabled` and adding `pepper-room` to
  `allowedChannelPlugins` are steps for a Team Owner or admin, when P3 lands. They are not done here.
