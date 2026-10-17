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
  preferences commands. They fetch and render what the server returns; the CLI holds no business
  logic (ranking, filtering, choosing).
- `credentials.py`, `secret.py` — the credentials file and the `SecretStr` wrapper for tokens.
- `provider/fs.py` — filesystem provider helpers.
- `agent_skill.py`, `skill_doc.py`, `data/agent-skill/SKILL.md` — the bundled agent skill.
- `proctitle.py`, `window_title.py` — process and terminal title helpers.
- `tests/` — pytest suite; HTTP is mocked, so it needs no network or server.

## Commands

```bash
uv sync --extra dev
uv run ruff format --check
uv run ruff check
uv run ty check src/pepper_cli/
uv run pytest
uv run pytest --cov --cov-report=json:coverage-summary.json   # what CI runs
```

## Rules that matter here

- The CLI follows the server's `/api/v1` contract. Wire models carry `# MIRROR:` comments
  naming the server model they follow; keep both in step. `/api/v1` is additive-only, so the
  client must tolerate unknown fields and unknown stream frame types.
- `contracts/api-v1.openapi.json` is a vendored copy of the server's schema; never hand-edit it
  (a weekly workflow refreshes it). `tests/test_api_contract.py` checks every `MIRROR`ed dataclass
  field against it, so a new field on a mirrored dataclass must already be in the snapshot: the
  server lands first, then the snapshot refresh, then the CLI.
- Tokens never get logged, printed, or placed in error messages; keep them in `SecretStr`.
- Do not add lint ignores to pass CI, and do not lower `coverage.floor` in
  `.claude/maintainerd.json`.
- The bundled `SKILL.md` documents the commands; change it together with the CLI.
- Guidelines: `.claude/guidelines/`. Vycari-wide house rules (review, PR template, escalation)
  load from the `vycari-ops` plugin enabled in `.claude/settings.json`.
