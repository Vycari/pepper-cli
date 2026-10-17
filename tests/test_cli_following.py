"""``pepper following list/show`` against a mocked server (synthetic data)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from pepper_cli import api_client, cli, credentials, following_view, numbered_view
from pepper_cli.secret import SecretStr

FEED_A = "feeds:feed:aaaaaaaa-1111-4111-8111-111111111111"
FEED_B = "feeds:feed:aaaabbbb-2222-4222-8222-222222222222"


def _row(title: str, when: str = "Today", *, new: bool = True, **kw) -> dict:
    return {
        "title": title,
        "url": f"https://example.com/{title}",
        "when_label": when,
        "new": new,
        "upcoming": False,
        "duration_label": None,
        **kw,
    }


DOC = {
    "rev": 1,
    "data": {
        "items": [
            {
                "ref": FEED_A,
                "kind": "feed",
                "group": "Reading",
                "name": "Alpha Weekly",
                "latest": "A1",
                "latest_at": "2026-10-16T17:00:00Z",
                "when_label": "Today",
                "live": False,
                "state": "ok",
                "recent": {"label": "Recent posts", "items": [_row("A1"), _row("A2")]},
            },
            {
                "ref": FEED_B,
                "kind": "feed",
                "group": "Reading",
                "name": "Alpha Daily",
                "latest": "B1 \x1b[2Jevil",
                "latest_at": "2026-10-16T16:00:00Z",
                "when_label": "Today",
                "live": False,
                "state": "ok",
                "recent": {"label": "Recent posts", "items": [_row("B1")]},
            },
            {
                "ref": "sports:team:nfl:21",
                "kind": "team",
                "group": "Sports",
                "name": "Eagles",
                "latest": "Q3 · Eagles 17, Cowboys 13",
                "latest_at": "2026-10-16T17:30:00Z",
                "when_label": "Live",
                "live": True,
                "state": "ok",
                "recent": {
                    "label": "Games",
                    "items": [_row("Eagles 17, Cowboys 13", "Live", new=False, url=None)],
                },
            },
            {
                "ref": "sports:event:olympics",
                "kind": "event",
                "group": "Sports",
                "name": "The Olympics",
                "latest": None,
                "latest_at": None,
                "when_label": None,
                "live": False,
                "state": "ok",
                "recent": {"label": "Games", "items": []},
            },
        ],
        "counts": {"unread": 4, "live": 1, "by_group": {"Reading": 2, "Sports": 2}},
    },
}


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("PEPPER_JSON", raising=False)
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    routes: dict[str, httpx.Response] = {
        "/api/v1/whoami": httpx.Response(
            200, json={"user_id": "u", "display_name": "U", "timezone": "UTC"}
        ),
        "/api/v1/apps/following/data": httpx.Response(200, json=DOC),
    }
    seen: list[tuple[str, str]] = []
    bodies: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "POST":
            bodies.append((request.url.path, json.loads(request.content)))
        return routes.get(request.url.path, httpx.Response(404, json={}))

    real = api_client.ApiClient
    monkeypatch.setattr(
        api_client,
        "ApiClient",
        lambda **kw: real(client=httpx.Client(transport=httpx.MockTransport(handler)), **kw),
    )
    return type("Server", (), {"routes": routes, "seen": seen, "bodies": bodies})


def _run(capsys, *argv: str) -> tuple[int, str, str]:
    rc = cli.main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def test_list_groups_and_marks(server, capsys) -> None:
    rc, out, _ = _run(capsys, "following", "list")
    assert rc == 0
    assert out.index("Reading (2)") < out.index("Sports (2)")  # the server's by_group order
    assert "LIVE" in out and "2 new" in out
    assert "nothing yet" in out  # the Olympics have no latest line
    assert "aaaaaaaa" in out and "nfl:21" in out  # short ids
    assert "4 followed · 1 live · 4 unread" in out
    assert "\x1b" not in out  # server text can't drive the terminal


def test_list_group_filter_takes_a_prefix(server, capsys) -> None:
    rc, out, _ = _run(capsys, "following", "list", "--group", "spo")
    assert rc == 0 and "Eagles" in out and "Alpha" not in out


def test_list_unknown_group_is_a_usage_error(server, capsys) -> None:
    rc, out, err = _run(capsys, "following", "list", "--group", "Nope", "--json")
    assert rc == 2 and out == ""
    assert json.loads(err)["error"]["code"] == "usage"


def test_list_json_is_the_envelope_with_the_servers_items(server, capsys) -> None:
    rc, out, _ = _run(capsys, "following", "list", "--json", "--group", "Reading")
    body = json.loads(out)
    assert rc == 0 and body["next_before"] is None
    assert [i["ref"] for i in body["items"]] == [FEED_A, FEED_B]
    assert body["counts"]["unread"] == 4
    assert ("GET", "/api/v1/apps/following/data") in server.seen


def test_show_by_short_id_ref_and_name(server, capsys) -> None:
    for query in ("aaaaaaaa", FEED_A, "alpha w"):
        rc, out, _ = _run(capsys, "following", "show", query)
        assert rc == 0, query
        assert "Alpha Weekly" in out and "A2" in out and "https://example.com/A1" in out


def test_show_json_is_the_item(server, capsys) -> None:
    rc, out, _ = _run(capsys, "following", "show", "nfl:21", "--json")
    assert rc == 0 and json.loads(out)["name"] == "Eagles"


def test_show_ambiguous_and_missing(server, capsys) -> None:
    rc, out, err = _run(capsys, "following", "show", "alpha", "--json")
    assert rc == 2 and out == ""
    assert json.loads(err)["error"]["code"] == "ambiguous"
    rc, _, err = _run(capsys, "following", "show", "zzz")
    assert rc == 2 and "don't follow" in err


def test_show_a_follow_with_nothing_recent(server, capsys) -> None:
    rc, out, _ = _run(capsys, "following", "show", "olympics")
    assert rc == 0 and "Nothing fetched from this one yet." in out


def test_server_error_is_reported(server, capsys) -> None:
    server.routes["/api/v1/apps/following/data"] = httpx.Response(500, json={"detail": "boom"})
    rc, _, err = _run(capsys, "following", "list")
    assert rc == 1 and "boom" in err


@pytest.mark.parametrize(
    ("ref", "short"),
    [
        (FEED_A, "aaaaaaaa"),
        ("calendars:follow:452713c6ae4e4b7bbc79625c73d59b4b", "452713c6"),
        ("sports:team:nfl:21", "nfl:21"),
        ("sports:event:olympics", "olympics"),
        ("odd", "odd"),
    ],
)
def test_short_id(ref, short) -> None:
    assert following_view.short_id(ref) == short


# --- search / create / delete ----------------------------------------------

ACT = "/api/v1/apps/following/actions/"
DODGERS = {
    "ref": "sports:team:mlb:19",
    "payload": {"espn": 19},
    "kind": "team",
    "name": "Los Angeles Dodgers",
    "sub": "MLB",
    "detail": "Team · MLB",
    "following": False,
}
OTHER = {**DODGERS, "ref": "sports:team:mlb:20", "payload": {"espn": 20}, "name": "Dodgers Fans"}
MINE = {**DODGERS, "ref": "sports:team:nfl:21", "name": "Eagles", "following": True}


def _search_returns(server, *, yours=(), new=()) -> None:
    server.routes[ACT + "search"] = httpx.Response(
        200, json={"result": {"yours": list(yours), "new": list(new)}}
    )
    server.routes[ACT + "follow"] = httpx.Response(
        200, json={"result": {"ok": True, "message": "Now following X.", "item": None}}
    )
    server.routes[ACT + "unfollow"] = httpx.Response(
        200, json={"result": {"ok": True, "message": "Unfollowed Alpha Weekly."}}
    )


def test_search_numbers_yours_then_new_and_saves_them(server, capsys) -> None:
    _search_returns(server, yours=[MINE], new=[DODGERS, OTHER])
    rc, out, _ = _run(capsys, "following", "search", "dodg", "ers")
    assert rc == 0
    assert out.index(" 1. Eagles") < out.index(" 2. Los Angeles Dodgers") < out.index(" 3. Dodgers")
    assert "following" in out and "Team · MLB" in out
    assert server.bodies == [(ACT + "search", {"params": {"q": "dodg ers"}})]
    saved = json.loads(
        (credentials.credentials_path().parent / "following-search.json").read_text()
    )
    assert saved["query"] == "dodg ers" and [c["n"] for c in saved["candidates"]] == [1, 2, 3]


def test_search_json_is_the_envelope(server, capsys) -> None:
    _search_returns(server, new=[DODGERS])
    rc, out, _ = _run(capsys, "following", "search", "dodgers", "--json")
    body = json.loads(out)
    assert rc == 0 and body["next_before"] is None
    assert body["items"][0]["n"] == 1 and body["items"][0]["payload"] == {"espn": 19}


def test_create_by_number_sends_ref_and_payload(server, capsys) -> None:
    _search_returns(server, new=[DODGERS, OTHER])
    _run(capsys, "following", "search", "dodgers")
    server.bodies.clear()
    rc, _, err = _run(capsys, "following", "create", "2")
    assert rc == 0 and "Now following" in err
    assert server.bodies == [
        (ACT + "follow", {"params": {"ref": "sports:team:mlb:20", "payload": {"espn": 20}}})
    ]


def test_create_by_number_without_or_past_the_last_search(server, capsys) -> None:
    rc, out, err = _run(capsys, "following", "create", "1", "--json")
    assert rc == 2 and out == "" and json.loads(err)["error"]["code"] == "not_found"
    _search_returns(server, new=[DODGERS])
    _run(capsys, "following", "search", "dodgers")
    rc, _, err = _run(capsys, "following", "create", "9")
    assert rc == 2 and "No candidate 9" in err
    assert all(path != ACT + "follow" for path, _ in server.bodies)


def test_create_refuses_typed_text_and_never_calls_the_server(server, capsys) -> None:
    _search_returns(server, new=[DODGERS])
    for text in ("dodgers", "https://example.com/feed"):
        rc, out, err = _run(capsys, "following", "create", text, "--json")
        assert rc == 2 and out == "" and json.loads(err)["error"]["code"] == "usage"
        assert "following search" in json.loads(err)["error"]["message"]
    assert server.bodies == []  # no search, no follow: the CLI picks nothing for typed text


def test_search_then_create_a_url_by_number(server, capsys) -> None:
    _search_returns(server, new=[{**DODGERS, "ref": "feeds:feed:new", "name": "Example"}])
    _run(capsys, "following", "search", "https://example.com")
    rc, _, _ = _run(capsys, "following", "create", "1")
    assert rc == 0 and server.bodies[-1][1]["params"]["ref"] == "feeds:feed:new"


def test_create_always_asks_the_server_even_for_a_row_marked_following(server, capsys) -> None:
    # The saved "following" flag is a snapshot: the follow may have been deleted since the
    # search, so the CLI sends the follow and the server says what is true now.
    _search_returns(server, yours=[MINE])
    _run(capsys, "following", "search", "eagles")
    _run(capsys, "following", "delete", "eagles", "--yes")
    server.bodies.clear()
    rc, _, err = _run(capsys, "following", "create", "1")
    assert rc == 0 and "Now following" in err
    assert server.bodies == [
        (ACT + "follow", {"params": {"ref": MINE["ref"], "payload": MINE["payload"]}})
    ]


def test_a_failed_search_save_drops_the_old_list_so_create_follows_nothing(
    server, capsys, monkeypatch
) -> None:
    _search_returns(server, new=[OTHER])
    _run(capsys, "following", "search", "other")

    def refuse(*a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(numbered_view.tempfile, "mkstemp", refuse)
    _search_returns(server, new=[DODGERS])
    rc, out, err = _run(capsys, "following", "search", "dodgers")
    assert rc == 0 and "Los Angeles Dodgers" in out
    assert "couldn't save" in err and "won't work" in err
    rc, out, _ = _run(capsys, "following", "search", "dodgers", "--json")
    assert rc == 0 and "numbers were not saved" in json.loads(out)["note"]
    server.bodies.clear()
    rc, _, err = _run(capsys, "following", "create", "1")
    assert rc == 2 and "No candidate 1" in err
    assert server.bodies == []  # never OTHER, the old list's candidate 1


def test_a_stale_search_list_that_cannot_be_removed_is_not_shown(
    server, capsys, monkeypatch
) -> None:
    _search_returns(server, new=[OTHER])
    _run(capsys, "following", "search", "other")

    def refuse(*a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(numbered_view.tempfile, "mkstemp", refuse)
    monkeypatch.setattr(Path, "unlink", refuse)
    _search_returns(server, new=[DODGERS])
    rc, out, err = _run(capsys, "following", "search", "dodgers", "--json")
    assert rc == 1 and out == ""
    assert "could not be removed" in json.loads(err)["error"]["message"]


def test_delete_unfollows_the_resolved_ref(server, capsys) -> None:
    _search_returns(server)
    rc, _, err = _run(capsys, "following", "delete", "alpha w", "--yes")
    assert rc == 0 and "Unfollowed" in err
    assert server.bodies == [(ACT + "unfollow", {"params": {"ref": FEED_A}})]


def test_delete_needs_yes_off_a_terminal_and_in_json(server, capsys) -> None:
    _search_returns(server)
    rc, _, err = _run(capsys, "following", "delete", "aaaaaaaa")
    assert rc == 2 and "--yes" in err
    rc, out, err = _run(capsys, "following", "delete", "aaaaaaaa", "--json")
    assert rc == 2 and out == ""
    assert server.bodies == []


def test_delete_prompts_on_a_terminal(server, capsys, monkeypatch) -> None:
    _search_returns(server)
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_prompt", lambda _p: "n")
    rc, _, err = _run(capsys, "following", "delete", "aaaaaaaa")
    assert rc == 0 and "Aborted" in err and server.bodies == []
    monkeypatch.setattr(cli, "_prompt", lambda _p: "y")
    rc, _, _ = _run(capsys, "following", "delete", "aaaaaaaa")
    assert rc == 0 and server.bodies[-1][0] == ACT + "unfollow"


def test_delete_unknown_and_ambiguous_never_call_unfollow(server, capsys) -> None:
    _search_returns(server)
    assert _run(capsys, "following", "delete", "zzz", "--yes")[0] == 2
    assert _run(capsys, "following", "delete", "alpha", "--yes")[0] == 2
    assert server.bodies == []


def test_search_state_is_per_profile(server, capsys) -> None:
    _search_returns(server, new=[DODGERS])
    _run(capsys, "following", "search", "dodgers")
    credentials.set_profile("other")
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_other"))
    credentials.set_profile(None)
    rc, _, err = _run(capsys, "--profile", "other", "following", "create", "1")
    assert rc == 2 and "No candidate" in err  # that profile never searched
    assert _run(capsys, "following", "create", "1")[0] == 0  # the default profile did


def _item(name: str, group: str) -> dict:
    return {"ref": f"x:{name}", "name": name, "group": group}


def test_groups_follow_the_servers_by_group_order() -> None:
    items = [_item("a", "Sports"), _item("b", "Reading")]
    by_group = {"Reading": 1, "Sports": 1}
    assert [g for g, _ in following_view.grouped(items, {"by_group": by_group})] == [
        "Reading",
        "Sports",
    ]
    reverse = {"by_group": {"Sports": 1, "Reading": 1}}
    assert [g for g, _ in following_view.grouped(items, reverse)] == ["Sports", "Reading"]


def test_groups_without_by_group_keep_first_appearance() -> None:
    items = [_item("a", "Sports"), _item("b", "Reading"), _item("c", "Sports")]
    got = following_view.grouped(items, {})
    assert [(g, len(r)) for g, r in got] == [("Sports", 2), ("Reading", 1)]
    assert [g for g, _ in following_view.grouped(items, None)] == ["Sports", "Reading"]


def test_a_group_missing_from_by_group_goes_last_by_first_appearance() -> None:
    items = [_item("a", "Zed"), _item("b", "Reading"), _item("c", "Alpha")]
    got = following_view.grouped(items, {"by_group": {"Reading": 1}})
    assert [g for g, _ in got] == ["Reading", "Zed", "Alpha"]


def test_an_unknown_group_matches_nothing(server, capsys) -> None:
    rc, out, err = _run(capsys, "following", "list", "--group", "nope")
    assert rc != 0 and "is not one of: Reading, Sports" in out + err
    data = {"counts": {"by_group": {"Reading": 1, "Sports": 1}}}
    assert following_view.resolve_group(data, "sp") == "Sports"


def test_an_item_without_a_group_filters_as_other(server, capsys) -> None:
    body = json.loads(json.dumps(DOC))
    body["data"]["items"].append({"ref": "x:loose", "name": "Loose", "group": None})
    server.routes["/api/v1/apps/following/data"] = httpx.Response(200, json=body)
    _, out, _ = _run(capsys, "following", "list")
    assert "Other (1)" in out and "Loose" in out
    rc, out, _ = _run(capsys, "following", "list", "--json", "--group", "Other")
    assert rc == 0 and [i["ref"] for i in json.loads(out)["items"]] == ["x:loose"]
