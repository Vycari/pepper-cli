"""Place this repo's help topics into a checkout of Vycari/pepper (`docs-push.yml`).

`help/<slug>.md` here is the user-facing help for the command-line app. Pepper's guide
(`src/pepper/features/help/skills/pepper-guide/reference/`) is where the agent, the in-app Help
and the public docs read it, so `docs-push.yml` copies it there and opens one sync PR
(`planning/help-docs-coverage.md` section 1 in Vycari/pepper).

Which files go is the server's decision, not this repo's: `.github/docs-owners.json` in the
pepper checkout maps each owned filename to the repo that owns it. A file in `help/` the
manifest does not give to this repo is held (a topic written ahead of its launch), and an owned
file missing from `help/` is an error rather than a deletion, because removing a topic is a change
to pepper's topic list too.

Standard library only, run as `python3 -I`; it reads the pepper checkout as data and executes
nothing from it.

    python3 -I scripts/docs_push.py place --repo Vycari/pepper-cli --help-dir help \
        --server server --paths-out paths.txt

prints `changed=true` or `changed=false` (for `$GITHUB_OUTPUT`) and writes the placed paths,
relative to the server checkout, one per line.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

MANIFEST = Path(".github/docs-owners.json")
REFERENCE = Path("src/pepper/features/help/skills/pepper-guide/reference")
_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\.md$")


class PushError(Exception):
    """The manifest or the help directory is not in a state that can be pushed."""


def owned_files(manifest: Path, repo: str) -> list[str]:
    """The filenames the server's manifest gives to `repo`, sorted."""
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PushError(f"{manifest} is missing from the server checkout") from None
    owners = data.get("owners") if isinstance(data, dict) else None
    if not isinstance(owners, dict):
        raise PushError(f"{manifest} has no `owners` object")
    names = []
    for name, owner in owners.items():
        if not isinstance(name, str) or not _NAME.match(name) or not isinstance(owner, str):
            raise PushError(f"{manifest}: bad entry {name!r}: {owner!r}")
        if owner.lower() == repo.lower():
            names.append(name)
    return sorted(names)


def place(*, repo: str, help_dir: Path, server: Path) -> tuple[bool, list[str], list[str]]:
    """Copy each owned file into the server checkout.

    Returns whether any placed file differs from what the checkout had, the placed paths
    (relative to `server`), and the help files held back because the manifest does not list
    them for `repo`.
    """
    owned = owned_files(server / MANIFEST, repo)
    missing = [name for name in owned if not (help_dir / name).is_file()]
    if missing:
        raise PushError(
            f"the server's manifest gives {repo} {', '.join(missing)}, but {help_dir}/ does not "
            "have them; a topic is removed from the server's manifest first"
        )
    changed = False
    placed = []
    for name in owned:
        body = (help_dir / name).read_bytes()
        dest = server / REFERENCE / name
        if not dest.is_file() or dest.read_bytes() != body:
            changed = True
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(body)
        placed.append(str(REFERENCE / name))
    held = sorted(p.name for p in help_dir.glob("*.md") if p.name not in owned)
    return changed, placed, held


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("place")
    cmd.add_argument("--repo", required=True)
    cmd.add_argument("--help-dir", type=Path, required=True)
    cmd.add_argument("--server", type=Path, required=True)
    cmd.add_argument("--paths-out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        changed, placed, held = place(repo=args.repo, help_dir=args.help_dir, server=args.server)
    except PushError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    for name in held:
        print(
            f"::notice::{name} is not in the server's manifest for {args.repo}; held.",
            file=sys.stderr,
        )
    args.paths_out.write_text("".join(f"{p}\n" for p in placed), encoding="utf-8")
    print(f"changed={'true' if changed else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
