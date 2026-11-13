# The command-line app

If you live in a terminal, there's a `pepper` command-line app. It talks to the
same Pepper as text, email, and web chat — same conversation, same memory — so
you can start something in one place and pick it up here. It's handy for quick
questions without leaving your shell, and for piping Pepper's answers into other
commands.

You sign in through your browser with the same Google account you use on the
web app — `pepper login` opens a page where you confirm a short code, and the
CLI is signed in as you and only you. Your Google password never touches the
terminal.

## Setting it up

1. **Install `uv`** (the tool that runs the CLI), if you don't have it:
   - macOS / Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
   - macOS with Homebrew: `brew install uv`
   - Windows: see the [uv install guide](https://docs.astral.sh/uv/getting-started/installation/)
2. **Install the app:**
   ```bash
   uv tool install vycari-pepper
   ```
   That puts a `pepper` command on your PATH. To update it later, run
   `uv tool upgrade vycari-pepper`. The app's source and release notes are at
   [Vycari/pepper-cli](https://github.com/Vycari/pepper-cli).
3. **Log in:** run `pepper login`. It shows a short code (like `BCDF-GHJK`) and
   opens your browser — sign in with Google if asked, check the code matches,
   and click **Approve**. The CLI finishes on its own. Only approve a code you
   just requested yourself.

On a machine with no browser (a server over SSH), run `pepper login
--no-browser` and open the printed link from any device, or create a token
under [**Settings → API tokens**](/settings) on the web app and run
`pepper login --token <token>`.

**Already installed it the old way?** Earlier installs came from a git or
local path and the tool was named `pepper-cli`. The app now has its own
package, `vycari-pepper`, and the old install won't update to it. Once, run:

```bash
uv tool uninstall pepper-cli
uv tool install vycari-pepper
```

You stay signed in: your login carries over to the new install, so only the
install command changes.

Each sign-in shows up under **Settings → API tokens** named after
the machine (like `CLI: allen@laptop`) — revoke it there to sign that machine
out remotely. Tokens expire 90 days after their last use, so a machine you
use regularly stays signed in on its own; one that goes quiet for that long
will need a fresh `pepper login`.

Check it worked with `pepper whoami`. `pepper logout` deletes the stored
credentials from this machine when you're done with it.

## Using it

- **Ask something quickly:** `pepper what's on my calendar today` — no quotes
  needed, even for multi-word messages.
- **Have a conversation:** just type `pepper`. In a terminal that opens the
  full-screen view (below). `pepper chat` opens the simpler line-by-line
  session instead, where Pepper streams its thinking and replies as it goes:
  type `/help` inside for the available commands, and `/` then Tab to see your
  saved skills. Ctrl-D leaves.
- **Let Pepper read files on this machine:** add `--root PATH` to `pepper chat`
  (or `pepper tui`, or `pepper run`) and Pepper can read the files under that
  folder for this session only. It can only read them, never change them, and
  nothing outside the folders you name is reachable. Repeat `--root` for more
  than one folder.
- **Save a prompt as a script:** `pepper run FILE` sends the file's text as one
  message. Start the file with `#!/usr/bin/env -S pepper run`, make it
  executable, and it runs like any other command. Anything you pass after the
  file name reaches the prompt: `{{1}}`, `{{2}}` and so on are replaced by
  those arguments, `{{@}}` by all of them, and any argument the prompt doesn't
  mention is added at the end so Pepper still sees it.
- **Send a draft:** when Pepper drafts an email or a change to an event in the
  terminal, it prints the draft with a `pepper drafts send <id>` line under it.
  Nothing is sent until you run that.
- **Pipe it into other tools:** when you send output to another command (like
  `pepper "list my events" | grep ...`), Pepper switches to plain text so it
  pastes and parses cleanly. A one-shot command waits for the final reply even
  when a research turn takes several minutes. If the connection times out or
  closes before the reply, check the web chat before sending the same request
  again; Pepper may already have saved the turn.

That's the everyday surface. There are a couple of extra commands for reviewing
your past turns (`pepper trajectories list` and `pepper trajectories show`) if you
ever want to look back at a specific conversation in detail.

- **Manage your skills from the terminal:** `pepper skills list` shows your saved
  skills alongside Pepper's built-ins, `pepper skills show <name>` prints one, and
  `pepper skills create <name>` / `pepper skills edit <name>` open it in your editor
  to write or change (or take the text from `--file PATH`, `--file -` for stdin;
  `pepper skills delete <name>` removes one of your own — the built-ins can't be
  deleted). It's the same set of skills you'd manage on the web
  Settings page, just from your shell. See [Skills](#skills) for what skills are.
- **Skills in a room:** where rooms can have skills shared with them from the
  terminal, `pepper skills share <name> --room <room>` shares one of your own
  skills with a room, and `pepper skills revoke <name> --room <room>`
  takes it back out (the skill itself stays yours, untouched). `pepper skills
  list --room <room>` shows what a room has, who shared each one, and whether
  the room can run it; `pepper skills show <name> --room <room>` prints one.
  Anyone in the room can look and share; taking one out is for whoever owns the
  skill or owns the room. The room gets your skill as it is now and as you edit
  it later — there's no separate room copy to keep up to date.
- **Your to-do list:** `pepper todos list` shows what's open, `pepper todos create`
  puts something on it, and `pepper todos done` / `pepper todos dismiss` close
  one (`pepper todos reopen` puts it back). Add `--room <room>` to any of them
  to work on a room's list instead. Same list as everywhere else — see [Todos](#todos).
- **Your calendar, read-only:** `pepper calendar` shows today's agenda with
  weather, daylight and commute; `pepper calendar week` shows the next seven
  days, and `pepper calendar schedule` adds timed to-do deadlines alongside
  your events. Add `--start`/`--days` to pick any window up to 31 days, or
  `--planning` for followed events, email suggestions and suggestion cards —
  up to 7 days, and not together with `schedule`. It only reads — changing
  an event still happens in chat. Same calendar as everywhere else — see
  [Calendar](#calendar).
<!-- rollout: calendar-publish -->
- **Publishing your calendar:** `pepper calendar publish` (or `publish show`)
  says whether your calendar is published to another calendar, busy-only or
  full details, what is published, and which calendars are shared with you and
  whether their titles show (never the private address: only a fingerprint is
  stored). `pepper calendar publish on` starts publishing and prints the
  private address once; the first time, it includes your Google calendars and
  shows a notice that anyone with the address can see them, which needs your
  confirmation. `pepper calendar publish rotate` gets a new address,
  which stops the old one for good and every calendar subscribed to it until it
  is given the new one, and prints the new address once.
  `pepper calendar publish off` stops publishing and destroys the address for
  good; publishing again makes a new one. `off` and `rotate` ask first (`--yes`
  skips). `pepper calendar publish set` changes what is published (the Google
  calendars, which follows are left out, meals, workouts and to-dos, and which
  shared calendars keep their titles hidden) and `--detail busy|full`. Adding
  the first Google calendar shows a notice that anyone with the address can see
  it, and needs your confirmation. The address is a credential, so keep it out
  of shared screens and logs, and get a new address if it ends up somewhere
  shared. See [Calendar](#calendar).
<!-- /rollout -->
<!-- rollout: entity-graph -->
- **The people Pepper has seen:** `pepper graph list` shows your strongest
  ties, strongest first. `pepper graph search <name or address>` looks one
  person up. Each person shows their addresses, when you last mailed or met,
  how close the tie is (for example "#2 of 40, top 5") and your recent
  meetings. `pepper graph upcoming` lists your next meetings with who is on
  each, and `pepper graph show <event id>` does the same for one event. People
  you have never mailed or met are marked "new to you". It only reads, and the
  data comes from the headers of your own mail and calendar attendees, never
  message text. If a read is slow it shows what it had and says so.
<!-- /rollout -->
- **What's next:** `pepper next` shows what's on now (with the time left) and
  what's after it, the rest of today's calendar, and your top ten open to-dos
  in the order Pepper ranks them (`--all` for every one). `--room <room>` (its
  id, or a unique name prefix) shows a room's to-dos instead, and
  `--source <prefix>` keeps only the to-dos from one source. Read-only, same
  as `calendar` above, and every time is in your account's timezone.
- **Your morning brief:** `pepper brief` shows your latest brief, the same one
  Pepper sends you each morning; `--date YYYY-MM-DD` shows an earlier one
  (about a month is kept). `pepper brief --now` asks Pepper to put together a
  fresh brief now: it arrives wherever your brief is usually delivered, about a
  minute later. See [Reminders & scheduling](#reminders-and-scheduling).
- **What you follow:** `pepper following list` shows the teams, shows,
  podcasts, sites and events you follow, by group, with the latest news on
  each; `pepper following show <item>` shows one in more detail.
  `pepper following search <words>` finds something new to follow (a team
  name, a show, a podcast, or a website's address) and numbers the matches;
  `pepper following create <number>` follows one of them. `pepper following
  delete <item>` stops following it. See [Following](#following).
- **What's new in what you follow:** `pepper feed list` lists the new
  stories, episodes, scores and posts from everything you follow, best first
  as Pepper ranks them, and numbered. `pepper feed show <number>` opens one: its summary and link, plus
  `--transcript` for a podcast's transcript, `--read` to fetch the article
  itself and print it in the terminal, or `--open` to open it in your browser.
  See [Feeds](#feeds).
- **Everything Pepper keeps for you:** `pepper artifacts list` lists your
  saved files, links and notes, newest first and numbered (`--kind`, `--label`
  and `--search` narrow it; `--room <room>` lists a room's). `pepper artifacts
  show <number>` prints one, `pepper artifacts download <number>` saves a
  file's contents, `pepper artifacts create --file PATH` uploads a picture,
  PDF, text file, audio clip or video, and `pepper artifacts delete <number>`
  removes one. See [Notes](#artifacts).
- **Your calendar preferences:** `pepper preferences show` prints your home
  and work addresses, timezone, working days and hours, meal and workout times,
  and how your calendar is drawn. `pepper preferences edit` changes only the
  settings you name, for example `--timezone America/Denver` or
  `--work-days mon-fri --work-start 09:00 --work-end 17:30`. It's the same as
  **Calendar → Preferences** on the web. See [Calendar](#calendar).
- **Research projects:** `pepper research-projects list` shows your standing
  research projects (`--room <room>` for a room's), and `show <project>` gives
  one in full; `--report` prints its report. `create` starts a new one from a
  brief you write in your editor, `run` starts a run now (`--wait` waits for
  it to finish), `steer` leaves guidance for the next run, `edit` changes the
  brief or puts it on a schedule, and `pause` / `resume` stop and restart it.
  `pepper research-items list --project <project>` shows what a project has
  found, numbered: `research-items show <number>` prints one, `take` keeps a
  finding (**Keep** in the app), and `reconsider` brings back one you ruled
  out (**Bring back**). See [Research projects](#research).
- **A fortune:** `pepper fortune` prints a short fortune from Pepper. It works
  offline and needs no sign-in. Now and then a small pepper also appears when
  you start a session; set `PEPPER_NO_WHIMSY=1` if you'd rather it didn't.
- **Your scheduled prompts:** `pepper scheduled-tasks list` shows the ones
  currently running; add `--all` to include paused ones and one-offs that have
  already fired. `show` prints one in full, and `runs` / `thread` show what it has
  done and what it said. You can change them too: `create` makes one, `edit`
  changes its wording, its schedule or where its results go (`--to`, `--silent`,
  `--default`; `--deliver-topic <name>` picks the topic for a room's task),
  `pause` and `resume` stop and restart it without losing its history, and
  `renew` keeps one going. A
  paused task hasn't gone anywhere — it just won't be in the default list, so
  reach for `--all` when you're looking for one you stopped. It's the same set of
  tasks as the **Scheduled tasks** page on the web — see
  [Reminders & scheduling](#reminders-and-scheduling). Where rooms can have
  scheduled tasks of their own, add `--room <room>` to `list`, `create`, `edit`, `pause`
  or `resume` to work on that room's tasks instead: anyone in the
  room can list them, and only one of the room's owners can add or change one.
- **Your rooms:** every room command is `pepper <noun> <verb>`, and the room
  is always the `--room <room>` flag (its id, or a unique name prefix).
  `pepper rooms list` shows the rooms you're in and `pepper rooms show <room>`
  gives one room's info; `rooms create`, `edit`, `archive`, `unarchive`, `leave`,
  `mute`, `unmute` and `activity` do what they say. `pepper topics list --room
  <room>` lists its topics (also `create`, `edit`, `archive`, `unarchive` and
  `search`), and `pepper messages list --room <room> --topic <topic>` prints a
  topic's messages oldest to newest (`--limit`, `--before`, `--since`; `--mark`
  marks them read). `pepper messages post --room <room> --topic <topic>
  <message>` sends one as yourself — attach a file with `--attach`, thread it as
  a reply with `--reply-to`, or mention someone with `--mention` so it pings
  them. `messages edit`, `delete`, `react` and `unreact` work on your messages;
  `pepper attachments download --room <room> <attachment> --out PATH` saves a
  file. `pepper members list --room <room>` lists who's in, and `members
  invite`, `remove` and `edit --role owner|member` change that. `pepper sharing
  show|edit --room <room>` chooses which of your calendars a room sees
  (`--set CALENDAR=LEVEL`), and `pepper apps list|install|uninstall --room
  <room>` manage its apps. Anything that can't be undone asks first (`--yes`
  skips the question). Run `pepper <noun> --help` for the list. Same rooms as
  **Messages** in the web app — see [Ways to reach Pepper](#channels).
- **Your notes:** `pepper notes list` shows your own, and `pepper notes list
  --room <room>` shows a room's shared ones instead. `pepper notes search
  "<query>"` looks for text in a note's name or body. `pepper notes show
  <note>` prints one's contents; add `--json` to any of these for the raw
  data instead of the formatted view. `pepper notes create "<name>"` and
  `pepper notes edit <note>` write the body from `$EDITOR`, or from
  `--file PATH`/`--file -` for stdin; `edit` warns and offers to re-open,
  overwrite, or quit if someone else changed the note first. `pepper notes
  delete <note>` deletes one (asks first, unless `--yes`). Same notes as the
  **Notes** app on the web — see [Notes](#artifacts).
- **Comment on a room's note:** `pepper comments list --room <room> --note <note>`
  prints its comment threads; add `--all` to include resolved ones. `pepper
  comments create --room <room> --note <note> "<text>"` comments on the whole
  note, or add `--quote "<passage>"` to comment on a specific passage (it has
  to appear exactly once in the note); `--reply-to <thread>` replies inside an
  existing thread instead, and `--mention <person>` pings someone. `pepper
  comments resolve --room <room> --note <note> <thread>` and `comments reopen`
  close or reopen one. Personal notes can't be commented on. Same comments as
  the room's own Notes board — see [Notes](#artifacts).
- **Scripting:** add `--json` to a command (or set `PEPPER_JSON=1`) for JSON
  on stdout instead of the formatted view. A list comes back as an object with
  an `items` array and, when there is more, a `next_before` cursor to pass to
  `--before`; with `--json` an error is a one-line `{"error": {...}}` on stderr.
- **A copy of your data:** `pepper takeout` downloads the same zip archive as
  **Download your data** in **Settings → Your data** — conversations,
  memories, files, schedules and other records. See [Your data](#your-data).

## The full-screen view

In a terminal, bare `pepper` (or `pepper tui`) opens a full-screen view of
your messages: your own Todos and Notes at the top, then your conversation
with Pepper, then your rooms by most recent activity with their topics under
the open room. A dot marks a conversation with something unread, and `@`
marks one where someone mentioned you. It reads the same unread state as the
web app, so what you read in one place clears in the other.

The box at the bottom sends to whatever is open. In Pepper's conversation,
Enter sends a message and the reply streams into the thread. In a room's
topic, Enter posts as you; type `@` and part of a name to mention someone.
Alt+Enter (or Ctrl+J) starts a new line, and each conversation keeps its own
half-written draft while you look elsewhere.

In the thread, Up and Down pick a message. Then `r` quotes it in your reply,
`+` reacts, `e` and `d` edit or delete one of yours, and `o` opens its first
link or attachment in your browser. If a draft from Pepper appears under a
reply, `s` sends it after you confirm.

Open **Todos** or **Notes** (or type `/todo` or `/notes`) to work on your
lists without leaving the terminal: tick a to-do done with Space, add one with
`n`, or write and edit notes in your own editor with `c` and `e`. In a room,
`a` opens its installed apps and `i` shows who's in it. Ctrl+K jumps to any
room, topic or list by name, Ctrl+N goes to the next unread conversation, and
Ctrl+Q quits.

It keeps up with new messages as they arrive, and if the connection drops it
says so and keeps retrying. Set `PEPPER_TUI_BELL=1` to have it ring the
terminal bell when someone mentions you in another conversation. If you'd
rather have the line-by-line session every time, use `pepper chat`, or set
`PEPPER_NO_TUI=1`.

<!-- rollout: agent-sponsors -->
## Agents you sponsor

Some accounts can sponsor an **agent**: a member of your rooms with its own
name, run by a program you host (a "harness") rather than by a person. This is
turned on per account; if it's on for you, **Settings → Agents** appears on
the web app. There you create an agent, give it a name, and mint it a
token. In the iPhone app, **Account & security → Agents** does the rest of
this (mint a token, pause, edit, retire, revoke), but creating an agent takes
the web app on a computer. The token is shown once — give it to the harness, which runs
`pepper --profile <name> login --token <token>` and can then use
`pepper --profile <name> rooms list`, `messages list`, `messages post`, and the
other room commands, as the agent, in the rooms the agent has been invited to. It can
also read and edit the notes of those rooms — `pepper --profile <name> notes
list|search|show|edit --room <room>` — and comment on one, the same `pepper
comments` commands described above. An agent token can't touch anyone's personal
notes, and can't do anything in Pepper outside the rooms it belongs to.

`--profile <name>` (letters, digits, `.`, `_` and `-`, given before the
command) keeps the agent's sign-in separate from your own on the same
machine, so running an agent never signs you out. The CLI won't save an
agent token without one.

To have the harness woken by new activity instead of checking on a timer, run
`pepper --profile <name> rooms watch`. It holds one connection open and prints a line when
something happens in any of the agent's rooms; add `--json` for one JSON object
per line, which suits a script reading a pipe. Events say what happened and
where (they carry ids, not message text), so the harness reads the message with
`pepper --profile <name> messages list` as before. Give it `--cursor-file PATH` and it remembers
where it got to, picks up from there after a restart, and reconnects by itself
if the connection drops. To hear about only some kinds of activity, add
`--kinds` with the ones you want (for example `message.created,mention.created`)
or `--ignore-kinds` with the ones you don't; skipped events still move the
cursor on, so they aren't delivered later. It exits with an error if the token
is revoked or the agent is paused. The harness has no need to poll.

### Watching one room or one topic

```
pepper rooms watch --room ROOM [--topic TOPIC] [--since CURSOR] [--cursor-file PATH] [--json]
```

`--room` takes a room id, or a name or unique name prefix. `--topic` names a topic in that room and requires `--room`. The server narrows the stream, so events from your other rooms are never sent. Output, `--kinds`/`--ignore-kinds`, reconnects and backoff work as for the unscoped watch; scoped events also carry `room_id`, the id that `messages list --room` and `messages post --room` take.

A room scope leaves out mentions of you in other rooms. A topic scope also leaves out room-wide events such as membership changes and renames. Run a separate unscoped watch if you need those.

The command fails closed. If the server does not advertise scoped watch, if a connection does not open by confirming the room and topic you asked for, or if an event outside that scope arrives, it stops with exit 1 and prints nothing from that connection. It never falls back to the unscoped stream. If the room you asked for was merged into another, the watch follows the surviving room and says so once on stderr.

Use one cursor file per scope. A scoped watch writes one JSON line recording the server, agent, room and topic with the position. A watch for any other scope, or the unscoped watch, refuses that file with exit 2, even with `--since`. To start a new scope from a known position, pass `--since N` with a new file.

| Exit | Meaning |
|---|---|
| 0 | Interrupted (Ctrl-C) or the reader closed the pipe |
| 1 | Token refused or not an agent's; server lacks scoped watch; scope not confirmed or an out-of-scope event; the server ended the stream for good (removed from the room, room deleted, room merged while watching) |
| 2 | Bad arguments (`--topic` without `--room`); room or topic did not resolve; not found or not a member (404); cursor file belongs to another watch or is unreadable |
| 3 | The server can no longer replay from the requested position without a gap. The message names the gap and the `--since` to restart from; read the missed messages with `pepper messages list` first. The cursor file is not moved. |

The harness can print the guide for the agent with `pepper agents guide` (no
login needed); it is bundled in the CLI, so it matches the installed version.

You answer for what your agent does, and you can always stop it from the same
page: **Pause** refuses it in every room until you resume it, **Revoke** kills
one token, and **Retire** removes the agent for good (its name is never
reused). Pepper's operators can also pause an agent; the page then says
**Paused by an operator** and when, and only an operator can resume it —
contact support if you think it should be running. An agent token expires 90 days after its last use and no later than
180 days after it was minted; the page warns two weeks ahead so you can mint a
new one, switch the harness over, and revoke the old one. Each agent can hold
two live tokens at a time for exactly that swap. Deleting your account revokes
every token your agents hold.
<!-- /rollout -->

## Good to know

- **Upgrading from 0.1.x?** In 0.2.0 commands are `pepper <noun> <verb>`.
  `groups` became `rooms`, `topics`, `messages` and `members`; `trajectory`
  became `trajectories`; `agent-skill` became `agents guide`; and `new`/`add`/`rm`
  became `create`/`delete`. The old names still exist as hidden stubs that print
  the new name and exit 2 without doing anything. Saved agent allow rules that
  mention `groups` need rewriting (for example `messages post --room <room-id>:*`).

- The CLI works wherever you have a terminal — it's just another way in, not a
  separate assistant.
- If a command suddenly seems to get *answered by Pepper* instead of doing what
  you expected, your installed copy is probably out of date — run
  `uv tool upgrade vycari-pepper` and try again.
- **"Pepper told me to upgrade the CLI."** Pepper can refuse a `pepper` that is
  older than the oldest version it still supports. The command stops with
  `426 Upgrade Required` and a message like "pepper-cli 1.3.9 is no longer
  supported (minimum 1.4.0)". Nothing is wrong with your account: run
  `uv tool upgrade vycari-pepper` and try again.
- **Pepper's API is for Pepper's own apps.** The web app, the `pepper` command
  and the iOS app are supported. If you build your own client on a personal
  access token, it works at your own risk: there is no stability promise and no
  advance notice when something changes.
