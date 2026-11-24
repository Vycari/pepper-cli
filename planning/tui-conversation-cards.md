# Terminal UI: replies and threads as conversation cards

**Status:** Draft. One question for the maintainer (§6).
**Author:** drafted with Claude Code for the maintainer
**Last updated:** 2026-11-24
**Tracked by:** #365
**Related:** `planning/terminal-ui.md` (the TUI this extends, §7 rendering parity). Vycari/pepper
`planning/conversation-cards.md` (the card model on the web), Vycari/pepper-apple `ConversationCards`
and `FeedCards` (the same model on iOS and the Mac).

## 1. Problem

In a room, the web and the Apple apps draw every top-level message as a **card** with its replies
nested under it. The TUI draws a topic as one flat list: a reply sits wherever it was posted, with
a one-line `┃ Author: excerpt` quote above it. In a busy topic the replies to one message are
scattered across the screen, and there is no way to read one conversation on its own.

The server already sends everything needed: every row carries `parent_message_id`, `title`,
`title_source` and `links`, and `GET …/topics/{tid}/messages?conversation=<root id>` reads one
conversation (its root, then its replies, paged with `before`). `r` already posts a reply with
`parent_message_id`, so the threads exist on the server today; only the TUI's drawing is flat.

## 2. The model, as web and iOS have it

- **Which rows are cards.** A *thread card* is a top-level row (no `parent_message_id`) of one of
  three kinds: a feed item (its first card is `feed_item`), a note comment (an `anchor` with text),
  or a conversation: any other top-level row that is not an activity line, a bridged mirror, a
  broken row or a blocked author's collapsed stub. A topic with `dedicated_conversation` (a bridged
  thread) takes no conversation cards; its rows stay plain, as today.
- **Nesting.** A reply nests under the card its chain of parents reaches, a reply to a reply
  included. A reply straight to the card shows no quote; a reply to a reply keeps its quote line.
  A deleted reply is not drawn, but replies under it still belong to the card. A deleted root stays
  a card while it has a live reply.
- **The face.** A conversation's title is `title` from the server, with the rest of the message as
  the body (when `title_source` says the title took the first line, that line is not drawn twice).
  The links are one flat list of chips, each tagged with its type ("Note", "Source", …), four on
  the card and then "+N more".
- **The fold.** A card shows its latest replies; "N earlier" opens the rest.
- **The bump.** A card with replies from other people sits where its latest such reply was posted,
  so it comes to the bottom when someone else answers. Your own reply never moves it. A card you
  are busy with (selected, or the one your reply is going into) holds its place until you leave it.
- **Missing roots.** A reply whose card is older than the loaded page fetches its root (a few hops
  at most) so it can still nest.
- **Full view.** "Open" shows one conversation as a page of its own: the root, then every reply,
  paged back like a topic, with the composer replying into it.

## 3. The terminal version

```
┌ Family › Trip ──────────────────────────────────────────────────┐
│ Sam                                                     9:41 AM │
│ ╭ Cabin for the 14th ─────────────────────────────────────────╮ │
│ │ Booked it, confirmation attached.                           │ │
│ │ 📎 cabin-confirmation.pdf                                   │ │
│ │ Linked: Note · Trip plan   Source · airbnb.com/rooms/…  +1  │ │
│ │ 👍 2                                                        │ │
│ ├─ 4 earlier replies ─────────────────────────────────────────┤ │
│ │ Ada                                                 9:50 AM │ │
│ │   Can we bring the dog?                                     │ │
│ │ Pepper                                              9:51 AM │ │
│ │   ┃ Ada: Can we bring the dog?                              │ │
│ │   The listing says pets are welcome, with a $40 fee.        │ │
│ ╰─ r reply · Enter open ──────────────────────────────────────╯ │
```

- **Card.** The author line sits above the frame, as on the web. The frame is a rounded box with
  the title in its top edge; inside are the body, previews, app cards, attachments, the "Linked"
  chip line and reactions, then the thread. A feed item keeps its current boxed face as the card's
  head, and a note comment shows its quoted passage (`┃ "…"`) as its title.
- **Thread inside the frame.** Replies use the stream's own row drawing, with the run rule applied
  within the thread (the same author within 15 minutes continues a run).
- **Selection.** `↑`/`↓` walk cards and the replies inside them, as they walk rows today. The fold
  line is a row too: `Enter` on it opens the earlier replies, and again folds them.
- **Reply.** `r` on a card, or anywhere in its thread, replies into that conversation: to the card
  itself, or to the selected reply (which then keeps its quote). The composer shows
  `Replying in "Cabin for the 14th"` where today it shows the quote; `Esc` cancels it, so the next
  send is a new card again. Sending shows the reply dimmed in its thread until the poll returns it.
- **Full view.** `Enter` on a card (or one of its replies) opens the conversation in the thread pane:
  header `Room › Topic › Title`, the root and then every reply as plain rows, older pages loaded on
  scroll-up, the composer replying into it. `Esc` goes back to the topic, to the same card.
- **Links.** Read only here: the chip line, and `o` opens the first one as it opens a link today.
  Adding and removing links stays on the web for now.

## 4. Phases

1. **Cards in the stream** (one PR): the mirror fields (`title`, `title_source`, `links`,
   `is_activity`), nesting, the face, the fold, the bump with its hold, and `r` replying into a
   card. Missing roots are fetched with `at=<id>`, a few hops at most, as iOS does.
2. **Full view** (one PR): `Enter` opens a conversation with `conversation=<root id>`.

Both reuse the existing poll, so a reply arrives in its card on the next poll with no new stream.

## 5. Not now

- Editing a card's links, sharing a conversation to another room.
- Conversation cards in the Pepper DM (it is not a room).
- A `pepper conversations` command outside the TUI.

## 6. Question for the maintainer

**How much of a thread shows inline?** The web and iOS show the latest ten replies before the
fold. A terminal is shorter: ten replies under each card can fill a screen with one conversation.

- **A. Latest 10, as web and iOS.** Exact parity; busy cards are tall.
- **B. Latest 3, then "N earlier".** Recommended. The stream stays scannable, and `Enter` on the
  fold or the full view shows the rest.
- **C. No replies inline, only "N replies · latest from Ada".** The most compact, but it drops the
  "see the conversation in place" that is the point of cards.
