"""Tests for the TUI update check, footer binding, and upgrade flow."""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import shutil
import subprocess

import httpx
import pytest

from pepper_cli import credentials, tui
from pepper_cli.api_client import UpgradeRequiredError
from pepper_cli.secret import SecretStr
from pepper_cli.tui import app as tui_app
from pepper_cli.tui.screens import ConfirmScreen
from tests import tui_support as ts
from tests.tui_support import until

SIZE = (120, 40)

#: The 426 body the server really sends (pepper ``platform/client_version.py``).
REAL_426_BODY = {
    "detail": (
        "pepper-cli 0.3.0 is no longer supported (minimum 0.4.0). "
        "Upgrade with: uv tool upgrade vycari-pepper"
    ),
    "code": "cli_upgrade_required",
    "minimum_version": "0.4.0",
    "client_version": "0.3.0",
    "upgrade_command": "uv tool upgrade vycari-pepper",
}


def real_426(**overrides: object) -> UpgradeRequiredError:
    """The error the client raises for the real 426 body; an override of None drops the field."""
    from pepper_cli import api_client

    body = {k: v for k, v in {**REAL_426_BODY, **overrides}.items() if v is not None}
    try:
        api_client._raise_for_error(httpx.Response(426, json=body))
    except UpgradeRequiredError as exc:
        return exc
    raise AssertionError("no UpgradeRequiredError")


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
    rc = tui._perform_upgrade("0.4.0", required="0.4.0")
    assert rc == 0
    assert ran == [["/usr/bin/uv", "tool", "upgrade", "vycari-pepper"]]
    captured = capsys.readouterr()
    assert "Upgrade command completed. Run 'pepper' to start." in captured.err


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_a_pinned_prompt_says_the_pin_is_removed(monkeypatch, tmp_path, platform) -> None:
    _uv_receipt(monkeypatch, tmp_path, "==0.3.0")
    monkeypatch.setattr("sys.platform", platform)
    question, detail = tui.upgrade_prompt("0.7.0")
    assert question == "Upgrade to v0.7.0, removing the pin vycari-pepper==0.3.0?"
    # A 426 names no version but must still reach a pinned install.
    assert tui.upgrade_prompt("server text", offer=False)[0] == (
        "Upgrade to the latest release, removing the pin vycari-pepper==0.3.0?"
    )
    assert "uv tool install --force vycari-pepper" in detail
    assert "pin is removed" in detail


@pytest.mark.parametrize(
    ("platform", "expect"),
    [
        # Windows exits and prints the command: there it is the user who must run it.
        (
            "win32",
            "Pepper will exit. Run 'uv tool install --force vycari-pepper' in your terminal.",
        ),
        # Everywhere else ``_perform_upgrade`` runs the reinstall itself, so the prompt must
        # not send the user to run it again (#127 review).
        (
            "linux",
            "Pepper will exit and run 'uv tool install --force vycari-pepper' in your terminal.",
        ),
        (
            "darwin",
            "Pepper will exit and run 'uv tool install --force vycari-pepper' in your terminal.",
        ),
    ],
)
def test_a_pinned_prompt_says_who_runs_the_reinstall(
    monkeypatch, tmp_path, platform, expect
) -> None:
    _uv_receipt(monkeypatch, tmp_path, "==0.3.0")
    monkeypatch.setattr("sys.platform", platform)
    _, detail = tui.upgrade_prompt("0.7.0")
    assert expect in detail


def test_an_unpinned_prompt_keeps_its_wording(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr("sys.prefix", str(tmp_path / "plain"))
    assert tui.upgrade_prompt("0.7.0") == (
        "Upgrade Pepper to v0.7.0?",
        "Pepper will exit. Run 'pip install --upgrade vycari-pepper' in your terminal.",
    )


async def test_a_426_opens_f10_and_a_pinned_reinstall(monkeypatch, tmp_path) -> None:
    """A pinned install gets no background offer, but the server refusing every request is
    not an offer: it must still reach F10, say the pin goes, and only after ``y`` (#127)."""
    server = ts.standard_server()
    _uv_receipt(monkeypatch, tmp_path, "==0.3.0")
    app = tui_app.PepperApp(server.client(), update_checker=lambda: None)

    def refused() -> None:
        raise real_426()

    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: not app._loops["rail"].busy)
        monkeypatch.setattr(app._rail, "step", refused)
        app._run("rail")
        await until(pilot, lambda: app.fatal is not None)
        # Open, but the server's sentence is not a release: F10 must not print it as one.
        assert app.update_version == tui_app.UNKNOWN_RELEASE
        assert app.upgrade_requested is False

        await pilot.press("f10")
        await until(pilot, lambda: isinstance(pilot.app.screen, ConfirmScreen))
        shown = " ".join(
            str(widget.render())
            for widget in pilot.app.screen.query(".dialog-title, .dialog-detail")
        )
        assert "Upgrade to v0.4.0, removing the pin vycari-pepper==0.3.0?" in shown
        assert "uv tool install --force vycari-pepper" in shown
        assert app.upgrade_requested is False  # opened is not accepted

        await pilot.press("y")
        await until(pilot, lambda: app.upgrade_requested)


def test_perform_upgrade_reinstalls_a_pinned_tool(monkeypatch, tmp_path, capsys) -> None:
    _uv_receipt(monkeypatch, tmp_path, "==0.3.0")
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/uv")
    ran = []

    def fake_run(cmd, check=False):
        ran.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert tui._perform_upgrade("0.7.0", required="0.7.0") == 0
    assert ran == [["/usr/bin/uv", "tool", "install", "--force", "vycari-pepper"]]


def test_perform_upgrade_windows(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(tui, "detect_install_method", lambda: "uv-tool")
    rc = tui._perform_upgrade("0.4.0", required="0.4.0")
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
    rc = tui._perform_upgrade("0.4.0", required="0.4.0")
    assert rc == 1
    captured = capsys.readouterr()
    assert "Upgrade failed" in captured.err


def test_perform_upgrade_missing_uv(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(tui, "detect_install_method", lambda: "uv-tool")
    monkeypatch.setattr("shutil.which", lambda _name: None)
    rc = tui._perform_upgrade("0.4.0", required="0.4.0")
    assert rc == 1
    captured = capsys.readouterr()
    assert "'uv' executable not found" in captured.err


def test_perform_upgrade_pipx(monkeypatch, capsys) -> None:
    monkeypatch.setattr(tui, "detect_install_method", lambda: "pipx")
    rc = tui._perform_upgrade("0.4.0", required="0.4.0")
    assert rc == 0
    captured = capsys.readouterr()
    assert "To upgrade to v0.4.0, run:\n  pipx upgrade vycari-pepper" in captured.err


def test_perform_upgrade_pip(monkeypatch, capsys) -> None:
    monkeypatch.setattr(tui, "detect_install_method", lambda: "pip")
    rc = tui._perform_upgrade("0.4.0", required="0.4.0")
    assert rc == 0
    captured = capsys.readouterr()
    assert "To upgrade to v0.4.0, run:\n  pip install --upgrade vycari-pepper" in captured.err


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs a real uv to write its receipt")
def test_the_command_really_moves_a_real_pinned_uv_tool(tmp_path) -> None:
    """The fixture above is our idea of uv's receipt; this pins the idea to uv's output.

    uv keeps the pin on ``uv tool upgrade`` and, on a plain re-install, rewrites the receipt
    unpinned while leaving the old version installed, so a working fix needs ``--force``.

    Both versions come from a local index holding a stub distribution under the real name,
    so nothing here asks PyPI or a mirror what ``latest`` is: the test says 0.4.0, and a
    release landing upstream mid-run cannot change the answer.
    """
    from pepper_cli import _version

    index = tmp_path / "index"
    for release in ("0.3.0", "0.4.0"):
        _publish_stub(index, _version.DISTRIBUTION, release)

    env = {
        **os.environ,
        "UV_TOOL_DIR": str(tmp_path / "tools"),
        "UV_TOOL_BIN_DIR": str(tmp_path / "bin"),
        "UV_INDEX_URL": index.as_uri(),
        # A resolver cache from the host would answer from PyPI instead of the stub.
        "UV_NO_CACHE": "1",
    }

    def uv(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["uv", *args], env=env, capture_output=True, text=True, timeout=120, check=False
        )

    installed = uv("tool", "install", f"{_version.DISTRIBUTION}==0.3.0")
    assert installed.returncode == 0, installed.stderr

    tool_env = tmp_path / "tools" / _version.DISTRIBUTION
    receipt = tool_env / "uv-receipt.toml"
    assert 'specifier = "==0.3.0"' in receipt.read_text(encoding="utf-8")
    # The version really installed, from the environment's own metadata rather than the receipt.
    assert _installed(tool_env) == "0.3.0"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("sys.prefix", str(tool_env))
        assert _version.uv_tool_requirement() == f"{_version.DISTRIBUTION}==0.3.0"
        assert _version.is_pinned(f"{_version.DISTRIBUTION}==0.3.0")
        command = tui.upgrade_command()
    assert command[:3] == ["uv", "tool", "install"]

    moved = uv(*command[1:])
    assert moved.returncode == 0, moved.stderr
    assert "specifier" not in receipt.read_text(encoding="utf-8")
    # Not just the receipt: the package the user runs is now the newer 0.4.0, not 0.3.0.
    assert _installed(tool_env) == "0.4.0"


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs a real uv to keep the exclusion")
def test_a_426_removes_a_real_exclusion_on_the_required_release(tmp_path) -> None:
    """``uv tool install vycari-pepper!=0.4.0`` answers ``uv tool upgrade`` "Nothing to
    upgrade" with exit 0 (VR-33db9e57), so the 426 path must re-install without it."""
    from pepper_cli import _version

    index = tmp_path / "index"
    for release in ("0.3.0", "0.4.0"):
        _publish_stub(index, _version.DISTRIBUTION, release)
    env = {
        **os.environ,
        "UV_TOOL_DIR": str(tmp_path / "tools"),
        "UV_TOOL_BIN_DIR": str(tmp_path / "bin"),
        "UV_INDEX_URL": index.as_uri(),
        "UV_NO_CACHE": "1",
    }

    def uv(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["uv", *args], env=env, capture_output=True, text=True, timeout=120, check=False
        )

    installed = uv("tool", "install", f"{_version.DISTRIBUTION}!=0.4.0")
    assert installed.returncode == 0, installed.stderr
    tool_env = tmp_path / "tools" / _version.DISTRIBUTION
    assert _installed(tool_env) == "0.3.0"

    target = real_426().minimum_version
    assert target == "0.4.0"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("sys.prefix", str(tool_env))
        mp.setattr("sys.platform", "linux")
        assert _version.is_pinned("vycari-pepper!=0.4.0", None) is False
        assert _version.is_pinned("vycari-pepper!=0.4.0", target) is True
        command = tui.upgrade_command(target)
    assert command == ["uv", "tool", "install", "--force", _version.DISTRIBUTION]

    moved = uv(*command[1:])
    assert moved.returncode == 0, moved.stderr
    assert "specifier" not in (tool_env / "uv-receipt.toml").read_text(encoding="utf-8")
    assert _installed(tool_env) == "0.4.0"


def _publish_stub(index, name: str, release: str) -> None:
    """Publish ``name`` ``release`` to a PEP 503 index as a wheel uv accepts unmodified.

    A wheel without ``RECORD`` or a module for its entry point is repacked or rejected, so
    the stub carries both and a console script that imports.
    """
    import hashlib
    import zipfile
    from io import BytesIO

    from packaging.utils import canonicalize_name

    canonical = canonicalize_name(name)
    module = canonical.replace("-", "_")
    project = index / canonical
    project.mkdir(parents=True, exist_ok=True)
    wheel_name = f"{module}-{release}-py3-none-any.whl"
    dist_info = f"{module}-{release}.dist-info"
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as wheel:
        wheel.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {release}\n",
        )
        wheel.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: stub\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        wheel.writestr(
            f"{dist_info}/entry_points.txt", f"[console_scripts]\n{canonical} = {module}:main\n"
        )
        wheel.writestr(f"{dist_info}/top_level.txt", f"{module}\n")
        wheel.writestr(f"{dist_info}/RECORD", "")
        wheel.writestr(f"{module}/__init__.py", "")
        wheel.writestr(f"{module}/__main__.py", "def main():\n    return 0\n")
    wheel_bytes = buffer.getvalue()
    (project / wheel_name).write_bytes(wheel_bytes)
    links = "".join(
        f'<a href="{other.name}#sha256={hashlib.sha256(other.read_bytes()).hexdigest()}">'
        f"{other.name}</a><br/>"
        for other in sorted(project.glob("*.whl"))
    )
    (project / "index.html").write_text(
        f"<!DOCTYPE html><html><body>{links}</body></html>", encoding="utf-8"
    )


def _installed(tool_env) -> str:
    """The package's version in a uv tool environment, read from its own metadata."""
    from importlib.metadata import PathDistribution
    from pathlib import Path

    from packaging.utils import canonicalize_name

    from pepper_cli import _version

    # POSIX nests it under lib/python3.x, Windows keeps it at Lib: find it rather than assume.
    candidates = [
        *(tool_env / "lib").glob("python*/site-packages"),
        *(tool_env / "Lib").glob("site-packages"),
    ]
    assert candidates, f"no site-packages under {tool_env}"
    site_packages = candidates[0]
    matches = {
        dist.version
        for dist in map(PathDistribution, Path(site_packages).glob("*.dist-info"))
        if canonicalize_name(dist.metadata["Name"] or "") == _version.DISTRIBUTION
    }
    assert len(matches) == 1, f"expected one {_version.DISTRIBUTION}, found {sorted(matches)}"
    return matches.pop()


def _uv_receipt(monkeypatch: pytest.MonkeyPatch, tmp_path, requirement: str) -> None:
    prefix = tmp_path / "uv-tool-env"
    shutil.rmtree(prefix, ignore_errors=True)
    prefix.mkdir(parents=True)
    # The key is uv's own (`uv-tool/src/tool.rs`), as a real install writes it.
    (prefix / "uv-receipt.toml").write_text(
        f'[tool]\nrequirements = [{{ name = "vycari-pepper", specifier = "{requirement}" }}]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("sys.prefix", str(prefix))


@pytest.mark.parametrize(
    ("requirement", "expected"),
    [
        ("", None),
        ("==0.3.0", "vycari-pepper==0.3.0"),
        ("<0.4", "vycari-pepper<0.4"),
        (">=0.3", None),
        ("!=0.2.0", None),
        (">=0.3.0,<0.8", None),
        (">=0.3.0,<0.7", "vycari-pepper>=0.3.0,<0.7"),
        ("!=0.2.0,<0.7", "vycari-pepper!=0.2.0,<0.7"),
    ],
)
def test_a_pin_that_excludes_the_latest_release_suppresses_the_offer(
    monkeypatch, tmp_path, requirement, expected
) -> None:
    """A pinned uv tool install keeps its pin on ``uv tool upgrade``, so offering the release
    the pin excludes only shows again on the next check (#127)."""
    from pepper_cli import _version

    _uv_receipt(monkeypatch, tmp_path, requirement)
    assert _version.pinned_below("0.7.0") == expected


def test_reading_the_receipt_without_one_is_not_an_error(monkeypatch, tmp_path) -> None:
    from pepper_cli import _version

    monkeypatch.setattr("sys.prefix", str(tmp_path / "no-such-env"))
    assert _version.uv_tool_requirement() is None
    assert _version.pinned_below("0.7.0") is None


def test_a_receipt_that_is_not_utf8_reads_as_no_receipt(monkeypatch, tmp_path) -> None:
    """``tomllib`` decodes the file, and ``UnicodeDecodeError`` is neither an ``OSError``
    nor a ``TOMLDecodeError``: a damaged receipt must fall back, not break the check."""
    from pepper_cli import _version

    prefix = tmp_path / "damaged-env"
    prefix.mkdir()
    (prefix / "uv-receipt.toml").write_bytes(b"[tool]\nrequirements = [{ name = '\xff\xfe' }]\n")
    monkeypatch.setattr("sys.prefix", str(prefix))
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(tui, "detect_install_method", lambda: "uv-tool")
    assert _version.uv_tool_requirement() is None
    assert _version.pinned_below("0.7.0") is None
    assert tui.upgrade_command() == ["uv", "tool", "upgrade", "vycari-pepper"]
    assert tui.upgrade_prompt("0.7.0")[0] == "Upgrade Pepper to v0.7.0?"


def test_the_upgrade_command_drops_a_pin_instead_of_keeping_it(monkeypatch, tmp_path) -> None:
    _uv_receipt(monkeypatch, tmp_path, "==0.3.0")
    assert tui.upgrade_command() == ["uv", "tool", "install", "--force", "vycari-pepper"]

    _uv_receipt(monkeypatch, tmp_path / "unpinned", ">=0.3")
    assert tui.upgrade_command() == ["uv", "tool", "upgrade", "vycari-pepper"]


def test_an_exclusion_is_not_a_pin_so_the_upgrade_keeps_it(monkeypatch, tmp_path) -> None:
    """``!=0.2.0`` excludes a release, not the upgrade: ``uv tool upgrade`` keeps it and still
    moves up, so dropping it would take away a constraint the user never had to lose."""
    monkeypatch.setattr("sys.platform", "linux")
    _uv_receipt(monkeypatch, tmp_path, "!=0.2.0")
    assert tui.upgrade_command() == ["uv", "tool", "upgrade", "vycari-pepper"]
    assert tui.upgrade_command("0.7.0") == ["uv", "tool", "upgrade", "vycari-pepper"]
    question, _ = tui.upgrade_prompt("0.7.0")
    assert question == "Upgrade Pepper to v0.7.0?"
    assert "removing the pin" not in question


@pytest.mark.parametrize(
    ("requirement", "target", "expect_pinned"),
    [
        # The exclusion covers the release the server demands: kept, the upgrade stays below.
        ("!=0.7.0", "0.7.0", True),
        (">=0.3,!=0.7.0", "0.7.0", True),
        ("!=0.7.0,!=0.6.0", "0.7.0", True),
        # It covers another release, or no target is known: ``uv tool upgrade`` moves up.
        ("!=0.7.0", "0.8.0", False),
        ("!=0.2.0", "0.7.0", False),
        ("!=0.7.0", None, False),
        (">=0.3", "0.7.0", False),
    ],
)
def test_an_exclusion_fitting_the_target_is_treated_as_a_pin(
    requirement, target, expect_pinned
) -> None:
    from pepper_cli import _version

    assert _version.is_pinned(f"vycari-pepper{requirement}", target) is expect_pinned


def test_an_exclusion_on_the_required_release_is_removed_for_a_426(monkeypatch) -> None:
    """Real uv, after ``uv tool install vycari-pepper!=0.4.0`` on 0.3.0, answers
    ``uv tool upgrade`` with "Nothing to upgrade" and exit 0: kept, the exclusion would report
    a success that never happened (VR-33db9e57)."""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(tui, "detect_install_method", lambda: "uv-tool")
    monkeypatch.setattr("pepper_cli._version.uv_tool_requirement", lambda: "vycari-pepper!=0.4.0")
    assert tui.upgrade_command("0.4.0") == ["uv", "tool", "install", "--force", "vycari-pepper"]
    question, detail = tui.upgrade_prompt(real_426().message, offer=False, target_version="0.4.0")
    assert question == "Upgrade to v0.4.0, removing the pin vycari-pepper!=0.4.0?"
    assert "uv tool install --force vycari-pepper" in detail


def test_perform_upgrade_fits_its_command_to_the_required_release(monkeypatch, capsys) -> None:
    """The command is built for the release the server named, not just the exit message."""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(tui, "detect_install_method", lambda: "uv-tool")
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/uv")
    monkeypatch.setattr("pepper_cli._version.uv_tool_requirement", lambda: "vycari-pepper!=0.4.0")
    ran = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, check=False: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )
    assert tui._perform_upgrade(tui_app.UNKNOWN_RELEASE, "0.4.0") == 0
    assert ran == [["/usr/bin/uv", "tool", "install", "--force", "vycari-pepper"]]
    assert "Upgrading vycari-pepper to v0.4.0..." in capsys.readouterr().err


def test_a_426_exits_to_upgrade_without_printing_the_error_as_a_version(
    monkeypatch, capsys
) -> None:
    """#127: the 426 sentence opens the flow but is not a release, so the upgrade runs and
    says what it does without inventing a version to print."""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(tui, "detect_install_method", lambda: "uv-tool")
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/uv")
    ran = []

    def fake_run(cmd, check=False):
        ran.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert tui._perform_upgrade(tui_app.UNKNOWN_RELEASE) == 0
    assert ran == [["/usr/bin/uv", "tool", "upgrade", "vycari-pepper"]]
    shown = capsys.readouterr().err
    assert "Upgrading vycari-pepper to the latest release..." in shown
    assert "vPepper" not in shown


async def test_a_pinned_install_is_not_offered_the_release_it_cannot_take(
    monkeypatch, tmp_path
) -> None:
    import threading

    server = ts.standard_server()
    _uv_receipt(monkeypatch, tmp_path, "==0.3.0")
    checked = threading.Event()
    app = tui_app.PepperApp(
        server.client(), live=False, update_checker=lambda: checked.set() or "0.7.0"
    )
    async with app.run_test(size=SIZE) as pilot:
        assert await asyncio.to_thread(checked.wait, 2)
        await pilot.pause()
        assert app.update_version is None
        assert pilot.app.screen.active_bindings.get("f10") is None


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
    monkeypatch.setattr(
        tui,
        "_perform_upgrade",
        lambda ver, required=None, **_kw: called.append((ver, required)) or 0,
    )

    rc = tui.run(server_url=creds.server_url, token=creds.token)
    assert rc == 0
    assert called == [("0.4.0", None)]


def test_a_426_keeps_its_release_for_the_command_it_runs(monkeypatch, tmp_path, capsys) -> None:
    """The prompt names the release; the command run afterwards must be built for it, not for
    an unknown target, or an exclusion covering it survives (Greptile on #218)."""
    _uv_receipt(monkeypatch, tmp_path, "!=0.4.0")
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(tui, "detect_install_method", lambda: "uv-tool")
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/uv")
    ran = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, check=False: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )

    app = tui_app.PepperApp.__new__(tui_app.PepperApp)
    app.update_version = None
    app._upgrade_prompt = None
    app._upgrade_target = None
    app._upgrade_is_426 = False
    app.fatal = None
    app.fail_fatal = lambda message: None
    app._upgrade_required(real_426())
    assert app.update_version == tui_app.UNKNOWN_RELEASE
    assert app._upgrade_target == "0.4.0"
    assert app._upgrade_prompt[0] == "Upgrade to v0.4.0, removing the pin vycari-pepper!=0.4.0?"

    assert tui._perform_upgrade(app.update_version, app._upgrade_target, upgrade_required=True) == 0
    assert ran == [["/usr/bin/uv", "tool", "install", "--force", "vycari-pepper"]]
    assert "Upgrading vycari-pepper to v0.4.0..." in capsys.readouterr().err


async def test_an_offer_that_lands_after_a_426_does_not_replace_it(monkeypatch, tmp_path) -> None:
    """The background check can deliver after the 426 has gone fatal; that offer must not
    clear the required release F10 is about to run (Greptile on #218)."""
    _uv_receipt(monkeypatch, tmp_path, "!=0.3.5")
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client(), update_checker=lambda: None)
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: not app._loops["rail"].busy)

        def refused() -> None:
            raise real_426()

        monkeypatch.setattr(app._rail, "step", refused)
        app._run("rail")
        await until(pilot, lambda: app.fatal is not None)
        assert app._upgrade_target == "0.4.0"

        app._on_update_available("0.3.5")
        await pilot.pause()
        assert app.update_version == tui_app.UNKNOWN_RELEASE
        assert app._upgrade_target == "0.4.0"


def test_a_background_offer_carries_no_target(monkeypatch, tmp_path) -> None:
    _uv_receipt(monkeypatch, tmp_path, "!=0.4.0")
    app = tui_app.PepperApp.__new__(tui_app.PepperApp)
    app.update_version = None
    app._upgrade_target = "0.4.0"
    app._upgrade_prompt = None
    app._quitting = False
    app.fatal = None
    app._mounted = lambda: True
    for name in ("refresh_bindings", "refresh_chrome"):
        setattr(app, name, lambda: None)
    app._on_update_available("0.4.0")
    assert app._upgrade_target is None
    assert app.update_version == "0.4.0"


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


@pytest.mark.parametrize(
    "closing", [RuntimeError, concurrent.futures.CancelledError], ids=["loop-gone", "cancelled"]
)
def test_a_post_racing_the_loop_closing_is_dropped(monkeypatch, closing) -> None:
    # Not quitting yet, so _post reaches call_from_thread; the loop closes under it.
    app = tui_app.PepperApp(ts.standard_server().client(), live=False)

    def call_from_thread(*_args):
        raise closing

    monkeypatch.setattr(app, "call_from_thread", call_from_thread)
    assert not app._quitting
    app._post(lambda: None)


@pytest.mark.parametrize(
    ("requirement", "minimum", "expect_pinned"),
    [
        ("==0.3.0", "0.4.0", True),
        ("!=0.4.0", "0.4.0", True),
        ("!=0.4.*", "0.4.0", True),
        ("!=0.4.0rc1", "0.4.0rc1", True),
        ("<0.4", "0.4.0", True),
        (">=0.3", "0.4.0", False),
        (">=0.3,!=0.3.5", "0.4.0", False),
        # No minimum on the 426 path: any recorded specifier forces the re-install.
        ("==0.3.0", None, True),
        ("!=0.4.0", None, True),
        (">=0.3", None, True),
    ],
)
def test_a_426_pin_decision_uses_the_server_minimum(requirement, minimum, expect_pinned) -> None:
    from pepper_cli import _version

    assert (
        _version.is_pinned(
            f"vycari-pepper{requirement}",
            real_426(minimum_version=minimum).minimum_version,
            upgrade_required=True,
        )
        is expect_pinned
    )


def test_the_background_offer_keeps_a_floor_unpinned_with_no_target() -> None:
    from pepper_cli import _version

    assert _version.is_pinned("vycari-pepper>=0.3") is False
    assert _version.is_pinned("vycari-pepper!=0.4.0") is False
    assert _version.is_pinned("vycari-pepper") is False
    assert _version.is_pinned("vycari-pepper", upgrade_required=True) is False


def test_a_426_prints_the_minimum_not_the_clients_own_version(
    monkeypatch, tmp_path, capsys
) -> None:
    _uv_receipt(monkeypatch, tmp_path, "!=0.4.0")
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(tui, "detect_install_method", lambda: "uv-tool")
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/uv")
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, check=False: subprocess.CompletedProcess(cmd, 0)
    )
    exc = real_426()
    assert (
        tui._perform_upgrade(tui_app.UNKNOWN_RELEASE, exc.minimum_version, upgrade_required=True)
        == 0
    )
    err = capsys.readouterr().err
    assert "Upgrading vycari-pepper to v0.4.0..." in err
    assert "v0.3.0" not in err


@pytest.mark.parametrize("drop", ["minimum_version"])
def test_a_426_without_a_minimum_forces_the_reinstall_and_names_no_release(
    monkeypatch, tmp_path, capsys, drop
) -> None:
    _uv_receipt(monkeypatch, tmp_path, "!=0.4.0")
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(tui, "detect_install_method", lambda: "uv-tool")
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/uv")
    ran = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, check=False: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )
    exc = real_426(**{drop: None})
    assert exc.minimum_version is None
    question, _ = tui.upgrade_prompt(exc.message, offer=False, target_version=exc.minimum_version)
    assert question == "Upgrade to the latest release, removing the pin vycari-pepper!=0.4.0?"
    assert tui._perform_upgrade(tui_app.UNKNOWN_RELEASE, None, upgrade_required=True) == 0
    assert ran == [["/usr/bin/uv", "tool", "install", "--force", "vycari-pepper"]]
    assert "to the latest release" in capsys.readouterr().err


def test_a_non_json_426_forces_the_reinstall(monkeypatch, tmp_path) -> None:
    from pepper_cli import api_client

    _uv_receipt(monkeypatch, tmp_path, "==0.3.0")
    try:
        api_client._raise_for_error(httpx.Response(426, text="Please upgrade to 0.4.0."))
    except UpgradeRequiredError as exc:
        assert exc.minimum_version is None
        assert tui.upgrade_command(exc.minimum_version, upgrade_required=True)[:3] == [
            "uv",
            "tool",
            "install",
        ]


def test_a_reinstall_keeps_the_receipts_with_packages(monkeypatch, tmp_path) -> None:
    prefix = tmp_path / "env"
    prefix.mkdir()
    (prefix / "uv-receipt.toml").write_text(
        "[tool]\nrequirements = [\n"
        '  { name = "vycari-pepper", specifier = "==0.3.0" },\n'
        '  { name = "rich", specifier = ">=13" },\n'
        '  { name = "local", directory = "/x" },\n'
        "]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("sys.prefix", str(prefix))
    assert tui.upgrade_command("0.4.0") == [
        "uv",
        "tool",
        "install",
        "--force",
        "--with",
        "rich>=13",
        "vycari-pepper",
    ]


async def test_a_426_does_not_replace_an_earlier_fatal_state(monkeypatch, tmp_path) -> None:
    _uv_receipt(monkeypatch, tmp_path, "==0.3.0")
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client(), update_checker=lambda: None)
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: not app._loops["rail"].busy)
        app.fail_fatal(tui_app.LOGIN_HINT)
        app._upgrade_required(real_426())
        assert app.fatal == tui_app.LOGIN_HINT
        assert app._upgrade_target is None
