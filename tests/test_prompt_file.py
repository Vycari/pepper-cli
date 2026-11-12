"""``prompt_file``: the shebang line, substitution and appended arguments."""

from __future__ import annotations

import pytest

from pepper_cli.prompt_file import PromptFileError, build_prompt, strip_shebang


def test_strip_shebang_drops_only_a_leading_hash_bang_line() -> None:
    assert strip_shebang("#!/usr/bin/env -S pepper run\nHello\n") == "Hello\n"
    assert strip_shebang("Hello\n#!not a shebang\n") == "Hello\n#!not a shebang\n"
    assert strip_shebang("#!/usr/bin/env -S pepper run") == ""


def test_no_args_sends_the_body_trimmed() -> None:
    assert build_prompt("#!/usr/bin/env -S pepper run\n\nSummarize my day.\n\n", []) == (
        "Summarize my day."
    )


def test_positional_placeholders_are_substituted() -> None:
    text = "Compare {{1}} with {{ 2 }}."
    assert build_prompt(text, ["a.md", "b.md"]) == "Compare a.md with b.md."


def test_a_placeholder_can_repeat() -> None:
    assert build_prompt("{{1}} and {{1}}", ["x"]) == "x and x"


def test_all_placeholder_joins_every_argument_and_uses_them_all() -> None:
    assert build_prompt("Look up {{@}}", ["red", "pepper"]) == "Look up red pepper"


def test_unreferenced_arguments_are_appended_as_context() -> None:
    assert build_prompt("Read {{2}}.", ["first", "second", "third"]) == (
        "Read second.\n\nArguments:\n- first\n- third"
    )


def test_a_file_with_no_placeholders_gets_every_argument_appended() -> None:
    assert build_prompt("Summarize these.", ["a.md", "b.md"]) == (
        "Summarize these.\n\nArguments:\n- a.md\n- b.md"
    )


def test_four_braces_are_a_literal_pair() -> None:
    assert build_prompt("Keep {{{{1}} as is: {{1}}", ["x"]) == "Keep {{1}} as is: x"


def test_other_brace_syntax_is_left_alone() -> None:
    assert build_prompt("A {{name}} and {{ 0x }}", []) == "A {{name}} and {{ 0x }}"


@pytest.mark.parametrize(("text", "args"), [("Read {{2}}", ["one"]), ("Read {{0}}", ["one"])])
def test_a_placeholder_without_its_argument_is_an_error(text: str, args: list[str]) -> None:
    with pytest.raises(PromptFileError, match=r"given 1 argument$"):
        build_prompt(text, args)


def test_missing_arguments_are_all_named() -> None:
    with pytest.raises(PromptFileError, match=r"\{\{1\}\}, \{\{3\}\} but was given 0 arguments"):
        build_prompt("{{1}} {{3}}", [])


def test_all_placeholder_with_no_arguments_is_an_error() -> None:
    with pytest.raises(PromptFileError, match=r"\{\{@\}\} but was given 0 arguments"):
        build_prompt("Summarize {{@}} and list open items.", [])


@pytest.mark.parametrize("text", ["", "   \n", "#!/usr/bin/env -S pepper run\n\n"])
def test_an_empty_prompt_is_an_error(text: str) -> None:
    with pytest.raises(PromptFileError, match="empty"):
        build_prompt(text, [])


def test_an_empty_body_with_arguments_is_still_an_error() -> None:
    with pytest.raises(PromptFileError, match="empty"):
        build_prompt("#!/usr/bin/env -S pepper run\n", ["x"])
