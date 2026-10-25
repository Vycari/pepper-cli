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


def _perform_upgrade(target_version: str) -> int:
    """Run upgrade command or print instructions based on the install method."""
    import shutil
    import subprocess
    import sys

    from pepper_cli import render

    method = detect_install_method()
    if method == "pipx":
        render.info(f"To upgrade to v{target_version}, run:\n  pipx upgrade vycari-pepper")
        return 0
    if method == "pip":
        render.info(f"To upgrade to v{target_version}, run:\n  pip install --upgrade vycari-pepper")
        return 0

    if sys.platform == "win32":
        render.info(f"To upgrade to v{target_version}, run:\n  uv tool upgrade vycari-pepper")
        return 0

    uv_bin = shutil.which("uv")
    if not uv_bin:
        render.error(
            "Error: 'uv' executable not found in PATH.\n"
            "Please run manually: uv tool upgrade vycari-pepper"
        )
        return 1

    render.info(f"Upgrading vycari-pepper to v{target_version}...")
    proc = subprocess.run(  # noqa: S603
        [uv_bin, "tool", "upgrade", "vycari-pepper"],
        check=False,
    )
    if proc.returncode == 0:
        render.info("Upgrade command completed. Run 'pepper' to start.")
    else:
        render.error("Upgrade failed. Run manually: uv tool upgrade vycari-pepper")
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
        target_version = getattr(app, "update_version", None)
        upgrade_requested = getattr(app, "upgrade_requested", False)
        ret_code = app.return_code or 0

    if upgrade_requested and target_version is not None:
        return _perform_upgrade(target_version)
    return ret_code
