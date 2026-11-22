"""`scripts/docs_push.py`, which places `help/` into a pepper checkout for `docs-push.yml`.

Pins the three things the workflow relies on: only the files pepper's manifest gives this repo
are placed (anything else in `help/` is held), an owned file missing here fails instead of being
deleted there, and `changed` is false when pepper already has the same bytes.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("docs_push", ROOT / "scripts" / "docs_push.py")
assert _SPEC
assert _SPEC.loader
docs_push = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(docs_push)

REPO = "Vycari/pepper-cli"


def _bash_4_or_newer() -> bool:
    """The workflow shell runs on Ubuntu's bash and uses bash 4 syntax (`${VAR,,}`).

    macOS ships bash 3.2, so a Mac with no newer bash on PATH skips these tests rather
    than failing them.
    """
    bash = shutil.which("bash")
    if sys.platform == "win32" or bash is None:
        return False
    major = subprocess.run(
        [bash, "-c", "echo ${BASH_VERSINFO[0]}"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return major.isdigit() and int(major) >= 4


_NEEDS_BASH_4 = pytest.mark.skipif(
    not _bash_4_or_newer(),
    reason="runs the workflow shell, which needs bash 4 or newer, with POSIX stub tools",
)


def _server(tmp_path: Path, owners: dict[str, str]) -> Path:
    server = tmp_path / "server"
    (server / ".github").mkdir(parents=True)
    (server / ".github" / "docs-owners.json").write_text(json.dumps({"owners": owners}))
    (server / docs_push.REFERENCE).mkdir(parents=True)
    return server


def _help(tmp_path: Path, files: dict[str, str]) -> Path:
    help_dir = tmp_path / "help"
    help_dir.mkdir()
    for name, body in files.items():
        (help_dir / name).write_text(body)
    return help_dir


def test_places_only_owned_files_and_holds_the_rest(tmp_path: Path) -> None:
    server = _server(tmp_path, {"cli.md": REPO, "iphone.md": "Vycari/pepper-apple"})
    help_dir = _help(tmp_path, {"cli.md": "# CLI\n", "later.md": "# Later\n"})
    changed, placed, held = docs_push.place(repo=REPO, help_dir=help_dir, server=server)
    assert changed
    assert placed == [str(docs_push.REFERENCE / "cli.md")]
    assert held == ["later.md"]
    assert (server / docs_push.REFERENCE / "cli.md").read_text() == "# CLI\n"
    assert not (server / docs_push.REFERENCE / "later.md").exists()


def test_unchanged_files_report_no_change(tmp_path: Path) -> None:
    server = _server(tmp_path, {"cli.md": REPO})
    (server / docs_push.REFERENCE / "cli.md").write_text("same\n")
    help_dir = _help(tmp_path, {"cli.md": "same\n"})
    changed, placed, _ = docs_push.place(repo=REPO, help_dir=help_dir, server=server)
    assert not changed
    assert placed == [str(docs_push.REFERENCE / "cli.md")]


def test_an_owned_file_missing_here_fails(tmp_path: Path) -> None:
    server = _server(tmp_path, {"cli.md": REPO, "tui.md": REPO})
    help_dir = _help(tmp_path, {"cli.md": "x\n"})
    with pytest.raises(docs_push.PushError, match=r"tui\.md"):
        docs_push.place(repo=REPO, help_dir=help_dir, server=server)


@pytest.mark.parametrize("name", ["../x.md", "Upper.md", "a/b.md", "x.txt"])
def test_a_bad_manifest_name_fails(tmp_path: Path, name: str) -> None:
    server = _server(tmp_path, {name: REPO})
    with pytest.raises(docs_push.PushError, match="bad entry"):
        docs_push.owned_files(server / docs_push.MANIFEST, REPO)


def test_main_writes_the_paths_and_the_output_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    server = _server(tmp_path, {"cli.md": REPO})
    help_dir = _help(tmp_path, {"cli.md": "# CLI\n"})
    out = tmp_path / "paths.txt"
    argv = ["place", "--repo", REPO, "--help-dir", str(help_dir), "--server", str(server)]
    assert docs_push.main([*argv, "--paths-out", str(out)]) == 0
    assert capsys.readouterr().out == "changed=true\n"
    assert out.read_text() == f"{docs_push.REFERENCE / 'cli.md'}\n"


def test_every_help_file_is_a_flat_markdown_topic() -> None:
    """What docs_push.py will accept: one level, lowercase slug, `.md`."""
    names = [p.name for p in (ROOT / "help").iterdir() if not p.name.startswith(".")]
    assert names
    for name in names:
        assert docs_push._NAME.match(name), name


def test_docs_push_workflow_labels_and_arms_auto_merge() -> None:
    # The sync PR skips review (prose, reviewed here) and merges through pepper's queue once its
    # required checks pass. Pinned to the pushed head; DOCS_SYNC_AUTO_MERGE=false switches it off.
    import yaml

    workflow = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / ".github/workflows/docs-push.yml").read_text()
    )
    step = next(
        s
        for s in workflow["jobs"]["push"]["steps"]
        if s.get("name") == "Open, update or close the sync PR"
    )
    script = step["run"]
    assert "--draft" in script
    assert "--label greptile:skip" in script
    assert 'gh pr ready "$pr"' in script
    assert '--auto --squash --match-head-commit "$(git rev-parse HEAD)"' in script
    # The kill switch disarms a PR an earlier run armed, before reporting the hand merge.
    final = script.index("if auto_merge_off; then")
    assert script.index('disarm_auto_merge "$pr"', final) < script.index("elif", final)
    # A disarm is read back, and one that did not take fails the run.
    disarm = script.index("disarm_auto_merge() {")
    body = script[disarm : script.index("\n}", disarm)]
    assert body.index("--disable-auto") < body.index("autoMergeRequest") < body.index("exit 1")
    # With the switch off, the open PR is disarmed before anything is pushed.
    early = script.index('if [ -n "$existing" ] && auto_merge_off; then')
    assert script.index('disarm_auto_merge "$existing"', early) < script.index("git push", early)
    # An existing draft is a maintainer's hold: the run exits before it can ready or arm it.
    # Only a draft someone converted is held; the job's own orphaned draft is finished.
    draft = script.index("--json isDraft")
    held = script.index("was converted to draft, so it is held", draft)
    assert draft < script.index('"convert_to_draft"', draft) < held
    assert script.index("exit 0", held) < script.index('gh pr ready "$pr"', held)
    # Labeled and ready before it is armed.
    assert script.index('gh pr ready "$pr"') < script.index("--auto --squash")
    assert step["env"]["AUTO_MERGE"] == "${{ vars.DOCS_SYNC_AUTO_MERGE }}"


def _run_pr_tail(
    tmp_path: Path, *, existing: str, converted: bool, auto_merge: str = "", draft: bool = True
) -> str:
    """Run the step's real PR handling (from the edit-or-create branch on) against a stub `gh`.

    Returns the `gh` calls it made, one per line. The PR is a draft; `converted` decides
    whether its timeline has a `convert_to_draft` event.
    """
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/docs-push.yml").read_text())
    step = next(
        s
        for s in workflow["jobs"]["push"]["steps"]
        if s.get("name") == "Open, update or close the sync PR"
    )
    script = step["run"]
    helpers = script[
        script.index("auto_merge_off() {") : script.index(
            "\n}", script.index("disarm_auto_merge() {")
        )
        + 2
    ]
    tail = script[script.index('if [ -n "$existing" ]; then\n  gh pr edit') :]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gh.log"
    timeline = "convert_to_draft" if converted else ""
    is_draft = "true" if draft else "false"
    (bin_dir / "gh").write_text(
        f'#!/bin/sh\necho "$*" >> "{log}"\n'
        'case "$1 $2" in\n'
        f'  "pr view") case "$*" in *isDraft*) echo {is_draft} ;; *) echo false ;; esac ;;\n'
        f'  "api "*) [ -n "{timeline}" ] && echo {timeline} ;;\n'
        '  "pr create") echo 77 ;;\n'
        "esac\nexit 0\n"
    )
    (bin_dir / "git").write_text("#!/bin/sh\necho deadbeef\n")
    for tool in ("gh", "git"):
        (bin_dir / tool).chmod(0o755)
    subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{helpers}\n{tail}"],
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "existing": existing,
            "AUTO_MERGE": auto_merge,
            "SYNC_BRANCH": "docs-sync/x",
            "title": "t",
            "body": "b",
        },
        check=True,
        capture_output=True,
        text=True,
    )
    return log.read_text()


@_NEEDS_BASH_4
def test_a_draft_someone_converted_is_neither_readied_nor_armed(tmp_path: Path) -> None:
    calls = _run_pr_tail(tmp_path, existing="42", converted=True)
    assert "pr ready" not in calls
    assert "pr merge" not in calls


@_NEEDS_BASH_4
def test_the_jobs_own_orphaned_draft_is_readied_and_armed(tmp_path: Path) -> None:
    calls = _run_pr_tail(tmp_path, existing="42", converted=False).splitlines()
    ready = calls.index("pr ready 42")
    assert calls.index("pr merge 42 --auto --squash --match-head-commit deadbeef") > ready


@_NEEDS_BASH_4
@pytest.mark.parametrize(
    ("value", "off"),
    [
        ("", False),
        ("true", False),
        ("1", False),
        ("false", True),
        (" OFF ", True),
        ("No", True),
        ("0", True),
    ],
)
def test_the_kill_switch_reads_its_value(tmp_path: Path, value: str, *, off: bool) -> None:
    calls = _run_pr_tail(tmp_path, existing="42", converted=False, auto_merge=value, draft=False)
    armed = "pr merge 42 --auto" in calls
    assert armed is not off
    assert ("pr merge 42 --disable-auto" in calls) is off
