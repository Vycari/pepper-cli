"""``pepper fortune`` and the rare startup pepper (``pepper_cli.whimsy``)."""

from __future__ import annotations

import json
import random

import pytest

from pepper_cli import cli, whimsy


class _Rolls(random.Random):
    """A generator whose ``random()`` returns a fixed value, so the coin flip is pinned."""

    def __init__(self, roll: float) -> None:
        super().__init__(0)
        self._roll = roll

    def random(self) -> float:
        return self._roll


@pytest.fixture(autouse=True)
def _whimsy_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PEPPER_NO_WHIMSY", raising=False)


def test_fortunes_are_one_short_line_each() -> None:
    assert len(set(whimsy.FORTUNES)) == len(whimsy.FORTUNES)
    for fortune in whimsy.FORTUNES:
        assert "\n" not in fortune
        assert len(fortune) <= 80, fortune


def test_pepper_art_is_plain_ascii_that_fits_a_narrow_terminal() -> None:
    assert whimsy.PEPPER_ART.isascii()
    assert max(len(line) for line in whimsy.PEPPER_ART.splitlines()) <= 40


def test_fortune_prints_one_line_without_credentials(tmp_home, capsys) -> None:
    rc = cli.main(["fortune"])
    out = capsys.readouterr()
    assert rc == 0
    assert out.out.rstrip("\n") in whimsy.FORTUNES
    assert out.err == ""


def test_fortune_json(tmp_home, capsys) -> None:
    rc = cli.main(["fortune", "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["fortune"] in whimsy.FORTUNES


def test_fortune_ignores_the_whimsy_switch(tmp_home, capsys, monkeypatch) -> None:
    monkeypatch.setenv("PEPPER_NO_WHIMSY", "1")
    assert cli.main(["fortune"]) == 0
    assert capsys.readouterr().out.rstrip("\n") in whimsy.FORTUNES


def test_startup_pepper_appears_on_a_winning_roll() -> None:
    art = whimsy.startup_pepper(is_tty=True, rng=_Rolls(0.0))
    assert art is not None
    assert art.startswith(whimsy.PEPPER_ART)
    assert art.splitlines()[-1].strip() in whimsy.FORTUNES


def test_startup_pepper_is_rare() -> None:
    assert whimsy.startup_pepper(is_tty=True, rng=_Rolls(whimsy.STARTUP_PEPPER_CHANCE)) is None
    assert 0 < whimsy.STARTUP_PEPPER_CHANCE <= 0.05


def test_startup_pepper_never_appears_off_a_terminal() -> None:
    assert whimsy.startup_pepper(is_tty=False, rng=_Rolls(0.0)) is None


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_startup_pepper_turns_off(monkeypatch, value: str) -> None:
    monkeypatch.setenv("PEPPER_NO_WHIMSY", value)
    assert whimsy.startup_pepper(is_tty=True, rng=_Rolls(0.0)) is None


@pytest.mark.parametrize("value", ["", "0", "false", "no"])
def test_startup_pepper_stays_on_for_falsey_values(monkeypatch, value: str) -> None:
    monkeypatch.setenv("PEPPER_NO_WHIMSY", value)
    assert whimsy.startup_pepper(is_tty=True, rng=_Rolls(0.0)) is not None


def test_splash_fortune_on_a_winning_roll() -> None:
    assert whimsy.splash_fortune(rng=_Rolls(0.0)) in whimsy.FORTUNES


def test_splash_fortune_is_rare() -> None:
    assert whimsy.splash_fortune(rng=_Rolls(whimsy.STARTUP_PEPPER_CHANCE)) is None


def test_splash_fortune_turns_off(monkeypatch) -> None:
    monkeypatch.setenv("PEPPER_NO_WHIMSY", "1")
    assert whimsy.splash_fortune(rng=_Rolls(0.0)) is None


def test_splash_art_is_plain_ascii_that_fits_a_small_terminal() -> None:
    lines = whimsy.SPLASH_ART.splitlines()
    assert whimsy.SPLASH_ART.isascii()
    assert len(lines) <= 18
    assert max(len(line) for line in lines) <= 60
