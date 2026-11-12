# pepper-cli

`pepper` is a terminal client for the Pepper `/api/v1` HTTP API: one-shot messages, an
interactive REPL, and read-only trajectory inspection. It authenticates with a personal access
token (PAT). The distribution is `vycari-pepper`, the import package is `pepper_cli`, the
command is `pepper`. User documentation is `README.md`.

## Layout

- `src/pepper_cli/cli.py` — command definitions and dispatch (`__main__.py` is the entry point).
- `api_client.py` — the HTTP/SSE client and the wire models that mirror the server.
- `render.py` — all terminal output. `next_view.py` — the `pepper next` view.
- `feed.py`, `following_*.py`, `brief_view.py`, `artifacts_view.py`, `preferences_view.py`,
  `numbered_view.py`, `*_render.py`, `article_reader.py` — the feed, following, brief, artifacts and
  preferences commands, and `calendar_publish_view.py` (`pepper calendar publish`). They fetch and
  render what the server returns; the CLI holds no business logic (ranking, filtering, choosing).
- `credentials.py`, `secret.py` — the credentials file and the `SecretStr` wrapper for tokens.
- `provider/fs.py` — filesystem provider helpers.
- `agent_skill.py`, `skill_doc.py`, `data/agent-skill/SKILL.md` — the bundled agent skill.
- `proctitle.py`, `window_title.py` — process and terminal title helpers.
- `tests/` — pytest suite; HTTP is mocked, so it needs no network or server.
- `help/cli.md` — the user-facing help for the CLI, in Pepper's guide. `.github/workflows/docs-push.yml`
  (logic in `scripts/docs_push.py`) copies it into Vycari/pepper as a `docs-sync/pepper-cli` PR.

## Commands

```bash
uv sync --extra dev
uv run ruff format --check
uv run ruff check
uv run ty check src/pepper_cli/
uv run pytest -n auto                                            # parallel; plain `uv run pytest` runs one process
uv run pytest -n auto --cov --cov-report=json:coverage-summary.json   # what CI runs
```

## Rules that matter here

- The CLI follows the server's `/api/v1` contract. Wire models carry `# MIRROR:` comments
  naming the server model they follow; keep both in step. `/api/v1` is additive-only, so the
  client must tolerate unknown fields and unknown stream frame types.
- `contracts/api-v1.openapi.json` is a vendored copy of the server's schema; never hand-edit it
  (Vycari/pepper's contract-push workflow opens a `contract-sync` PR when it changes). `tests/test_api_contract.py` checks every `MIRROR`ed dataclass
  field against it, so a new field on a mirrored dataclass must already be in the snapshot: the
  server lands first, then the snapshot refresh, then the CLI.
- A big new feature is gated on the server by a rollout (`planning/feature-rollouts.md` in
  Vycari/pepper), and the CLI never gates on a flag of its own. It reads `GET /api/v1/rollouts`,
  which lists the person-scoped rollout keys enabled for the token's person, hides a gated command
  whose key is absent, and ignores keys it does not know. A 404 from an older server means an
  empty list, so the command stays hidden. Server first: a CLI change that reads the list merges
  only after the endpoint has deployed.
- Tokens never get logged, printed, or placed in error messages; keep them in `SecretStr`.
- Do not add lint ignores to pass CI, and do not lower `coverage.floor` in
  `.claude/maintainerd.json`.
- The bundled `SKILL.md` documents the commands; change it together with the CLI.
- **Help ships with the feature.** `help/cli.md` is what the Pepper agent, the in-app Help and
  www.vycari.ai/pepper/docs tell people about the CLI. A user-visible command change updates it in
  the same PR, in plain language for a person rather than flag-by-flag. Text about a feature behind a
  server rollout goes inside `<!-- rollout: <key> -->` … `<!-- /rollout -->`, so the public docs
  leave it out; an unknown key fails pepper's checks. Edit it here, never in Vycari/pepper: its
  docs-owners check refuses a hand edit there. The push needs the `vycari-contract-sync` App
  secrets on this repo's `docs-push` environment; without them the job is a no-op.
- Guidelines: `.claude/guidelines/`. Vycari-wide house rules (review, PR template, escalation)
  load from the `vycari-ops` plugin enabled in `.claude/settings.json`.

**Review.** This repo is on first-line review (`review.mode: "first-line"` in
`.claude/maintainerd.json`): Vycari Review (VR) reviews first, agents answer VR's findings and then
Greptile's on the capable tier, and agents never request a Greptile review themselves. VR posts
`@greptileai review` once, when it is happy, and a human may too. Greptile at
`review.approvalThreshold` is still the merge gate. A `greptile:skip` PR (prose, design docs,
config) gets neither reviewer and never needs a Greptile review: open it as a draft, label it,
then mark it ready. The design is `docs/design/vr-first-line-review.md` in `Vycari/vycari`.
