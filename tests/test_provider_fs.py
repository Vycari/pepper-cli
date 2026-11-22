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

import os
import sys
from pathlib import Path

import pytest

from pepper_cli.provider import _winfs
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
    real_fdopen = os.fdopen

    def spy(*args, **kwargs):
        handle = real_fdopen(*args, **kwargs)
        original = handle.read

        def read(size=-1):
            read_sizes.append(size)
            return original(size)

        handle.read = read
        return handle

    monkeypatch.setattr(provider_fs.os, "fdopen", spy)
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


# ---------------------------------------------------------------------------
# Arguments are validated, not coerced (#298)


@pytest.mark.parametrize("bad", [None, 1, 1.5, True, ["a.md"], {"p": "a.md"}])
@pytest.mark.parametrize(
    ("tool", "key"), [("fs_list", "path"), ("fs_read", "path"), ("fs_grep", "path")]
)
def test_a_non_string_path_is_an_input_error(notes: Path, tool: str, key: str, bad) -> None:
    args = {"query": "plan", key: bad} if tool == "fs_grep" else {key: bad}
    text, is_error = FilesystemProvider([notes]).call(tool, args)
    assert is_error
    assert "must be a string" in text


@pytest.mark.parametrize("bad", [None, 7, False, ["plan"], {"q": "plan"}])
def test_a_non_string_query_is_an_input_error(notes: Path, bad) -> None:
    text, is_error = FilesystemProvider([notes]).call("fs_grep", {"query": bad})
    assert is_error
    assert "must be a string" in text


def test_missing_required_values_are_input_errors(notes: Path) -> None:
    provider = FilesystemProvider([notes])
    assert provider.call("fs_read", {}) == ("Which file? `path` is required.", True)
    text, is_error = provider.call("fs_grep", {})
    assert is_error
    assert "`query` is required" in text


def test_oversized_inputs_are_refused_before_any_work(notes: Path) -> None:
    provider = FilesystemProvider([notes])
    text, is_error = provider.call("fs_read", {"path": "a" * (provider_fs.MAX_PATH_CHARS + 1)})
    assert is_error
    assert "too long" in text
    text, is_error = provider.call("fs_grep", {"query": "a" * (provider_fs.MAX_QUERY_CHARS + 1)})
    assert is_error
    assert "too long" in text
    _, is_error = provider.call("fs_grep", {"query": "plan", "path": "p" * 5_000})
    assert is_error


def test_a_nul_in_a_path_is_refused_not_raised(notes: Path) -> None:
    text, is_error = FilesystemProvider([notes]).call("fs_read", {"path": "plan.md\x00"})
    assert is_error
    assert "NUL" in text


def test_unknown_extra_arguments_are_ignored(notes: Path) -> None:
    text, is_error = FilesystemProvider([notes]).call("fs_read", {"path": "plan.md", "extra": [1]})
    assert not is_error
    assert "Ship the thing" in text


# ---------------------------------------------------------------------------
# Per-file read cap is reported, not silent (#292)


def test_grep_finds_nothing_past_the_cap_but_does_not_claim_a_clean_miss(
    notes: Path, monkeypatch
) -> None:
    monkeypatch.setattr(provider_fs, "MAX_READ_BYTES", 100)
    (notes / "big.md").write_text("x" * 500 + "\nneedle after the cap\n")
    text, is_error = FilesystemProvider([notes]).call("fs_grep", {"query": "needle"})
    assert not is_error
    assert "needle after" not in text
    assert text != "No matches for 'needle'."
    assert "search incomplete" in text
    assert "notes/big.md" in text


def test_grep_reports_a_cut_file_alongside_real_hits(notes: Path, monkeypatch) -> None:
    monkeypatch.setattr(provider_fs, "MAX_READ_BYTES", 100)
    (notes / "big.md").write_text("x" * 500 + "\nneedle late\n")
    (notes / "small.md").write_text("needle early\n")
    text, _ = FilesystemProvider([notes]).call("fs_grep", {"query": "needle"})
    assert "small.md:1: needle early" in text
    assert "only the first 100 bytes of notes/big.md" in text


def test_grep_of_files_under_the_cap_stays_unqualified(notes: Path) -> None:
    text, _ = FilesystemProvider([notes]).call("fs_grep", {"query": "absent-term"})
    assert text == "No matches for 'absent-term'."


# ---------------------------------------------------------------------------
# The traversal budget (#293, #300)


def test_unusable_entries_do_not_spend_the_file_budget(
    notes: Path, tmp_path: Path, monkeypatch
) -> None:
    """Ignored names, external symlinks and binary suffixes precede the match.

    Six files are really opened (three NUL-sniffed blobs, ``plan.md``, the
    match, ``projects/2026.md``); the dozen unusable entries ahead of them are
    free, so a budget of exactly six still completes.
    """
    monkeypatch.setattr(provider_fs, "MAX_GREP_FILES", 6)
    outside = tmp_path / "outside.md"
    outside.write_text("needle outside\n")
    for i in range(6):
        (notes / f"a{i}-link.md").symlink_to(outside)
        (notes / f"a{i}-pic.png").write_bytes(b"png")
    for i in range(3):
        (notes / f"b{i}-blob.md").write_bytes(b"needle\x00")
    (notes / ".git").mkdir()
    (notes / ".git" / "x.md").write_text("needle in git\n")
    (notes / "zz-match.md").write_text("needle here\n")
    text, _ = FilesystemProvider([notes]).call("fs_grep", {"query": "needle"})
    assert "zz-match.md:1: needle here" in text
    assert "search incomplete" not in text


def test_a_search_that_fits_the_budget_exactly_is_complete(notes: Path, monkeypatch) -> None:
    """Three entries: ``plan.md``, the ``projects`` folder and ``projects/2026.md``."""
    monkeypatch.setattr(provider_fs, "MAX_GREP_FILES", 2)
    monkeypatch.setattr(provider_fs, "MAX_GREP_ENTRIES", 3)
    text, _ = FilesystemProvider([notes]).call("fs_grep", {"query": "absent-term"})
    assert text == "No matches for 'absent-term'."


def test_file_budget_exhaustion_is_reported_even_with_no_matches(notes: Path, monkeypatch) -> None:
    monkeypatch.setattr(provider_fs, "MAX_GREP_FILES", 1)
    text, is_error = FilesystemProvider([notes]).call("fs_grep", {"query": "2026"})
    assert not is_error
    assert "No matches" in text
    assert "search incomplete" in text
    assert "2026.md" not in text  # the match lives past the budget


def test_entry_budget_stops_a_walk_of_unusable_entries_and_says_so(
    notes: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(provider_fs, "MAX_GREP_ENTRIES", 3)
    for i in range(10):
        (notes / f"a{i}.png").write_bytes(b"png")
    (notes / "zz.md").write_text("needle\n")
    text, _ = FilesystemProvider([notes]).call("fs_grep", {"query": "needle"})
    assert "zz.md" not in text
    assert "No matches" in text
    assert "search incomplete" in text


def test_entry_budget_counts_folders_and_stops_before_descending_past_it(
    tmp_path: Path, monkeypatch
) -> None:
    """A tree of empty folders opens no file, so only the entry budget can stop it."""
    root = tmp_path / "tree"
    root.mkdir()
    for i in range(30):
        (root / f"d{i:02}" / "inner").mkdir(parents=True)
    monkeypatch.setattr(provider_fs, "MAX_GREP_ENTRIES", 5)
    visited: list[str] = []
    real_scandir = provider_fs.os.scandir

    def spy_scandir(path):
        visited.append(str(path))
        return real_scandir(path)

    monkeypatch.setattr(provider_fs.os, "scandir", spy_scandir)
    text, is_error = FilesystemProvider([root]).call("fs_grep", {"query": "needle"})
    assert not is_error
    assert "No matches" in text
    assert "search incomplete" in text
    # The root, then only the five folders the budget paid for; none of their children.
    assert len(visited) == 6
    assert not any(v.endswith("inner") for v in visited)


def test_a_tree_of_folders_that_fits_the_entry_budget_is_complete(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "tree"
    (root / "a" / "b").mkdir(parents=True)
    monkeypatch.setattr(provider_fs, "MAX_GREP_ENTRIES", 2)
    text, _ = FilesystemProvider([root]).call("fs_grep", {"query": "needle"})
    assert text == "No matches for 'needle'."


def test_files_that_fail_to_open_do_not_spend_the_file_budget(notes: Path, monkeypatch) -> None:
    monkeypatch.setattr(provider_fs, "MAX_GREP_FILES", 1)
    for i in range(5):
        (notes / f"a{i}-locked.md").write_text("needle locked\n")
    (notes / "plan.md").unlink()
    (notes / "projects" / "2026.md").unlink()
    (notes / "zz.md").write_text("needle here\n")
    real_open_file = provider_fs._open_file

    def refuse_locked(root: Path, real: Path):
        if "locked" in real.name:
            raise PermissionError(13, "Permission denied")
        return real_open_file(root, real)

    monkeypatch.setattr(provider_fs, "_open_file", refuse_locked)
    text, _ = FilesystemProvider([notes]).call("fs_grep", {"query": "needle"})
    # The file allowance of one went to zz.md, not to the five refused opens…
    assert "zz.md:1: needle here" in text
    assert "stopped after" not in text
    # …but the refused files are still part of the folder that went unsearched.
    assert "5 items in the folder could not be read" in text
    assert "locked" not in text


def test_a_clean_miss_is_not_claimed_when_a_file_could_not_be_opened(
    notes: Path, monkeypatch
) -> None:
    (notes / "locked.md").write_text("needle\n")
    real_open_file = provider_fs._open_file

    def refuse_locked(root: Path, real: Path):
        if real.name == "locked.md":
            raise PermissionError(13, "Permission denied")
        return real_open_file(root, real)

    monkeypatch.setattr(provider_fs, "_open_file", refuse_locked)
    text, is_error = FilesystemProvider([notes]).call("fs_grep", {"query": "needle"})
    assert not is_error
    assert text.startswith("No matches for 'needle' in the part that was searched.")
    assert "1 item in the folder could not be read" in text
    assert "locked" not in text


def test_an_unreadable_folder_is_reported_not_silently_pruned(notes: Path, monkeypatch) -> None:
    """Tests may run as root, where ``chmod 000`` blocks nothing, so refuse the listing."""
    (notes / "private").mkdir()
    (notes / "private" / "a.md").write_text("needle\n")
    real_scandir = provider_fs.os.scandir

    def refuse_private(path):
        if Path(path).name == "private":
            raise PermissionError(13, "Permission denied")
        return real_scandir(path)

    monkeypatch.setattr(provider_fs.os, "scandir", refuse_private)
    text, _ = FilesystemProvider([notes]).call("fs_grep", {"query": "needle"})
    assert text.startswith("No matches for 'needle' in the part that was searched.")
    assert "1 item in the folder could not be read" in text
    assert "private" not in text


class _FakeEntry:
    def __init__(self, name: str) -> None:
        self.name = name

    def is_dir(self) -> bool:
        return False

    def is_symlink(self) -> bool:
        return False


def test_a_huge_folder_is_read_only_as_far_as_the_entry_budget(notes: Path, monkeypatch) -> None:
    """The budget bounds how many names are pulled from the OS, not just visited."""
    monkeypatch.setattr(provider_fs, "MAX_GREP_ENTRIES", 10)
    pulled = 0

    class _HugeListing:
        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            return None

        def __iter__(self):
            nonlocal pulled
            for i in range(1_000_000):
                pulled += 1
                yield _FakeEntry(f"f{i:07}.png")

    monkeypatch.setattr(provider_fs.os, "scandir", lambda _path: _HugeListing())
    text, _ = FilesystemProvider([notes]).call("fs_grep", {"query": "needle"})
    assert pulled == 11  # the allowance, plus the one that says there was more
    assert "search incomplete" in text


def test_a_symlinked_folder_inside_the_root_is_not_descended(notes: Path) -> None:
    (notes / "loop").symlink_to(notes, target_is_directory=True)
    text, _ = FilesystemProvider([notes]).call("fs_grep", {"query": "phase one"})
    assert text == "notes/plan.md:2: Call it phase one."


def test_one_budget_is_shared_across_roots(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(provider_fs, "MAX_GREP_FILES", 2)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "1.md").write_text("x\n")
    (a / "2.md").write_text("x\n")
    (b / "3.md").write_text("needle\n")
    text, _ = FilesystemProvider([a, b]).call("fs_grep", {"query": "needle"})
    assert "3.md" not in text
    assert "search incomplete" in text


# ---------------------------------------------------------------------------
# The open is bound to what was checked (#291)

posix_only = pytest.mark.skipif(
    not provider_fs._HAS_DIRFD,
    reason="descriptor-relative opens need dir_fd and O_NOFOLLOW",
)


@posix_only
def test_a_component_swapped_for_a_symlink_after_the_check_is_not_followed(
    notes: Path, tmp_path: Path, monkeypatch
) -> None:
    """Replace ``projects`` with a link out of the root *between* the
    containment check and the open: the open must refuse, not follow it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "2026.md").write_text("secret outside the grant\n")
    provider = FilesystemProvider([notes])
    real_resolve = provider._resolve

    def resolve_then_swap(relative: str) -> Path:
        real = real_resolve(relative)
        (notes / "projects" / "2026.md").unlink()
        (notes / "projects").rmdir()
        (notes / "projects").symlink_to(outside, target_is_directory=True)
        return real

    monkeypatch.setattr(provider, "_resolve", resolve_then_swap)
    text, is_error = provider.call("fs_read", {"path": "projects/2026.md"})
    assert is_error
    assert "secret" not in text
    assert text == "That path isn't inside the folder you shared, or doesn't exist."


@posix_only
def test_a_file_swapped_for_a_symlink_after_the_check_is_not_followed(
    notes: Path, tmp_path: Path, monkeypatch
) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("secret outside the grant\n")
    provider = FilesystemProvider([notes])
    real_resolve = provider._resolve

    def resolve_then_swap(relative: str) -> Path:
        real = real_resolve(relative)
        real.unlink()
        real.symlink_to(outside)
        return real

    monkeypatch.setattr(provider, "_resolve", resolve_then_swap)
    text, is_error = provider.call("fs_read", {"path": "plan.md"})
    assert is_error
    assert "secret" not in text


@posix_only
def test_a_file_swapped_for_a_fifo_after_the_check_does_not_block(notes: Path, monkeypatch) -> None:
    provider = FilesystemProvider([notes])
    real_resolve = provider._resolve

    def resolve_then_swap(relative: str) -> Path:
        real = real_resolve(relative)
        real.unlink()
        os.mkfifo(real)
        return real

    monkeypatch.setattr(provider, "_resolve", resolve_then_swap)
    text, is_error = provider.call("fs_read", {"path": "plan.md"})
    assert is_error
    assert "readable file" in text


@posix_only
def test_grep_does_not_read_a_candidate_swapped_for_an_outward_link(
    notes: Path, tmp_path: Path, monkeypatch
) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("needle outside the grant\n")
    provider = FilesystemProvider([notes])
    real_contained = provider._contained

    def contained_then_swap(path: Path) -> Path | None:
        real = real_contained(path)
        if real is not None and real.name == "plan.md":
            real.unlink()
            real.symlink_to(outside)
        return real

    monkeypatch.setattr(provider, "_contained", contained_then_swap)
    text, _ = provider.call("fs_grep", {"query": "needle"})
    assert "outside the grant" not in text


@posix_only
def test_list_does_not_report_the_size_of_a_swapped_in_outward_link(
    notes: Path, tmp_path: Path, monkeypatch
) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("x" * 4096)
    provider = FilesystemProvider([notes])
    real_contained = provider._contained

    def contained_then_swap(path: Path) -> Path | None:
        real = real_contained(path)
        if real is not None and real.name == "plan.md":
            real.unlink()
            real.symlink_to(outside)
        return real

    monkeypatch.setattr(provider, "_contained", contained_then_swap)
    text, is_error = provider.call("fs_list", {})
    assert not is_error
    assert "4096" not in text
    assert "plan.md" not in text


@posix_only
def test_list_refuses_a_directory_swapped_for_a_link_after_the_check(
    notes: Path, tmp_path: Path, monkeypatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.md").write_text("x")
    provider = FilesystemProvider([notes])
    real_resolve = provider._resolve

    def resolve_then_swap(relative: str) -> Path:
        real = real_resolve(relative)
        (real / "2026.md").unlink()
        real.rmdir()
        real.symlink_to(outside, target_is_directory=True)
        return real

    monkeypatch.setattr(provider, "_resolve", resolve_then_swap)
    text, is_error = provider.call("fs_list", {"path": "projects"})
    assert is_error
    assert "leak.md" not in text


def test_symlinks_that_stay_inside_the_root_still_work(notes: Path) -> None:
    (notes / "alias.md").symlink_to(notes / "plan.md")
    (notes / "palias").symlink_to(notes / "projects", target_is_directory=True)
    provider = FilesystemProvider([notes])
    text, is_error = provider.call("fs_read", {"path": "alias.md"})
    assert not is_error
    assert "Ship the thing" in text
    text, is_error = provider.call("fs_read", {"path": "palias/2026.md"})
    assert not is_error
    listing, _ = provider.call("fs_list", {})
    assert "alias.md (" in listing
    assert "palias/" in listing


_HAS_PROC_FD = Path("/proc/self/fd").is_dir()


@pytest.fixture
def path_fallback(monkeypatch) -> None:
    """The Windows shape on Linux: no ``dir_fd``, and the handle located via ``/proc``.

    ``/proc/self/fd/N`` names the object open on ``N`` the way
    ``GetFinalPathNameByHandleW`` does on Windows, so the containment check
    sees what was opened, not what a name points at now.
    """
    if not _HAS_PROC_FD:
        pytest.skip("needs /proc/self/fd to stand in for GetFinalPathNameByHandleW")
    monkeypatch.setattr(provider_fs, "_HAS_DIRFD", False)
    monkeypatch.setattr(
        provider_fs, "_final_path", lambda fd: Path(os.readlink(f"/proc/self/fd/{fd}"))
    )


@pytest.mark.usefixtures("path_fallback")
def test_the_path_fallback_without_dir_fd_still_reads_and_refuses(
    notes: Path, tmp_path: Path
) -> None:
    """The Windows path: no ``dir_fd``, so open by path and check the opened handle."""
    provider = FilesystemProvider([notes])
    text, is_error = provider.call("fs_read", {"path": "plan.md"})
    assert not is_error
    assert "Ship the thing" in text
    listing, _ = provider.call("fs_list", {"path": "projects"})
    assert "2026.md (" in listing
    listing, _ = provider.call("fs_list", {})
    assert "projects/" in listing
    assert "plan.md (" in listing
    grep, _ = provider.call("fs_grep", {"query": "phase one"})
    assert "plan.md:2" in grep
    outside = tmp_path / "outside.md"
    outside.write_text("secret\n")
    (notes / "link.md").symlink_to(outside)
    _, is_error = provider.call("fs_read", {"path": "link.md"})
    assert is_error


@pytest.mark.usefixtures("path_fallback")
def test_the_path_fallback_judges_the_opened_file_not_a_restored_name(
    notes: Path, tmp_path: Path, monkeypatch
) -> None:
    """Greptile's race on #344: point the name outside for the open, restore it after.

    The old fallback re-resolved the *name* (inside again) and compared it with
    ``samestat``, which a third swap defeats; that third swap is modelled by
    making ``samestat`` agree. The check on the opened handle sees the outside
    file whatever the name says afterwards.
    """
    outside = tmp_path / "outside.md"
    outside.write_text("secret outside the grant\n")
    target = notes / "plan.md"
    provider = FilesystemProvider([notes])
    real_resolve = provider._resolve
    real_open = provider_fs.os.open

    def resolve_then_swap(relative: str) -> Path:
        real = real_resolve(relative)
        real.unlink()
        real.symlink_to(outside)
        return real

    def open_then_restore(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if Path(path) == target and target.is_symlink():
            target.unlink()
            target.write_text("Ship the thing.\n")
        return fd

    monkeypatch.setattr(provider, "_resolve", resolve_then_swap)
    monkeypatch.setattr(provider_fs.os, "open", open_then_restore)
    monkeypatch.setattr(provider_fs.os.path, "samestat", lambda _a, _b: True)
    text, is_error = provider.call("fs_read", {"path": "plan.md"})
    assert is_error
    assert "secret" not in text
    assert text == "That path isn't inside the folder you shared, or doesn't exist."


@pytest.mark.usefixtures("path_fallback")
def test_the_path_fallback_reads_what_it_opened_when_the_name_moves_after(
    notes: Path, tmp_path: Path, monkeypatch
) -> None:
    """A swap after the open cannot redirect the read: the descriptor is the file checked."""
    outside = tmp_path / "outside.md"
    outside.write_text("secret\n")
    real_fstat = os.fstat

    def fstat_then_swap(fd):
        result = real_fstat(fd)
        target = notes / "plan.md"
        if target.exists() and not target.is_symlink():
            target.unlink()
            target.symlink_to(outside)
        return result

    monkeypatch.setattr(provider_fs.os, "fstat", fstat_then_swap)
    text, _ = FilesystemProvider([notes]).call("fs_read", {"path": "plan.md"})
    assert "secret" not in text
    assert "Ship the thing" in text


@pytest.mark.usefixtures("path_fallback")
def test_the_path_fallback_refuses_a_directory_as_not_a_file(notes: Path) -> None:
    """Windows raises PermissionError opening a directory; the answer must not depend on it."""
    text, is_error = FilesystemProvider([notes]).call("fs_read", {"path": "projects"})
    assert is_error
    assert "readable file" in text


@pytest.mark.usefixtures("path_fallback")
def test_the_path_fallback_does_not_list_a_directory_swapped_for_an_outward_link(
    notes: Path, tmp_path: Path, monkeypatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.md").write_text("x")
    provider = FilesystemProvider([notes])
    real_resolve = provider._resolve

    def resolve_then_swap(relative: str) -> Path:
        real = real_resolve(relative)
        (real / "2026.md").unlink()
        real.rmdir()
        real.symlink_to(outside, target_is_directory=True)
        return real

    monkeypatch.setattr(provider, "_resolve", resolve_then_swap)
    text, is_error = provider.call("fs_list", {"path": "projects"})
    assert is_error
    assert "leak.md" not in text


@pytest.mark.usefixtures("path_fallback")
def test_the_path_fallback_does_not_size_an_entry_swapped_for_an_outward_link(
    notes: Path, tmp_path: Path, monkeypatch
) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("x" * 4096)
    provider = FilesystemProvider([notes])
    real_contained = provider._contained

    def contained_then_swap(path: Path) -> Path | None:
        real = real_contained(path)
        if real is not None and real.name == "plan.md":
            real.unlink()
            real.symlink_to(outside)
        return real

    monkeypatch.setattr(provider, "_contained", contained_then_swap)
    text, is_error = provider.call("fs_list", {})
    assert not is_error
    assert "4096" not in text
    assert "plan.md" not in text
    assert "projects/" in text


def test_without_dir_fd_or_a_locatable_handle_the_provider_refuses(
    notes: Path, monkeypatch
) -> None:
    """No ``openat`` and no way to ask a handle where it is: refuse, don't check by name."""
    monkeypatch.setattr(provider_fs, "_HAS_DIRFD", False)
    monkeypatch.setattr(provider_fs, "_final_path", lambda _fd: None)
    provider = FilesystemProvider([notes])
    for tool, args in (("fs_read", {"path": "plan.md"}), ("fs_list", {"path": "projects"})):
        text, is_error = provider.call(tool, args)
        assert is_error, tool
        assert "can't confirm" in text
        assert "Ship the thing" not in text


@pytest.mark.skipif(sys.platform == "win32", reason="the stub only exists off Windows")
def test_final_path_is_unknowable_off_windows() -> None:
    assert provider_fs._final_path(0) is None


# ---------------------------------------------------------------------------
# The Windows handle helpers' pure halves


def test_extended_prefixes_are_stripped_to_realpath_spelling() -> None:
    assert _winfs.strip_extended_prefix("\\\\?\\C:\\Users\\x") == "C:\\Users\\x"
    assert _winfs.strip_extended_prefix("\\\\?\\UNC\\srv\\share\\x") == "\\\\srv\\share\\x"
    assert _winfs.strip_extended_prefix("C:\\plain") == "C:\\plain"


def _dir_record(name: str, attributes: int, *, last: bool) -> bytes:
    encoded = name.encode("utf-16-le")
    size = _winfs._FULL_DIR_INFO.size + len(encoded)
    padded = (size + 7) // 8 * 8
    header = _winfs._FULL_DIR_INFO.pack(
        0 if last else padded, 0, 0, 0, 0, 0, 0, 0, attributes, len(encoded), 0
    )
    return (header + encoded).ljust(padded, b"\x00")


def test_directory_records_parse_to_names_and_kinds() -> None:
    buf = (
        _dir_record(".", 0x10, last=False)
        + _dir_record("..", 0x10, last=False)
        + _dir_record("notes", 0x10, last=False)
        + _dir_record("plan.md", 0x20, last=False)
        + _dir_record("caf\u00e9.txt", 0x20, last=True)
    )
    assert _winfs.parse_full_dir_info(buf + b"\x00" * 64) == [
        ("notes", True),
        ("plan.md", False),
        ("caf\u00e9.txt", False),
    ]
