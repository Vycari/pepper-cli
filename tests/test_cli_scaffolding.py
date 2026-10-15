"""Unit tests for the shared CLI scaffolding (``planning/cli-information-architecture.md``).

These helpers are built in phase 1 and adopted leaf by leaf in the later phases, so
nothing in ``cli.py`` calls most of them yet; these tests are what keep them honest.
"""

from __future__ import annotations

import argparse
import dataclasses
import json

import pytest

from pepper_cli import api_client, cli, credentials
from pepper_cli.secret import SecretStr


def _ns(**kw: object) -> argparse.Namespace:
    return argparse.Namespace(**kw)


# -- J10: --json / PEPPER_JSON -------------------------------------------------


def test_json_mode_off_by_default() -> None:
    assert cli._json_mode(_ns(json=False)) is False
    assert cli._json_mode(_ns()) is False  # a leaf without the flag


def test_json_mode_flag_wins(monkeypatch) -> None:
    monkeypatch.setenv("PEPPER_JSON", "0")
    assert cli._json_mode(_ns(json=True)) is True


def test_json_mode_env_exactly_one(monkeypatch) -> None:
    monkeypatch.setenv("PEPPER_JSON", "1")
    assert cli._json_mode(_ns(json=False)) is True
    for value in ("0", "", "true", "yes", " 1", "1 ", "11"):
        monkeypatch.setenv("PEPPER_JSON", value)
        assert cli._json_mode(_ns(json=False)) is False, value


def test_main_resolves_json_mode_once(monkeypatch) -> None:
    seen: list[object] = []

    def handler(args: argparse.Namespace) -> int:
        seen.append(args.json)
        return 0

    parser = argparse.ArgumentParser()
    leaf = parser.add_subparsers().add_parser("x")
    leaf.add_argument("--json", action="store_true")
    leaf.set_defaults(handler=handler)
    monkeypatch.setattr(cli, "_build_parser", lambda: (parser, frozenset({"x"})))
    monkeypatch.setenv("PEPPER_JSON", "1")
    assert cli.main(["x"]) == 0
    monkeypatch.setenv("PEPPER_JSON", "0")
    assert cli.main(["x"]) == 0
    assert seen == [True, False]


# -- shared flag parents -------------------------------------------------------


def _parser(*parents: argparse.ArgumentParser) -> argparse.ArgumentParser:
    return argparse.ArgumentParser(prog="t", parents=list(parents), exit_on_error=False)


def test_json_parent() -> None:
    p = _parser(cli._json_parent)
    assert p.parse_args([]).json is False
    assert p.parse_args(["--json"]).json is True


def test_room_parent_optional_and_required() -> None:
    optional = _parser(cli._room_parent())
    assert optional.parse_args([]).room is None
    assert optional.parse_args(["--room", "ski"]).room == "ski"
    required = argparse.ArgumentParser(
        prog="t", parents=[cli._room_parent(required=True)], exit_on_error=False
    )
    assert required.parse_args(["--room", "ski"]).room == "ski"
    with pytest.raises(argparse.ArgumentError):
        required.parse_args([])


def test_topic_yes_and_page_parents() -> None:
    p = _parser(cli._topic_parent, cli._yes_parent, cli._page_parent)
    empty = p.parse_args([])
    assert (empty.topic, empty.yes, empty.limit, empty.before, empty.since) == (
        None,
        False,
        None,
        None,
        None,
    )
    full = p.parse_args(["--topic", "t", "--yes", "--limit", "5", "--before", "b", "--since", "s"])
    assert (full.topic, full.yes, full.limit, full.before, full.since) == ("t", True, 5, "b", "s")
    with pytest.raises(argparse.ArgumentError):
        p.parse_args(["--limit", "many"])


# -- emit helpers --------------------------------------------------------------


@dataclasses.dataclass
class _Row:
    id: str


def test_emit_json_and_human(capsys, monkeypatch) -> None:
    ran: list[bool] = []
    cli._emit({"a": 1}, _ns(json=True), lambda: ran.append(True))
    assert json.loads(capsys.readouterr().out) == {"a": 1}
    assert ran == []
    cli._emit({"a": 1}, _ns(json=False), lambda: ran.append(True))
    assert ran == [True]
    assert capsys.readouterr().out == ""
    monkeypatch.setenv("PEPPER_JSON", "1")
    cli._emit({"a": 2}, _ns(json=False), lambda: ran.append(True))
    assert json.loads(capsys.readouterr().out) == {"a": 2}
    assert ran == [True]


def test_list_envelope_shape() -> None:
    assert cli._list_envelope([]) == {"items": [], "next_before": None}
    env = cli._list_envelope([_Row("x"), {"id": "y"}], "cur", truncated=True)
    assert env == {
        "items": [{"id": "x"}, {"id": "y"}],
        "next_before": "cur",
        "truncated": True,
    }


def test_emit_list(capsys) -> None:
    ran: list[bool] = []
    cli._emit_list([_Row("x")], _ns(json=True), lambda: ran.append(True), "n", total=1)
    assert json.loads(capsys.readouterr().out) == {
        "items": [{"id": "x"}],
        "next_before": "n",
        "total": 1,
    }
    assert ran == []
    cli._emit_list([_Row("x")], _ns(json=False), lambda: ran.append(True))
    assert ran == [True]
    assert capsys.readouterr().out == ""


# -- J8 errors -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (None, "network"),
        (401, "auth"),
        (403, "forbidden"),
        (404, "not_found"),
        (409, "conflict"),
        (412, "conflict"),
        (422, "validation"),
        (400, "validation"),
        (426, "upgrade_required"),
        (500, "server"),
        (503, "server"),
    ],
)
def test_error_code_for_status(status, code) -> None:
    assert cli._error_code_for_status(status) == code
    assert code in cli._ERROR_CODES


def test_emit_error_json_line_on_stderr(capsys) -> None:
    cli._emit_error(_ns(json=True), "not_found", "no such room", status=404)
    cap = capsys.readouterr()
    assert cap.out == ""
    assert json.loads(cap.err) == {
        "error": {"code": "not_found", "status": 404, "message": "no such room"}
    }
    assert cap.err.count("\n") == 1


def test_emit_error_json_without_status(capsys) -> None:
    cli._emit_error(_ns(json=True), "usage", "bad flag")
    assert json.loads(capsys.readouterr().err)["error"]["status"] is None


def test_emit_error_text_mode(capsys) -> None:
    cli._emit_error(_ns(json=False), "not_found", "no such room", status=404)
    cap = capsys.readouterr()
    assert cap.out == ""
    assert cap.err == "error: no such room\n"


def test_emit_error_follows_pepper_json(capsys, monkeypatch) -> None:
    monkeypatch.setenv("PEPPER_JSON", "1")
    cli._emit_error(_ns(json=False), "server", "boom", status=500)
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "server"


def test_emit_error_rejects_unknown_code() -> None:
    with pytest.raises(ValueError, match="unknown error code"):
        cli._emit_error(_ns(json=True), "oops", "x")


# -- renamed-noun stub ---------------------------------------------------------


def _stub_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pepper")
    sub = parser.add_subparsers(dest="cmd")
    cli._add_renamed_stub(sub, "old", "use `pepper new`")
    return parser


@pytest.mark.parametrize(
    "rest",
    [[], ["list"], ["list", "--json"], ["--help"], ["x", "--room", "ski", "--yes", "-h"]],
)
def test_renamed_stub_names_the_replacement_and_exits_2(rest, capsys, monkeypatch) -> None:
    # No credentials exist and no client may be built: the stub touches neither.
    def boom(*a, **k):
        raise AssertionError("the stub must not reach the API")

    monkeypatch.setattr(api_client, "ApiClient", boom)
    args = _stub_parser().parse_args(["old", *rest])
    assert args.handler(args) == 2
    cap = capsys.readouterr()
    assert cap.out == ""
    assert cap.err == "pepper: 'old' was renamed in 0.2.0 — use `pepper new`\n"


def test_renamed_stub_is_unlisted_in_help(capsys) -> None:
    parser = _stub_parser()
    parser.print_help()
    assert "was renamed" not in capsys.readouterr().out


def test_renamed_stub_does_not_fall_through_to_chat(tmp_path, monkeypatch, capsys) -> None:
    """Wired into the real parser, the stub (not a chat message) answers the old noun."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    real_build = cli._build_parser

    def build():
        parser, names = real_build()
        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        cli._add_renamed_stub(sub, "retired-noun", "use `pepper other`")
        return parser, names | {"retired-noun"}

    monkeypatch.setattr(cli, "_build_parser", build)

    def no_chat(*a, **k):
        raise AssertionError("must not chat")

    monkeypatch.setattr(cli, "_run_one_shot", no_chat, raising=False)
    assert cli.main(["retired-noun", "anything"]) == 2
    assert "was renamed" in capsys.readouterr().err
