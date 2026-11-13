<!--
Two audiences, one template, and this file is the canonical copy of both: a repo's
`.github/PULL_REQUEST_TEMPLATE.md` is this file with its checklist block filled in. Change the
shape here, in `plugins/vycari-ops/references/pr-template.md`, not in a repo.

Everything above `## AI reviewer` is for a human who is busy and reading across many workstreams.
Everything below it is for the Greptile reviewer and for future Claude sessions reading this PR as
history. The section is folded in a `<details>` block, the same split Vycari Review uses on its
inline threads: GitHub shows the heading and one closed line, while the raw body (what the API
returns to an agent) is plain Markdown. Keep the heading outside the fold, since tools find the
section by that line, and keep `</details>` as the last line you write: Greptile appends its summary
after it, and an unclosed fold would hide that summary too. Write each for its own reader: don't pad the human half with detail, and don't thin the AI
half because a human might skim it. State gaps and trade-offs plainly — a description that admits
its own limitations is the one a reviewer trusts. No marketing language, no emoji.

Closing keywords are a live footgun. GitHub matches `close`/`fix`/`resolve` next to an issue number
ANYWHERE in the body and ignores negation — the sentence "does not close #123" closes #123 on merge
(that has wrongly closed issues before). For a partial fix write `Refs #123`, and keep the
keyword away from the number: "this does not finish #123".
-->

## Human overview

<!-- 2–4 sentences, glanceable: what changed and why, for a reviewer working across many streams.
Link the issue (`Fixes #N` / `Refs #N`). No implementation detail — that is the AI section's job. -->

### Human required (optional)

<!-- Only when a person must do or decide something: a merge-order constraint, a settings / secrets
/ prod / branch-protection step, a product decision. One line each, imperative. Drop the whole
section when it is empty. -->

- [ ]

## AI reviewer

<details><summary>Details for agents: what the diff does, invariants, verification, checklist</summary>

<!-- For Greptile and for future Claude sessions; assume no human reads this. As dense as is useful.
Cover what applies and drop what doesn't:

- what the diff does, by subsystem
- invariants touched, and why they still hold
- alternatives rejected, and why
- failure posture: what breaks if this is wrong, and what it takes with it
- migration notes: ordering, backfill, rollback
- where to look hardest — the subtle bug this nearly shipped, the "simplification" a later PR would
  apply without understanding
- verification: exact commands and their actual results, not what should pass; say what was skipped
  and why
- deferred work, each with an issue link (a follow-up that lives only in a PR dies with the PR)
-->

### Checklist (repo-specific)

<!-- REPO-SPECIFIC SLOT — the only part of this template a repo owns. Fill it with that repo's own
gates (its format / lint / typecheck / test commands, migration and coverage rules, doc surfaces
that must move with the code). Tick only what actually ran; mark an inapplicable box `[ ]` with a
one-line reason. An N/A with a reason is fine, a silently ticked box is not. -->

- [ ] `uv run ruff format --check` and `uv run ruff check` clean (no new lint ignores)
- [ ] `uv run ty check src/pepper_cli/` clean
- [ ] `uv run pytest` passes; coverage floor in `.claude/maintainerd.json` not lowered
- [ ] Server-contract changes: `/api/v1` usage stays additive-only, `# MIRROR:` comments updated, unknown fields and frames still tolerated
- [ ] No token or credential is logged, printed, or put in a fixture
- [ ] README and the bundled `SKILL.md` updated if commands or output changed
- [ ] Help: `help/cli.md` updated for a new or changed command, or not user-facing because ___

</details>
