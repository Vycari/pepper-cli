"""``pepper tui``: the full-screen terminal client (``planning/terminal-ui.md``).

Importing this package is cheap on purpose. Textual is imported only inside :func:`run`, so
every other ``pepper`` command pays nothing for it (a test pins that).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pepper_cli.api_client import ApiClient

if TYPE_CHECKING:
    from pepper_cli.provider.fs import FilesystemProvider
    from pepper_cli.secret import SecretStr


def run(*, server_url: str, token: SecretStr, provider: FilesystemProvider | None = None) -> int:
    """Run the app until the user quits; the process exit code (0 normal, 1 error).

    ``provider`` is the ``--root`` toolset Pepper's DM turns may call; ``None`` shares nothing.
    """
    from pepper_cli.tui.app import PepperApp  # Textual is imported here, not at module load

    with ApiClient(server_url=server_url, token=token) as api:
        app = PepperApp(api, provider=provider)
        app.run()
        return app.return_code or 0
