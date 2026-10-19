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


def _perform_upgrade(target_version: str) -> int:
    """Run `uv tool upgrade vycari-pepper` after TUI exits cleanly."""
    import shutil
    import subprocess

    from pepper_cli import render

    uv_bin = shutil.which("uv")
    if not uv_bin:
        render.error(
            "Error: 'uv' executable not found in PATH.\n"
            "Please run manually: uv tool upgrade vycari-pepper"
        )
        return 1

    render.info(f"Upgrading vycari-pepper to v{target_version}...")
    cache_dir = os.path.join(os.path.expanduser("~"), ".uv-upgrade-cache")
    proc = subprocess.run(  # noqa: S603
        [uv_bin, "tool", "upgrade", "--cache-dir", cache_dir, "vycari-pepper"],
        check=False,
    )
    if proc.returncode == 0:
        render.info(f"Successfully upgraded to v{target_version}. Run 'pepper' to start.")
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
        if getattr(app, "upgrade_requested", False) and target_version is not None:
            return _perform_upgrade(target_version)
        return app.return_code or 0
