"""``pepper tui``: the full-screen terminal client (``planning/terminal-ui.md``).

Importing this package is cheap on purpose. Textual is imported only inside :func:`run`, so
every other ``pepper`` command pays nothing for it (a test pins that).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from pepper_cli.api_client import ApiClient

if TYPE_CHECKING:
    from collections.abc import Callable

    from pepper_cli.provider.fs import FilesystemProvider
    from pepper_cli.secret import SecretStr


def bell_enabled() -> bool:
    """``PEPPER_TUI_BELL``: ring the terminal bell on a new mention (§15 C, opt-in only)."""
    return os.environ.get("PEPPER_TUI_BELL", "").strip().lower() not in ("", "0", "false", "no")


def detect_install_method() -> str:
    """Return 'uv-tool', 'pipx', or 'pip' based on sys.prefix and environment."""
    import sys
    from pathlib import Path

    if (Path(sys.prefix) / "uv-receipt.toml").is_file():
        return "uv-tool"
    parts = [p.lower() for p in Path(sys.prefix).parts]
    if "pipx" in parts:
        return "pipx"
    return "pip"


def upgrade_command(
    target_version: str | None = None, *, upgrade_required: bool = False
) -> list[str]:
    """The uv command that actually moves this install to the release it needs.

    A pinned uv tool install (``uv tool install vycari-pepper==0.3.0``) keeps its pin on
    ``uv tool upgrade``, so it gets re-installed unpinned instead (#127). ``--force`` is what
    makes that a re-install: installed at 0.3.0, plain ``install`` writes the new receipt and
    leaves 0.3.0 in place, because it already satisfies the requirement it just wrote.
    ``--force`` re-installs the package too, which the receipt alone would not show, so the
    real-uv test asserts the version in the environment's metadata, not just the receipt.

    ``target_version`` is the required release when the prompt names one: an exclusion that
    covers it (``!=0.4`` against 0.4) keeps ``uv tool upgrade`` below it, so it is a pin too.
    ``upgrade_required`` (a 426) treats any recorded specifier as a pin when no target is known.
    The receipt's ``--with`` packages are passed again, since ``--force`` rewrites the receipt.
    """
    from pepper_cli import _version

    requirement = _version.uv_tool_requirement()
    if requirement is not None and _version.is_pinned(
        requirement, target_version, upgrade_required=upgrade_required
    ):
        withs = [arg for w in _version.uv_tool_with_requirements() for arg in ("--with", w)]
        return ["uv", "tool", "install", "--force", *withs, _version.DISTRIBUTION]
    return ["uv", "tool", "upgrade", _version.DISTRIBUTION]


def manual_command(command: list[str], *, windows: bool | None = None) -> str:
    """``command`` as one line a person can paste into their shell.

    A plain ``" ".join`` breaks once an argument carries shell syntax: ``rich>=13`` is a
    redirect, and a ``--with`` marker (``colorama; sys_platform == 'win32'``) ends the command
    at the semicolon. POSIX gets :func:`shlex.join`. Windows gets double quotes, which both
    PowerShell and cmd read as one argument; an argument with ``$``, a backtick or ``"`` (none
    of which a requirement normally holds) gets PowerShell single quotes instead. The list
    handed to ``subprocess.run`` is never quoted.
    """
    import re
    import shlex
    import sys

    if windows is None:
        windows = sys.platform == "win32"
    if not windows:
        return shlex.join(command)

    def quote(arg: str) -> str:
        if arg and re.fullmatch(r"[A-Za-z0-9_\-./:\\=]+", arg):
            return arg
        if re.search(r'[$`"]', arg):
            return "'" + arg.replace("'", "''") + "'"
        return f'"{arg}"'

    return " ".join(quote(arg) for arg in command)


def upgrade_prompt(
    message: str, *, offer: bool = True, target_version: str | None = None
) -> tuple[str, str]:
    """The ``F10`` question and detail for this install.

    ``offer`` is the background offer, which names the target release in ``message``; the
    server's upgrade-required text is not a version, so the question leaves it out. Either
    way a pinned install is told its constraint goes: the command it runs drops the pin, and
    that must not happen silently (#127). ``target_version`` is the release the server named
    on a 426, so an exclusion that covers it is called a pin, as ``upgrade_command`` does.
    """
    import sys

    from pepper_cli import _version

    requirement = _version.uv_tool_requirement()
    pinned = requirement is not None and _version.is_pinned(
        requirement, target_version, upgrade_required=not offer
    )
    subject = (
        f"v{message}"
        if offer
        else (f"v{target_version}" if target_version else "the latest release")
    )
    if pinned:
        question = f"Upgrade to {subject}, removing the pin {requirement}?"
    else:
        question = f"Upgrade Pepper to {subject}?"

    method = detect_install_method()
    if method == "uv-tool" and sys.platform == "win32":
        manual = manual_command(upgrade_command(target_version, upgrade_required=not offer))
        detail = f"Pepper will exit. Run '{manual}' in your terminal."
    elif method == "uv-tool" and pinned:
        manual = manual_command(upgrade_command(target_version, upgrade_required=not offer))
        detail = f"Pepper will exit and run '{manual}' in your terminal."
    elif method == "uv-tool":
        detail = "Pepper will exit and run 'uv tool upgrade vycari-pepper' in your terminal."
    elif method == "pipx":
        detail = "Pepper will exit. Run 'pipx upgrade vycari-pepper' in your terminal."
    else:
        detail = "Pepper will exit. Run 'pip install --upgrade vycari-pepper' in your terminal."
    if pinned:
        detail += " The pin is removed, so later releases install too."
    return question, detail


def _perform_upgrade(
    target_version: str, required: str | None = None, *, upgrade_required: bool = False
) -> int:
    """Run upgrade command or print instructions based on the install method.

    After a 426 ``target_version`` is the ``UNKNOWN_RELEASE`` marker unless the server named
    a release, which arrives in ``required``: the command must fit that release, and only a
    real one is printed as ``v{version}``.
    """
    import shutil
    import subprocess
    import sys

    from pepper_cli import render

    to = f"v{required}" if required is not None else "the latest release"
    method = detect_install_method()
    if method == "pipx":
        render.info(f"To upgrade to {to}, run:\n  pipx upgrade vycari-pepper")
        return 0
    if method == "pip":
        render.info(f"To upgrade to {to}, run:\n  pip install --upgrade vycari-pepper")
        return 0

    command = upgrade_command(required, upgrade_required=upgrade_required)
    manual = manual_command(command)
    if sys.platform == "win32":
        render.info(f"To upgrade to {to}, run:\n  {manual}", soft_wrap=True)
        return 0

    uv_bin = shutil.which("uv")
    if not uv_bin:
        render.error(f"Error: 'uv' executable not found in PATH.\nPlease run manually: {manual}")
        return 1

    render.info(f"Upgrading vycari-pepper to {to}...")
    proc = subprocess.run([uv_bin, *command[1:]], check=False)  # noqa: S603
    if proc.returncode == 0:
        render.info("Upgrade command completed. Run 'pepper' to start.")
    else:
        render.error(f"Upgrade failed. Run manually: {manual}")
    return proc.returncode


def run(
    *,
    server_url: str,
    token: SecretStr,
    provider: FilesystemProvider | None = None,
    update_checker: Callable[[], str | None] | None = None,
) -> int:
    """Run the app until the user quits; the process exit code (0 normal, 1 error).

    ``provider`` is the ``--root`` toolset Pepper's DM turns may call; ``None`` shares nothing.
    """
    from pepper_cli.tui.app import PepperApp  # Textual is imported here, not at module load

    with ApiClient(server_url=server_url, token=token) as api:
        app = PepperApp(api, provider=provider, bell=bell_enabled(), update_checker=update_checker)
        app.run()
        update_version = getattr(app, "update_version", None)
        upgrade_requested = getattr(app, "upgrade_requested", False)
        upgrade_target = getattr(app, "_upgrade_target", None)
        upgrade_required = getattr(app, "_upgrade_is_426", False)
        ret_code = app.return_code or 0

    if upgrade_requested and update_version is not None:
        return _perform_upgrade(update_version, upgrade_target, upgrade_required=upgrade_required)
    return ret_code
