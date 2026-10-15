# Testing guidelines

- Run `uv run pytest`. The suite must pass with no network: HTTP is mocked with `pytest-httpx`.
- Coverage is a ratchet. `coverage.floor` in `.claude/maintainerd.json` may be raised, never
  lowered to make a change pass. CI fails when measured line coverage is below it.
- A behaviour change comes with a test that fails without it. Prefer asserting on rendered output
  and request shape over asserting on internals.
- Tests use obviously fake credentials and identities (for example `pat_supersecret`,
  `alice@example.com`). Never put a real token, address, or hostname in a fixture.
- Tests never read or write the real credentials file; use the temporary-directory fixtures in
  `tests/conftest.py`.
- Tests must pass on Windows: isolate both `$HOME` and `$USERPROFILE` in fixtures,
  skip POSIX-only tests on Windows, and avoid assuming interactive TTY buffers exist.
