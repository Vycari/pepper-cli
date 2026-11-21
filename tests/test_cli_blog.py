"""``pepper blog``: list, check, publish and withdraw a room blog's posts.

Same approach as ``test_cli_notes.py``: ``cli.main([...])`` against a fake ``ApiClient``. The
room and note are given as UUIDs, which pass through their resolvers untouched.
"""

from __future__ import annotations

import json

import pytest

from pepper_cli import api_client, cli, credentials
from pepper_cli.secret import SecretStr

_ROOM = "00000000-0000-0000-0000-0000000000a1"
_NOTE = "00000000-0000-0000-0000-0000000000d1"
_POST = "00000000-0000-0000-0000-0000000000e1"

_CHECK = {
    "note_id": _NOTE,
    "title": "An Old Letter",
    "slug": "2014/03/02/an-old-letter",
    "republish": False,
    "source": "n.f.s",
    "refusal": None,
    "warnings": [],
    "flattened_links": ["Some Note"],
    "linked_posts": [],
    "other_images": [],
    "copied_images": 0,
    "review": {"human_version_found": False, "diff": "", "body": "Dear reader."},
}


class _FakeBlogApi:
    instances: list[_FakeBlogApi] = []

    def __init__(self, *, server_url, token, check=None, publish_error=None) -> None:
        self.calls: list[tuple] = []
        self._check = check or _CHECK
        self._publish_error = publish_error
        _FakeBlogApi.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def blog_posts(self, group_id):
        self.calls.append(("blog_posts", group_id))
        return {
            "room_id": group_id,
            "enabled": True,
            "may_publish": True,
            "may_withdraw": True,
            "posts": [
                {
                    "id": _POST,
                    "note_id": _NOTE,
                    "slug": "2014/03/02/an-old-letter",
                    "title": "An Old Letter",
                    "url": "https://example.com/b/2014/03/02/an-old-letter",
                    "published_at": "2014-03-02T17:00:00+00:00",
                    "withdrawn": False,
                }
            ],
            "unpublished": [{"note_id": "n2", "title": "Draft"}],
        }

    def blog_check(self, group_id, note_id, *, published_at=None):
        self.calls.append(("blog_check", group_id, note_id, published_at))
        return self._check

    def blog_publish(self, group_id, note_id, **kwargs):
        self.calls.append(("blog_publish", group_id, note_id, kwargs))
        if self._publish_error is not None:
            raise self._publish_error
        return api_client.BlogPublished(
            post_id=_POST,
            slug=self._check["slug"],
            url="https://example.com/b/" + self._check["slug"],
            republished=False,
            card="none",
        )

    def blog_withdraw(self, group_id, post_id):
        self.calls.append(("blog_withdraw", group_id, post_id))
        return api_client.BlogWithdrawn(post_id=post_id, already=False, card="edited")


@pytest.fixture
def fake(tmp_home, monkeypatch):
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    _FakeBlogApi.instances.clear()

    def install(**canned):
        monkeypatch.setattr(api_client, "ApiClient", lambda **kw: _FakeBlogApi(**canned, **kw))

    install()
    return install


def _calls() -> list[tuple]:
    return _FakeBlogApi.instances[-1].calls


def _published(calls: list[tuple]) -> list[tuple]:
    return [c for c in calls if c[0] == "blog_publish"]


def test_list_json_is_the_envelope_with_the_unpublished_notes(fake, capsys) -> None:
    assert cli.main(["blog", "list", "--room", _ROOM, "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert [p["id"] for p in body["items"]] == [_POST]
    assert body["next_before"] is None
    assert body["unpublished"] == [{"note_id": "n2", "title": "Draft"}]
    assert body["may_publish"] is True


def test_list_human_shows_posts_and_notes_not_yet_published(fake, capsys) -> None:
    assert cli.main(["blog", "list", "--room", _ROOM]) == 0
    out = capsys.readouterr().out
    assert "An Old Letter" in out
    assert "https://example.com/b/2014/03/02/an-old-letter" in out
    assert "Not yet published" in out
    assert "Draft" in out


def test_check_passes_the_date_and_prints_the_review(fake, capsys) -> None:
    when = "2014-03-02T09:00:00-08:00"
    assert cli.main(["blog", "check", "--room", _ROOM, _NOTE, "--published-at", when]) == 0
    assert ("blog_check", _ROOM, _NOTE, when) in _calls()
    out = capsys.readouterr().out
    assert "2014/03/02/an-old-letter" in out
    assert "Some Note" in out
    assert "Dear reader." in out
    assert not _published(_calls())


def test_publish_with_yes_checks_then_hands_back_the_source(fake, capsys) -> None:
    when = "2014-03-02T09:00:00-08:00"
    argv = ["blog", "publish", "--room", _ROOM, _NOTE, "--published-at", when, "--yes", "--json"]
    assert cli.main(argv) == 0
    calls = _calls()
    assert calls[0] == ("blog_check", _ROOM, _NOTE, when)
    ((_, _, _, sent),) = _published(calls)
    assert sent == {
        "source": "n.f.s",
        "acknowledge": True,
        "slug": None,
        "summary": None,
        "published_at": when,
    }
    body = json.loads(capsys.readouterr().out)
    assert body["slug"] == "2014/03/02/an-old-letter"
    assert body["card"] == "none"


def test_publish_asks_on_a_terminal_and_a_no_publishes_nothing(fake, capsys, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    asked: list[str] = []
    monkeypatch.setattr(cli, "_prompt", lambda prompt: asked.append(prompt) or "n")
    assert cli.main(["blog", "publish", "--room", _ROOM, _NOTE]) == 0
    assert "An Old Letter" in asked[0]
    assert "Dear reader." in capsys.readouterr().out
    assert not _published(_calls())


def test_publish_a_yes_at_the_prompt_publishes(fake, capsys, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_prompt", lambda prompt: "y")
    assert cli.main(["blog", "publish", "--room", _ROOM, _NOTE, "--slug", "letter"]) == 0
    ((_, _, _, sent),) = _published(_calls())
    assert sent["slug"] == "letter"
    assert "Published" in capsys.readouterr().out


def test_publish_without_yes_off_a_terminal_reads_nothing_and_exits_2(
    fake, capsys, monkeypatch
) -> None:
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    assert cli.main(["blog", "publish", "--room", _ROOM, _NOTE]) == 2
    assert "--yes" in capsys.readouterr().err
    assert _calls() == []


def test_a_refused_note_is_exit_2_and_nothing_is_published(fake, capsys) -> None:
    fake(check={**_CHECK, "refusal": "This note links to a private room."})
    assert cli.main(["blog", "publish", "--room", _ROOM, _NOTE, "--yes"]) == 2
    assert "private room" in capsys.readouterr().err
    assert not _published(_calls())


def test_a_note_changed_since_the_check_is_exit_2_with_the_servers_reason(fake, capsys) -> None:
    fake(publish_error=api_client.ApiError(status=409, body="", detail="The note changed."))
    assert cli.main(["blog", "publish", "--room", _ROOM, _NOTE, "--yes", "--json"]) == 2
    err = json.loads(capsys.readouterr().err.strip())
    assert err["error"]["code"] == "conflict"
    assert err["error"]["message"] == "The note changed."


def test_a_missing_blog_is_one_not_found_answer(fake, capsys) -> None:
    fake(publish_error=api_client.ApiError(status=404, body="", detail="Not Found"))
    assert cli.main(["blog", "publish", "--room", _ROOM, _NOTE, "--yes"]) == 2
    assert "not found" in capsys.readouterr().err


def test_withdraw_with_yes(fake, capsys) -> None:
    assert cli.main(["blog", "withdraw", "--room", _ROOM, _POST, "--yes"]) == 0
    assert ("blog_withdraw", _ROOM, _POST) in _calls()
    assert "Withdrawn" in capsys.readouterr().out
