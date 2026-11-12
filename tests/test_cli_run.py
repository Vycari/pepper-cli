"""``pepper run FILE [ARGS...]``: a prompt file as one one-shot turn."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Self

import pytest

from pepper_cli import api_client, cli, credentials
from pepper_cli.secret import SecretStr


class _FakeApi:
    instances: list[_FakeApi] = []

    def __init__(self, **_: object) -> None:
        self.messages: list[str] = []
        self.client_tools: list[list[dict] | None] = []
        _FakeApi.instances.append(self)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def chat_send_stream(
        self, message: str, *, client_tools: list[dict] | None = None, **_: object
    ) -> Iterator[api_client.StreamEvent]:
        self.messages.append(message)
        self.client_tools.append(client_tools)
        yield api_client.StreamEvent(kind="model_thought", payload={"text": "private"})
        yield api_client.StreamEvent(kind="reply", payload={"reply": "done"})


@pytest.fixture
def api(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> type[_FakeApi]:
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    _FakeApi.instances.clear()

    def factory(**kwargs: Any) -> _FakeApi:
        return _FakeApi(**kwargs)

    monkeypatch.setattr(api_client, "ApiClient", factory)
    return _FakeApi


def _script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "digest.prompt"
    path.write_text(body, encoding="utf-8")
    return path


def test_run_sends_the_file_without_its_shebang_and_prints_the_reply(api, tmp_path, capsys) -> None:
    path = _script(tmp_path, "#!/usr/bin/env -S pepper run\nSummarize {{1}}.\n")
    assert cli.main(["run", str(path), "notes.md", "extra"]) == 0
    assert api.instances[-1].messages == ["Summarize notes.md.\n\nArguments:\n- extra"]
    assert api.instances[-1].client_tools == [None]
    assert capsys.readouterr().out.strip() == "done"


def test_run_reads_the_prompt_from_stdin(api, monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("Hello {{1}}"))
    assert cli.main(["run", "-", "there"]) == 0
    assert api.instances[-1].messages == ["Hello there"]


def test_dash_dash_passes_arguments_that_look_like_flags(api, tmp_path) -> None:
    path = _script(tmp_path, "Echo {{1}}")
    assert cli.main(["run", str(path), "--", "--root"]) == 0
    assert api.instances[-1].messages == ["Echo --root"]


def test_root_after_the_arguments_grants_local_read_tools(api, tmp_path) -> None:
    path = _script(tmp_path, "Look in my notes")
    notes = tmp_path / "notes"
    notes.mkdir()
    assert cli.main(["run", str(path), "a", "--root", str(notes)]) == 0
    tools = api.instances[-1].client_tools[0]
    assert tools is not None
    assert {t["name"] for t in tools} >= {"fs_read"}


def test_a_missing_argument_exits_2_before_any_network_call(api, tmp_path, capsys) -> None:
    path = _script(tmp_path, "Compare {{1}} and {{2}}")
    assert cli.main(["run", str(path), "only-one"]) == 2
    assert api.instances == []
    assert "{{2}} but was given 1 argument" in capsys.readouterr().err


def test_an_empty_file_exits_2(api, tmp_path, capsys) -> None:
    path = _script(tmp_path, "#!/usr/bin/env -S pepper run\n")
    assert cli.main(["run", str(path)]) == 2
    assert api.instances == []
    assert "empty" in capsys.readouterr().err


def test_an_unreadable_file_exits_2(api, tmp_path, capsys) -> None:
    assert cli.main(["run", str(tmp_path / "nope.prompt")]) == 2
    assert api.instances == []
    assert "couldn't read" in capsys.readouterr().err


def test_a_bad_root_exits_2(api, tmp_path, capsys) -> None:
    path = _script(tmp_path, "hi")
    assert cli.main(["run", str(path), "--root", str(tmp_path / "missing")]) == 2
    assert api.instances == []
    assert "--root" in capsys.readouterr().err


def test_not_logged_in_exits_1(tmp_home, tmp_path, capsys) -> None:
    path = _script(tmp_path, "hi")
    assert cli.main(["run", str(path)]) == 1
    assert "pepper login" in capsys.readouterr().err


def test_json_emits_one_line_per_event_with_the_reply_last(api, tmp_path, capsys) -> None:
    path = _script(tmp_path, "hi")
    assert cli.main(["run", str(path), "--json"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    assert lines[-1]["kind"] == "reply"


def test_run_is_not_implicit_chat() -> None:
    _, names = cli._build_parser()
    assert "run" in names


@pytest.mark.skipif(sys.platform == "win32", reason="shebang scripts are a Unix feature")
def test_an_executable_prompt_file_runs_through_its_shebang(tmp_path: Path) -> None:
    """The kernel hands ``pepper run`` the script's path then its arguments.

    Without a login the run stops at credentials, which proves the file reached
    ``pepper run`` through the shebang with its arguments parsed: a missing
    ``{{2}}`` would have exited 2 first.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    pepper = bindir / "pepper"
    pepper.write_text(
        f"#!{sys.executable}\nfrom pepper_cli.cli import main\nraise SystemExit(main())\n"
    )
    pepper.chmod(0o755)
    script = tmp_path / "hello.prompt"
    script.write_text("#!/usr/bin/env -S pepper run\nSay hi to {{1}} and {{2}}\n")
    script.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
    }
    missing = subprocess.run([str(script), "Allen"], env=env, capture_output=True, text=True)
    assert missing.returncode == 2, missing.stderr
    assert "{{2}}" in missing.stderr
    full = subprocess.run([str(script), "Allen", "Chris"], env=env, capture_output=True, text=True)
    assert full.returncode == 1, full.stderr
    assert "pepper login" in full.stderr


@pytest.mark.parametrize(
    "tail",
    [
        ["a", "--json", "b"],
        ["--json", "a", "b"],
        ["a", "b", "--json"],
    ],
)
def test_arguments_may_sit_on_either_side_of_options(api, tmp_path, capsys, tail) -> None:
    path = _script(tmp_path, "{{1}} then {{2}}")
    assert cli.main(["run", str(path), *tail]) == 0
    assert api.instances[-1].messages == ["a then b"]
    capsys.readouterr()


def test_arguments_split_around_root(api, tmp_path) -> None:
    path = _script(tmp_path, "{{1}} then {{2}}")
    notes = tmp_path / "notes"
    notes.mkdir()
    assert cli.main(["run", str(path), "a", "--root", str(notes), "b"]) == 0
    assert api.instances[-1].messages == ["a then b"]
    assert api.instances[-1].client_tools[0] is not None


def test_run_help_still_lists_its_options(capsys) -> None:
    with pytest.raises(SystemExit):
        cli.main(["run", "--help"])
    out = capsys.readouterr().out
    assert "--root" in out
    assert "--json" in out
