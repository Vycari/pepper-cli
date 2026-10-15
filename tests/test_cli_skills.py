"""Tests for the ``pepper skills`` subcommands + the SKILL.md round-trip.

Same approach as ``test_cli_todos.py``: invoke ``cli.main([...])`` with a
monkeypatched ``api_client.ApiClient`` so commands run without the
network. The editor is monkeypatched at ``cli._open_in_editor`` so tests
"type" into the document programmatically.
"""

from __future__ import annotations

import pytest

from pepper_cli import api_client, cli, credentials, skill_doc
from pepper_cli.secret import SecretStr


@pytest.fixture
def logged_in(tmp_home):
    credentials.save(server_url="https://pepper.test", token=SecretStr("pep_pat_test"))
    return tmp_home


def _detail(**over) -> api_client.SkillDetail:
    base = {
        "name": "meeting-notes",
        "description": "When the user asks for meeting notes.",
        "body": "Take notes.",
        "model": None,
        "toolsets": (),
        "max_tool_rounds": None,
        "builtin": False,
    }
    base.update(over)
    return api_client.SkillDetail(**base)


class _FakeSkillsApi:
    instances: list[_FakeSkillsApi] = []

    canned_list: list[api_client.SkillSummary] = []
    canned_detail: api_client.SkillDetail = _detail()

    def __init__(self, *, server_url, token) -> None:
        self.server_url = server_url
        self.calls: list[tuple[str, dict]] = []
        _FakeSkillsApi.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def list_skills(self):
        self.calls.append(("list_skills", {}))
        return list(self.canned_list)

    def get_skill(self, name):
        self.calls.append(("get_skill", {"name": name}))
        return self.canned_detail

    def create_skill(self, **kwargs):
        self.calls.append(("create_skill", kwargs))
        return _detail(**{k: v for k, v in kwargs.items() if k in _detail().__dict__})

    def update_skill(self, **kwargs):
        self.calls.append(("update_skill", kwargs))
        return _detail()

    def delete_skill(self, name):
        self.calls.append(("delete_skill", {"name": name}))


@pytest.fixture
def fake_api(monkeypatch):
    _FakeSkillsApi.instances = []
    _FakeSkillsApi.canned_list = []
    _FakeSkillsApi.canned_detail = _detail()
    monkeypatch.setattr(cli.api_client, "ApiClient", _FakeSkillsApi)
    return _FakeSkillsApi


# --- skill_doc round-trip -------------------------------------------------


def test_compose_parse_round_trip_with_extras():
    fields = skill_doc.SkillFields(
        name="repo-triage",
        description="Use this when: the user asks about the backlog.",
        body="Call the github tool.\n\nThen summarize.",
        model="pro",
        toolsets=("github",),
        max_tool_rounds=12,
    )
    text = skill_doc.compose_skill_md(fields)
    parsed, err = skill_doc.parse_skill_md(text)
    assert err is None
    assert parsed == fields


def test_compose_omits_unset_extras():
    text = skill_doc.compose_skill_md(
        skill_doc.SkillFields(name="a-skill", description="d", body="b")
    )
    assert "model" not in text
    assert "toolsets" not in text
    assert "max_tool_rounds" not in text


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("no frontmatter", "missing YAML frontmatter"),
        ("---\nname: x\ndescription: d", "unterminated"),
        ("---\nname: x\n---\n\nbody\n", "missing 'description'"),
        ("---\ndescription: d\n---\n\nbody\n", "missing 'name'"),
        ("---\nname: x\ndescription: d\ntoolsets: github\n---\n\nbody\n", "YAML list"),
        (
            "---\nname: x\ndescription: d\nmax_tool_rounds: soon\n---\n\nbody\n",
            "integer",
        ),
    ],
)
def test_parse_rejects_malformed_documents(text, fragment):
    fields, err = skill_doc.parse_skill_md(text)
    assert fields is None
    assert fragment in err


# --- subcommands ------------------------------------------------------------


def test_skills_list_groups_yours_and_builtin(logged_in, fake_api, capsys):
    fake_api.canned_list = [
        api_client.SkillSummary(name="mine", description="my skill"),
        api_client.SkillSummary(name="morning-brief", description="built", builtin=True),
    ]
    assert cli.main(["skills", "list"]) == 0
    out = capsys.readouterr().out
    assert "mine" in out
    assert "morning-brief" in out
    assert "Built-in" in out


def test_skills_show_prints_skill_md(logged_in, fake_api, capsys):
    fake_api.canned_detail = _detail(model="pro", toolsets=("github",))
    assert cli.main(["skills", "show", "meeting-notes"]) == 0
    out = capsys.readouterr().out
    assert "name: meeting-notes" in out
    assert "model: pro" in out
    assert "toolsets:" in out


def test_skills_edit_updates_own_skill(logged_in, fake_api, monkeypatch):
    edited = skill_doc.compose_skill_md(
        skill_doc.SkillFields(
            name="meeting-notes",
            description="Updated trigger.",
            body="New body.",
            toolsets=("github",),
        )
    )
    monkeypatch.setattr(cli, "_open_in_editor", lambda initial: edited)
    assert cli.main(["skills", "edit", "meeting-notes"]) == 0
    calls = [c for inst in fake_api.instances for c in inst.calls]
    update = next(kw for name, kw in calls if name == "update_skill")
    assert update["description"] == "Updated trigger."
    assert update["toolsets"] == ["github"]


def test_skills_edit_builtin_offers_override(logged_in, fake_api, monkeypatch):
    fake_api.canned_detail = _detail(name="morning-brief", builtin=True)
    edited = skill_doc.compose_skill_md(
        skill_doc.SkillFields(name="morning-brief", description="My version.", body="Mine.")
    )
    monkeypatch.setattr(cli, "_open_in_editor", lambda initial: edited)
    monkeypatch.setattr(cli, "_prompt", lambda prompt: "y")
    assert cli.main(["skills", "edit", "morning-brief"]) == 0
    calls = [c for inst in fake_api.instances for c in inst.calls]
    assert any(name == "create_skill" for name, _ in calls)
    assert not any(name == "update_skill" for name, _ in calls)


def test_skills_edit_builtin_aborts_without_consent(logged_in, fake_api, monkeypatch):
    fake_api.canned_detail = _detail(name="morning-brief", builtin=True)
    monkeypatch.setattr(cli, "_prompt", lambda prompt: "n")
    assert cli.main(["skills", "edit", "morning-brief"]) == 0
    calls = [c for inst in fake_api.instances for c in inst.calls]
    assert not any(name in ("create_skill", "update_skill") for name, _ in calls)


def test_skills_edit_rejects_rename(logged_in, fake_api, monkeypatch):
    edited = skill_doc.compose_skill_md(
        skill_doc.SkillFields(name="different-name", description="d", body="b")
    )
    monkeypatch.setattr(cli, "_open_in_editor", lambda initial: edited)
    assert cli.main(["skills", "edit", "meeting-notes"]) == 2


def test_skills_new_creates_with_extras(logged_in, fake_api, monkeypatch):
    edited = skill_doc.compose_skill_md(
        skill_doc.SkillFields(
            name="fresh-skill",
            description="Trigger.",
            body="Steps.",
            model="flash",
            max_tool_rounds=8,
        )
    )
    monkeypatch.setattr(cli, "_open_in_editor", lambda initial: edited)
    assert cli.main(["skills", "create", "fresh-skill"]) == 0
    calls = [c for inst in fake_api.instances for c in inst.calls]
    create = next(kw for name, kw in calls if name == "create_skill")
    assert create["model"] == "flash"
    assert create["max_tool_rounds"] == 8


def test_skills_delete_requires_confirmation(logged_in, fake_api, monkeypatch):
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_prompt", lambda prompt: "n")
    assert cli.main(["skills", "delete", "meeting-notes"]) == 0
    assert not any(name == "delete_skill" for inst in fake_api.instances for name, _ in inst.calls)
    monkeypatch.setattr(cli, "_prompt", lambda prompt: "y")
    assert cli.main(["skills", "delete", "meeting-notes"]) == 0
    assert any(name == "delete_skill" for inst in fake_api.instances for name, _ in inst.calls)


def test_skills_delete_off_a_terminal_without_yes_is_exit_2(logged_in, fake_api, monkeypatch):
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    assert cli.main(["skills", "delete", "meeting-notes"]) == 2
    assert not any(name == "delete_skill" for inst in fake_api.instances for name, _ in inst.calls)


def test_skills_old_new_spelling_is_rejected(logged_in, fake_api):
    with pytest.raises(SystemExit) as ei:
        cli.main(["skills", "new", "x"])
    assert ei.value.code == 2


def test_skills_delete_yes_skips_the_prompt(logged_in, fake_api, monkeypatch):
    def fail_prompt(prompt):
        raise AssertionError("should not prompt with --yes")

    monkeypatch.setattr(cli, "_prompt", fail_prompt)
    assert cli.main(["skills", "delete", "meeting-notes", "--yes"]) == 0
    assert any(name == "delete_skill" for inst in fake_api.instances for name, _ in inst.calls)


def _skill_file(tmp_path, **kw):
    fields = {"name": "fresh-skill", "description": "Trigger.", "body": "Steps."} | kw
    path = tmp_path / "SKILL.md"
    path.write_text(skill_doc.compose_skill_md(skill_doc.SkillFields(**fields)), encoding="utf-8")
    return str(path)


def _no_editor(monkeypatch):
    def boom(initial):
        raise AssertionError("--file must not open $EDITOR")

    monkeypatch.setattr(cli, "_open_in_editor", boom)


def test_skills_create_file_posts_without_the_editor(logged_in, fake_api, monkeypatch, tmp_path):
    _no_editor(monkeypatch)
    path = _skill_file(tmp_path, model="flash")
    assert cli.main(["skills", "create", "fresh-skill", "--file", path]) == 0
    calls = [c for inst in fake_api.instances for c in inst.calls]
    create = next(kw for name, kw in calls if name == "create_skill")
    assert create["name"] == "fresh-skill" and create["model"] == "flash"


def test_skills_create_file_name_must_match_the_argument(
    logged_in, fake_api, monkeypatch, tmp_path, capsys
):
    _no_editor(monkeypatch)
    path = _skill_file(tmp_path, name="other-name")
    assert cli.main(["skills", "create", "fresh-skill", "--file", path]) == 2
    assert not any(n == "create_skill" for inst in fake_api.instances for n, _ in inst.calls)


def test_skills_create_file_unreadable_or_unparseable_is_exit_2(
    logged_in, fake_api, monkeypatch, tmp_path, capsys
):
    _no_editor(monkeypatch)
    assert cli.main(["skills", "create", "x", "--file", str(tmp_path / "missing.md")]) == 2
    bad = tmp_path / "bad.md"
    bad.write_text("no frontmatter here", encoding="utf-8")
    assert cli.main(["skills", "create", "x", "--file", str(bad)]) == 2
    assert "couldn't parse the document" in capsys.readouterr().err


def test_skills_edit_file_updates_without_the_editor(logged_in, fake_api, monkeypatch, tmp_path):
    _no_editor(monkeypatch)
    path = _skill_file(tmp_path, name="meeting-notes", description="From a file.")
    assert cli.main(["skills", "edit", "meeting-notes", "--file", path]) == 0
    calls = [c for inst in fake_api.instances for c in inst.calls]
    update = next(kw for name, kw in calls if name == "update_skill")
    assert update["description"] == "From a file."


def test_skills_edit_file_on_a_builtin_creates_the_override_without_prompting(
    logged_in, fake_api, monkeypatch, tmp_path
):
    fake_api.canned_detail = _detail(name="morning-brief", builtin=True)
    _no_editor(monkeypatch)

    def no_prompt(prompt):
        raise AssertionError("--file is non-interactive")

    monkeypatch.setattr(cli, "_prompt", no_prompt)
    path = _skill_file(tmp_path, name="morning-brief")
    assert cli.main(["skills", "edit", "morning-brief", "--file", path]) == 0
    assert any(n == "create_skill" for inst in fake_api.instances for n, _ in inst.calls)
