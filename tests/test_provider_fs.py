"""The escape matrix, and the caps, for the local filesystem toolset.

The client-tools design puts **all** filesystem enforcement on this
side of the wire — the server sends a tool name and arguments and cannot
check a path on a disk it cannot see. So this file is where the security
argument of the whole feature is either true or not, and each test here is
one row of the escape matrix: ``..``, a symlink pointing out of the root,
an absolute path, and a root-prefix confusion like ``/notes`` versus
``/notes-private``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from pepper_cli.provider import fs as provider_fs
from pepper_cli.provider.fs import FilesystemProvider, PathRefused


@pytest.fixture
def notes(tmp_path: Path) -> Path:
    root = tmp_path / "notes"
    (root / "projects").mkdir(parents=True)
    (root / "plan.md").write_text("Ship the thing.\nCall it phase one.\n")
    (root / "projects" / "2026.md").write_text("A plan for 2026.\n")
    return root


# ---------------------------------------------------------------------------
# Containment — the four escapes


def test_dotdot_cannot_climb_out_of_the_root(notes: Path, tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("not yours")
    provider = FilesystemProvider([notes])
    text, is_error = provider.call("fs_read", {"path": "../secret.txt"})
    assert is_error
    assert "not yours" not in text


def test_a_symlink_pointing_out_of_the_root_is_refused(notes: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("not yours")
    (notes / "link.txt").symlink_to(outside)
    provider = FilesystemProvider([notes])
    text, is_error = provider.call("fs_read", {"path": "link.txt"})
    assert is_error, text
    assert "not yours" not in text


def test_an_absolute_path_is_refused_rather_than_joined(notes: Path, tmp_path: Path) -> None:
    """``Path(root) / "/etc/passwd"`` is ``/etc/passwd`` — the join silently
    discards the root, which is why absolute paths are refused before any
    resolution happens rather than sanitized after it."""
    outside = tmp_path / "secret.txt"
    outside.write_text("not yours")
    provider = FilesystemProvider([notes])
    text, is_error = provider.call("fs_read", {"path": str(outside)})
    assert is_error
    assert "absolute" in text.lower()


def test_a_sibling_whose_name_starts_with_the_root_is_not_inside_it(tmp_path: Path) -> None:
    """``"/notes-private".startswith("/notes")`` is True.

    The containment check compares resolved *path components*, so this
    passes; a prefix check would let the whole sibling tree through.
    """
    root = tmp_path / "notes"
    root.mkdir()
    sibling = tmp_path / "notes-private"
    sibling.mkdir()
    (sibling / "diary.md").write_text("not yours")
    provider = FilesystemProvider([root])
    text, is_error = provider.call("fs_read", {"path": "../notes-private/diary.md"})
    assert is_error
    assert "not yours" not in text
    assert not provider_fs._is_within(sibling.resolve(), root.resolve())


def test_a_missing_file_and_an_out_of_root_file_refuse_identically(
    notes: Path, tmp_path: Path
) -> None:
    """Otherwise ``fs_read`` answers "does this exist?" for the whole disk."""
    (tmp_path / "real-but-outside.txt").write_text("x")
    provider = FilesystemProvider([notes])
    missing, _ = provider.call("fs_read", {"path": "nope.md"})
    outside, _ = provider.call("fs_read", {"path": "../real-but-outside.txt"})
    assert missing == outside


def test_grep_does_not_follow_a_symlink_out_of_the_root(notes: Path, tmp_path: Path) -> None:
    """The escape ``fs_read`` closes and ``fs_grep`` nearly did not.

    ``os.walk(followlinks=False)`` refuses to *descend* a symlinked
    directory and says nothing about a symlinked file, so a walked candidate
    reaches the open with no realpath check of its own unless one is applied
    to it — which is how a search would return content from outside the
    grant that a direct read of the same link is refused.
    """
    outside = tmp_path / "outside.md"
    outside.write_text("needle in a file you never shared\n")
    (notes / "link.md").symlink_to(outside)
    text, _ = FilesystemProvider([notes]).call("fs_grep", {"query": "needle"})
    assert "No matches" in text
    assert "never shared" not in text


def test_grep_does_not_descend_a_symlinked_directory_out_of_the_root(
    notes: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("needle in a folder you never shared\n")
    (notes / "linked").symlink_to(outside, target_is_directory=True)
    text, _ = FilesystemProvider([notes]).call("fs_grep", {"query": "needle"})
    assert "No matches" in text


def test_list_does_not_publish_a_symlink_pointing_out_of_the_root(
    notes: Path, tmp_path: Path
) -> None:
    """``stat()`` follows the link, so listing one would leak an external
    file's existence and its size."""
    outside = tmp_path / "outside.md"
    outside.write_text("x" * 4096)
    (notes / "link.md").symlink_to(outside)
    text, _ = FilesystemProvider([notes]).call("fs_list", {})
    assert "link.md" not in text


def test_read_does_not_load_more_than_the_cap(notes: Path, monkeypatch) -> None:
    """The cap has to bound I/O, not just the returned string: slicing
    ``read_bytes()`` loads a multi-gigabyte file in full before trimming it,
    and ``fs_grep`` would do it once per candidate."""
    monkeypatch.setattr(provider_fs, "MAX_READ_BYTES", 8)
    read_sizes: list[int | None] = []
    real_open = Path.open

    def spy(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        original = handle.read

        def read(size=-1):
            read_sizes.append(size)
            return original(size)

        handle.read = read
        return handle

    monkeypatch.setattr(Path, "open", spy)
    (notes / "long.md").write_text("y" * 100_000)
    text, is_error = FilesystemProvider([notes]).call("fs_read", {"path": "long.md"})
    assert not is_error
    assert "truncated" in text
    # The binary sniff reads its own fixed window; the content read must ask
    # for the cap plus the one byte that distinguishes "ended" from "cut".
    assert provider_fs.MAX_READ_BYTES + 1 in read_sizes
    assert all(size is not None and size > 0 for size in read_sizes)


# ---------------------------------------------------------------------------
# Special files and binaries


def test_a_directory_is_not_a_readable_file(notes: Path) -> None:
    text, is_error = FilesystemProvider([notes]).call("fs_read", {"path": "projects"})
    assert is_error
    assert "file" in text.lower()


# POSIX-only on purpose (#46): ``os.mkfifo`` has no Windows equivalent, and no
# other non-regular file can be created from a test there without elevated
# rights or reserved device names. The ``is_file()`` refusal this exercises is
# platform-neutral and runs on Windows via the directory case above.
@pytest.mark.skipif(sys.platform == "win32", reason="intentionally POSIX-only: no Windows FIFO")
def test_a_fifo_is_refused_rather_than_opened(notes: Path) -> None:
    import os

    os.mkfifo(notes / "pipe")
    text, is_error = FilesystemProvider([notes]).call("fs_read", {"path": "pipe"})
    assert is_error
    assert "readable file" in text


def test_a_binary_file_is_refused_by_content_not_only_by_suffix(notes: Path) -> None:
    (notes / "blob.md").write_bytes(b"text\x00more")
    text, is_error = FilesystemProvider([notes]).call("fs_read", {"path": "blob.md"})
    assert is_error
    assert "binary" in text


def test_a_binary_suffix_is_refused_without_reading_it(notes: Path) -> None:
    (notes / "photo.png").write_bytes(b"not actually a png")
    _, is_error = FilesystemProvider([notes]).call("fs_read", {"path": "photo.png"})
    assert is_error


# ---------------------------------------------------------------------------
# Caps


def test_read_truncates_at_the_cap_and_says_so(notes: Path, monkeypatch) -> None:
    monkeypatch.setattr(provider_fs, "MAX_READ_BYTES", 16)
    (notes / "long.md").write_text("x" * 200)
    text, is_error = FilesystemProvider([notes]).call("fs_read", {"path": "long.md"})
    assert not is_error
    assert "truncated" in text
    assert text.count("x") == 16


def test_list_truncates_at_the_cap_and_says_so(notes: Path, monkeypatch) -> None:
    monkeypatch.setattr(provider_fs, "MAX_LIST_ENTRIES", 3)
    for i in range(10):
        (notes / f"f{i}.md").write_text("x")
    text, is_error = FilesystemProvider([notes]).call("fs_list", {})
    assert not is_error
    assert "truncated" in text


def test_grep_stops_at_the_match_cap(notes: Path, monkeypatch) -> None:
    monkeypatch.setattr(provider_fs, "MAX_GREP_MATCHES", 2)
    (notes / "many.md").write_text("needle\n" * 10)
    text, is_error = FilesystemProvider([notes]).call("fs_grep", {"query": "needle"})
    assert not is_error
    assert text.count("needle") <= 4  # two hit lines, each echoing the term
    assert "stopped at" in text


def test_grep_snippets_are_bounded(notes: Path, monkeypatch) -> None:
    monkeypatch.setattr(provider_fs, "GREP_SNIPPET_CHARS", 10)
    (notes / "wide.md").write_text("needle" + "z" * 5_000)
    text, _ = FilesystemProvider([notes]).call("fs_grep", {"query": "needle"})
    assert max(len(line) for line in text.splitlines()) < 80


def test_grep_skips_ignored_directories(notes: Path) -> None:
    (notes / ".git").mkdir()
    (notes / ".git" / "config.md").write_text("needle in the git dir")
    text, _ = FilesystemProvider([notes]).call("fs_grep", {"query": "needle"})
    assert "No matches" in text


def test_grep_skips_binaries(notes: Path) -> None:
    (notes / "blob.md").write_bytes(b"needle\x00")
    text, _ = FilesystemProvider([notes]).call("fs_grep", {"query": "needle"})
    assert "No matches" in text


# ---------------------------------------------------------------------------
# Happy paths and shape


def test_list_read_and_grep_answer(notes: Path) -> None:
    provider = FilesystemProvider([notes])
    listing, err = provider.call("fs_list", {})
    assert not err
    assert "plan.md" in listing
    assert "projects/" in listing

    body, err = provider.call("fs_read", {"path": "plan.md"})
    assert not err
    assert "Ship the thing." in body

    hits, err = provider.call("fs_grep", {"query": "PHASE"})
    assert not err
    assert "plan.md:2" in hits


def test_a_result_never_ships_an_absolute_path(notes: Path) -> None:
    """Labels are rooted at the folder's own name, so a result does not hand
    the server the user's home directory."""
    provider = FilesystemProvider([notes])
    for name, args in (
        ("fs_list", {}),
        ("fs_read", {"path": "plan.md"}),
        ("fs_grep", {"query": "Ship"}),
    ):
        text, _ = provider.call(name, args)
        assert str(notes.parent) not in text


def test_manifest_declares_three_read_only_tools(notes: Path) -> None:
    manifest = FilesystemProvider([notes]).manifest()
    assert [t["name"] for t in manifest] == ["fs_list", "fs_read", "fs_grep"]
    assert all(t["annotations"]["readOnlyHint"] is True for t in manifest)
    # Invariant 1: no tool takes a root, base path or scope argument. The
    # moment one does, the grant moves to the server.
    for tool in manifest:
        assert set(tool["inputSchema"].get("properties", {})) <= {"path", "query"}


def test_an_unknown_tool_name_is_an_error_not_an_exception(notes: Path) -> None:
    text, is_error = FilesystemProvider([notes]).call("fs_delete", {})
    assert is_error
    assert "No such tool" in text


def test_a_non_directory_root_is_refused_at_construction(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("x")
    with pytest.raises(PathRefused):
        FilesystemProvider([tmp_path / "file.txt"])


# ---------------------------------------------------------------------------
# Several roots


def test_several_roots_list_as_folders_and_resolve_by_their_own_name(tmp_path: Path) -> None:
    a = tmp_path / "notes"
    b = tmp_path / "work"
    a.mkdir()
    b.mkdir()
    (a / "one.md").write_text("alpha")
    (b / "two.md").write_text("beta")
    provider = FilesystemProvider([a, b])

    top, _ = provider.call("fs_list", {})
    assert "notes/" in top
    assert "work/" in top

    # A path the model read off a result ("work/two.md") resolves in the
    # root it was labelled with, not as ``notes/work/two.md``.
    body, err = provider.call("fs_read", {"path": "work/two.md"})
    assert not err
    assert "beta" in body


def test_grep_with_no_path_searches_every_root(tmp_path: Path) -> None:
    a = tmp_path / "notes"
    b = tmp_path / "work"
    a.mkdir()
    b.mkdir()
    (b / "two.md").write_text("needle")
    hits, err = FilesystemProvider([a, b]).call("fs_grep", {"query": "needle"})
    assert not err
    assert "work/two.md" in hits
