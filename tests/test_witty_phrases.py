"""The loading-phrase list and the helper that rotates it."""

from __future__ import annotations

from pepper_cli import witty_phrases
from pepper_cli.witty_phrases import WITTY_PHRASES, pick, tail


class _FirstChoice:
    """A deterministic chooser: the worst case for a repeat is picking the same slot."""

    def choice(self, seq: tuple[str, ...], /) -> str:
        return seq[0]


def test_the_list_is_well_formed() -> None:
    assert WITTY_PHRASES
    assert len(set(WITTY_PHRASES)) == len(WITTY_PHRASES)
    assert all(phrase.strip() and len(phrase) <= 90 for phrase in WITTY_PHRASES)
    assert witty_phrases.ROTATE_SECONDS == 4.0


def test_removed_lines_stay_out_and_the_dog_stays_in() -> None:
    for gone in ("I'm Feeling Lucky", "Rickrolling my boss", "New line? That’s Ctrl+J."):
        assert not any(gone in phrase for phrase in WITTY_PHRASES)
    assert "Giving Cloudy a pat on the head" in WITTY_PHRASES


def test_pick_never_repeats_the_previous_phrase() -> None:
    rng = _FirstChoice()
    previous = None
    for _ in range(500):
        chosen = pick(previous, rng)
        assert chosen in WITTY_PHRASES
        assert chosen != previous
        previous = chosen


def test_tail_flattens_and_keeps_the_end() -> None:
    assert tail("a\n b") == "a b"
    long = "x" * 100 + "END"
    assert tail(long).startswith("…") and tail(long).endswith("END") and len(tail(long)) == 70
