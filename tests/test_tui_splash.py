"""The rare full-screen pepper at TUI start (``SplashScreen``)."""

from __future__ import annotations

import pytest

from pepper_cli import tui, whimsy
from pepper_cli.tui import app as tui_app
from pepper_cli.tui.screens import SplashScreen
from tests import tui_support as ts
from tests.tui_support import until


async def test_no_splash_by_default() -> None:
    app = tui_app.PepperApp(ts.standard_server().client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        assert not isinstance(app.screen, SplashScreen)


async def test_splash_shows_the_pepper_and_any_key_closes_it() -> None:
    fortune = whimsy.FORTUNES[0]
    app = tui_app.PepperApp(ts.standard_server().client(), splash=fortune)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, SplashScreen)
        text = " ".join(str(w.render()) for w in app.screen.query("Static"))
        assert fortune in text
        assert "'---._" in text
        # The app's first reads run behind the splash.
        await until(pilot, lambda: len(app.rooms) == 2)
        await pilot.press("x")
        await until(pilot, lambda: not isinstance(app.screen, SplashScreen))
        # The key that closed it did not reach the app underneath.
        assert app.return_code is None


async def test_splash_closes_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SplashScreen, "SECONDS", 0.05)
    app = tui_app.PepperApp(ts.standard_server().client(), splash=whimsy.FORTUNES[0])
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: not isinstance(app.screen, SplashScreen))


def test_run_passes_the_rolled_splash(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    class _App:
        return_code = 0

        def __init__(self, *_: object, **kwargs: object) -> None:
            seen.update(kwargs)

        def run(self) -> None:
            return None

    monkeypatch.setattr(tui_app, "PepperApp", _App)
    monkeypatch.setattr(whimsy, "splash_fortune", lambda **_: "a fortune")
    from pepper_cli.secret import SecretStr

    assert tui.run(server_url="http://example.com", token=SecretStr("pat_supersecret")) == 0
    assert seen["splash"] == "a fortune"
