"""`scripts/docs_push.py`, which places `help/` into a pepper checkout for `docs-push.yml`.

Pins the three things the workflow relies on: only the files pepper's manifest gives this repo
are placed (anything else in `help/` is held), an owned file missing here fails instead of being
deleted there, and `changed` is false when pepper already has the same bytes.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("docs_push", ROOT / "scripts" / "docs_push.py")
assert _SPEC
assert _SPEC.loader
docs_push = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(docs_push)

REPO = "Vycari/pepper-cli"


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
