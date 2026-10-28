"""Tests for the TUI update check, footer binding, and upgrade flow."""

from __future__ import annotations

import subprocess

import pytest

from pepper_cli import credentials, tui
from pepper_cli.secret import SecretStr
from pepper_cli.tui import app as tui_app
from pepper_cli.tui.screens import ConfirmScreen
from tests import tui_support as ts
from tests.tui_support import until

SIZE = (120, 40)


@pytest.fixture(autouse=True)
def _fast_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tui_app, "utcnow", lambda: ts.NOW)


async def test_tui_registers_upgrade_binding_when_update_available() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client(), update_checker=lambda: "0.4.0")
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: pilot.app.update_version == "0.4.0")
        binding = pilot.app.screen.active_bindings.get("f10")
        assert binding is not None
        assert binding.binding.description == "Update"
        assert binding.binding.show is True


async def test_ctrl_u_deletes_to_start_of_line_in_composer() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client(), update_checker=lambda: "0.4.0")
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: pilot.app.update_version == "0.4.0")
        pilot.app.composer.focus()
        await pilot.pause()
        await pilot.press("a", "b", "c")
        # In composer, ctrl+u deletes to line start without opening ConfirmScreen
        await pilot.press("ctrl+u")
        await pilot.pause()
        assert not isinstance(pilot.app.screen, ConfirmScreen)
        assert pilot.app.composer.text == ""


async def test_f10_works_when_composer_has_focus() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client(), update_checker=lambda: "0.4.0")
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: pilot.app.update_version == "0.4.0")
        pilot.app.composer.focus()
        await pilot.pause()
        await pilot.press("f10")
        await until(pilot, lambda: isinstance(pilot.app.screen, ConfirmScreen))


async def test_tui_upgrade_action_cancel() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client(), update_checker=lambda: "0.4.0")
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: pilot.app.update_version == "0.4.0")
        pilot.app.action_upgrade()
        await until(pilot, lambda: isinstance(pilot.app.screen, ConfirmScreen))
        await pilot.press("n")
        await pilot.pause()
        assert not isinstance(pilot.app.screen, ConfirmScreen)
        assert pilot.app.upgrade_requested is False


async def test_confirm_screen_enter_cancels_safely() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client(), update_checker=lambda: "0.4.0")
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: pilot.app.update_version == "0.4.0")
        pilot.app.action_upgrade()
        await until(pilot, lambda: isinstance(pilot.app.screen, ConfirmScreen))
        # Pressing enter on focused #confirm-no dismisses False
        await pilot.press("enter")
        await pilot.pause()
        assert not isinstance(pilot.app.screen, ConfirmScreen)
        assert pilot.app.upgrade_requested is False


async def test_tui_upgrade_action_confirm() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client(), update_checker=lambda: "0.4.0")
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: pilot.app.update_version == "0.4.0")
        pilot.app.action_upgrade()
        await until(pilot, lambda: isinstance(pilot.app.screen, ConfirmScreen))
        await pilot.press("y")
        await pilot.pause()
        assert pilot.app.upgrade_requested is True


async def test_tui_upgrade_action_allowed_in_fatal_state() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client(), update_checker=lambda: "0.4.0")
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: pilot.app.update_version == "0.4.0")
        pilot.app.fatal = "Upgrade required to talk to this server."
        pilot.app.action_upgrade()
        await until(pilot, lambda: isinstance(pilot.app.screen, ConfirmScreen))
        await pilot.press("y")
        await pilot.pause()
        assert pilot.app.upgrade_requested is True


async def test_f10_in_fatal_state_opens_confirm_screen() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client(), update_checker=lambda: "0.4.0")
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: pilot.app.update_version == "0.4.0")
        pilot.app.fatal = "Upgrade required to talk to this server."
        await pilot.press("f10")
        await until(pilot, lambda: isinstance(pilot.app.screen, ConfirmScreen))


async def test_f10_in_fatal_state_works_when_composer_has_focus() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client(), update_checker=lambda: "0.4.0")
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: pilot.app.update_version == "0.4.0")
        pilot.app.fatal = "Upgrade required to talk to this server."
        pilot.app.composer.focus()
        await pilot.pause()
        await pilot.press("f10")
        await until(pilot, lambda: isinstance(pilot.app.screen, ConfirmScreen))


async def test_tui_upgrade_mouse_click_cancel() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client(), update_checker=lambda: "0.4.0")
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: pilot.app.update_version == "0.4.0")
        pilot.app.action_upgrade()
        await until(pilot, lambda: isinstance(pilot.app.screen, ConfirmScreen))
        await until(pilot, lambda: bool(pilot.app.screen.query("#confirm-no")))
        await pilot.click("#confirm-no")
        await pilot.pause()
        assert not isinstance(pilot.app.screen, ConfirmScreen)
        assert pilot.app.upgrade_requested is False


async def test_tui_upgrade_mouse_click_confirm() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client(), update_checker=lambda: "0.4.0")
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: pilot.app.update_version == "0.4.0")
        pilot.app.action_upgrade()
        await until(pilot, lambda: isinstance(pilot.app.screen, ConfirmScreen))
        await until(pilot, lambda: bool(pilot.app.screen.query("#confirm-yes")))
        await pilot.click("#confirm-yes")
        await pilot.pause()
        assert pilot.app.upgrade_requested is True


def test_perform_upgrade_non_windows_success(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(tui, "detect_install_method", lambda: "uv-tool")
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/uv")
    ran = []

    def fake_run(cmd, check=False):
        ran.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    rc = tui._perform_upgrade("0.4.0")
    assert rc == 0
    assert ran == [["/usr/bin/uv", "tool", "upgrade", "vycari-pepper"]]
    captured = capsys.readouterr()
    assert "Upgrade command completed. Run 'pepper' to start." in captured.err


def test_perform_upgrade_windows(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(tui, "detect_install_method", lambda: "uv-tool")
    rc = tui._perform_upgrade("0.4.0")
    assert rc == 0
    captured = capsys.readouterr()
    assert "To upgrade to v0.4.0, run:\n  uv tool upgrade vycari-pepper" in captured.err


def test_perform_upgrade_failure(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(tui, "detect_install_method", lambda: "uv-tool")
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/uv")

    def fake_run(cmd, check=False):
        return subprocess.CompletedProcess(cmd, 1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    rc = tui._perform_upgrade("0.4.0")
    assert rc == 1
    captured = capsys.readouterr()
    assert "Upgrade failed" in captured.err


def test_perform_upgrade_missing_uv(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(tui, "detect_install_method", lambda: "uv-tool")
    monkeypatch.setattr("shutil.which", lambda _name: None)
    rc = tui._perform_upgrade("0.4.0")
    assert rc == 1
    captured = capsys.readouterr()
    assert "'uv' executable not found" in captured.err


def test_perform_upgrade_pipx(monkeypatch, capsys) -> None:
    monkeypatch.setattr(tui, "detect_install_method", lambda: "pipx")
    rc = tui._perform_upgrade("0.4.0")
    assert rc == 0
    captured = capsys.readouterr()
    assert "To upgrade to v0.4.0, run:\n  pipx upgrade vycari-pepper" in captured.err


def test_perform_upgrade_pip(monkeypatch, capsys) -> None:
    monkeypatch.setattr(tui, "detect_install_method", lambda: "pip")
    rc = tui._perform_upgrade("0.4.0")
    assert rc == 0
    captured = capsys.readouterr()
    assert "To upgrade to v0.4.0, run:\n  pip install --upgrade vycari-pepper" in captured.err


def test_detect_install_method_variants(monkeypatch, tmp_path) -> None:
    uv_env = tmp_path / "custom-uv-env"
    uv_env.mkdir()
    (uv_env / "uv-receipt.toml").write_text("", encoding="utf-8")
    monkeypatch.setattr("sys.prefix", str(uv_env))
    assert tui.detect_install_method() == "uv-tool"

    monkeypatch.setattr("sys.prefix", "/home/user/.local/pipx/venvs/vycari-pepper")
    assert tui.detect_install_method() == "pipx"

    monkeypatch.setattr("sys.prefix", "/home/user/.venv")
    assert tui.detect_install_method() == "pip"


def test_tui_run_performs_upgrade_when_requested(monkeypatch) -> None:
    called = []
    creds = credentials.Credentials(server_url="http://example.com", token=SecretStr("pep_pat_x"))

    class FakeApp:
        def __init__(self, *args, **kwargs):
            self.upgrade_requested = True
            self.update_version = "0.4.0"
            self.return_code = 0

        def run(self):
            pass

    monkeypatch.setattr("pepper_cli.tui.app.PepperApp", FakeApp)
    monkeypatch.setattr(tui, "_perform_upgrade", lambda ver: called.append(ver) or 0)

    rc = tui.run(server_url=creds.server_url, token=creds.token)
    assert rc == 0
    assert called == ["0.4.0"]


def test_quitting_does_not_wait_for_a_blocked_update_check() -> None:
    """Exercise asyncio executor shutdown and interpreter exit in a child process."""
    import sys
    import textwrap

    code = textwrap.dedent(
        """
        import threading
        from pathlib import Path
        from pepper_cli.tui import app as tui_app
        from tests import tui_support as ts

        started = threading.Event()
        blocked = threading.Event()
        def check():
            started.set()
            blocked.wait()
            return None

        class Probe(tui_app.PepperApp):
            CSS_PATH = str(Path(tui_app.__file__).with_name('styles.tcss'))
            def on_mount(self):
                # Compose the real app, but skip the unrelated network poll loops.
                self._start_update_check()
                self.set_interval(0.01, self.quit_when_checking)
            def quit_when_checking(self):
                if started.is_set():
                    self.exit()

        app = Probe(ts.standard_server().client(), live=False, update_checker=check)
        app.run(headless=True, size=(120, 40))
        assert started.is_set()
        assert app._quitting
        assert not blocked.is_set()
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr


async def test_update_result_after_quit_is_dropped(monkeypatch) -> None:
    import threading

    started = threading.Event()
    release = threading.Event()
    posted = threading.Event()
    delivered = []

    stopped = []

    def check(*, should_stop):
        started.set()
        release.wait()
        stopped.append(should_stop())
        return "99.0.0"

    monkeypatch.setattr("pepper_cli._version.check_latest_version", check)
    app = tui_app.PepperApp(ts.standard_server().client(), live=False)
    update_callback = delivered.append
    original_post = app._post

    def post(*args):
        try:
            original_post(*args)
        finally:
            if args[0] == update_callback:
                posted.set()

    monkeypatch.setattr(app, "_post", post)
    monkeypatch.setattr(app, "_on_update_available", update_callback)
    try:
        async with app.run_test(size=SIZE) as pilot:
            await until(pilot, started.is_set)
            app.exit()
        release.set()
        assert posted.wait(2)
        assert stopped == [True]
        assert delivered == []
        assert app.update_version is None
    finally:
        release.set()
