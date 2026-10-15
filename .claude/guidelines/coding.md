# Coding guidelines

- Python 3.13+. Format with `ruff format`, lint with `ruff check` (the rule set is `ALL` minus the
  documented ignores in `pyproject.toml`), type-check with `ty check src/pepper_cli/`.
- Do not add new lint ignores to make a change pass. Fix the code, or justify a narrowly scoped
  ignore in the PR description.
- Bearer tokens and PATs are wrapped in `SecretStr` (`secret.py`) at the boundary and are never
  logged, printed, or put in an exception message.
- Wire models mirror the server's `/api/v1` response shapes. Each mirrored model carries a
  `# MIRROR:` comment naming the server model it follows. Keep models tolerant: ignore unknown
  fields and unknown stream frame types rather than failing.
- Formatted output goes through `render.py` and its rich consoles. Bare `print` is allowed in
  `cli.py` only, for plain lines meant for pipes (a created id, `whoami` fields) and argv errors
  on stderr; the `T201` ignore in `pyproject.toml` is scoped to that one file, so a `print`
  anywhere else fails lint.
- Keep the CLI dependency-light. A new runtime dependency needs a reason in the PR.
- Cross-platform support: keep the CLI working on Windows alongside Linux/macOS.
  Guard Unix-only APIs (like `time.tzset` or `os.umask`) with platform checks,
  use `pathlib.Path`, and handle Windows file locking (`PermissionError`).
