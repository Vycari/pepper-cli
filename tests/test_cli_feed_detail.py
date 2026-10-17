"""``pepper feed list`` (the server's ranked list, numbered) and ``pepper feed show N`` (detail,
``--transcript``, ``--read``) against a mocked server and a mocked web (synthetic data only)."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

import httpx
import pytest

from pepper_cli import api_client, article_reader, cli, credentials, feed, numbered_view
from pepper_cli.secret import SecretStr

SITE = "feeds:feed:aaaaaaaa-1111-4111-8111-111111111111"
POD = "feeds:podcast:bbbbbbbb-2222-4222-8222-222222222222"
TEAM = "sports:team:ncaaf:252"
EP = "cccccccc-3333-4333-8333-333333333333"

LONG = "A long enough paragraph to survive the reader's short-line filter, for sure."


def _entry(title: str, ref: str = SITE, **kw) -> dict:
    return {
        "source": "Example News",
        "ref": ref,
        "group": "Reading",
        "kind": "feed",
        "title": title,
        "url": f"https://news.example/{title.replace(' ', '-')}",
        "when_label": "Today",
        "published_at": "2026-10-17T13:02:00Z",
        "starts_at": None,
        "summary": None,
        "new": True,
        "upcoming": False,
        "live": False,
        "more": [],
        "fold": None,
        **kw,
    }


def _doc() -> dict:
    third = _entry("Third", published_at=None)
    fourth = _entry("Fourth", published_at=None)
    return {
        "rev": 1,
        "data": {
            "items": [
                {
                    "ref": POD,
                    "kind": "podcast",
                    "group": "Podcasts",
                    "name": "The Show",
                    "recent": {
                        "items": [
                            {
                                "title": "Ep 1",
                                "url": None,
                                "episode": {"id": EP, "show_title": "The Show"},
                            }
                        ]
                    },
                }
            ],
            "counts": {"by_group": {"Reading": 1}},
            # a deliberately odd order: the CLI must show it as sent
            "feed": [
                _entry(
                    "Notre Dame at BYU",
                    TEAM,
                    source="BYU Cougars",
                    group="Sports",
                    kind="team",
                    url=None,
                    when_label="Tomorrow 4:30 PM",
                    published_at=None,
                    starts_at="2026-10-17T23:30:00Z",
                    new=False,
                    upcoming=True,
                    fold="+ next: Oct 23 BYU at UCF",
                    league="NCAAF",
                    records="ND 5-1 · BYU 6-0",
                    headline="Two unbeatens meet in Provo.",
                    note="Ranked matchup",
                    status="4:30 PM",
                ),
                _entry(
                    "Rock &#038; Roll",
                    summary="Fish &amp; chips",
                    more=[
                        {k: v for k, v in third.items() if k not in ("more", "fold")},
                        {k: v for k, v in fourth.items() if k not in ("more", "fold")},
                    ],
                    fold="+2 more",
                ),
                _entry(
                    "Ep 1",
                    POD,
                    source="The Show",
                    group="Podcasts",
                    kind="podcast",
                    url=None,
                    episode_id=EP,
                ),
            ],
        },
    }


PAGE = (
    "<html><head><meta property='og:title' content='Rock Page'>"
    "<meta name='author' content='Ann Author'></head><body><article>"
    + "".join(f"<p>{LONG} #{i}</p>" for i in range(1, 16))
    + "</article></body></html>"
)


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("PEPPER_JSON", raising=False)
    credentials.save(server_url="http://pepper.test", token=SecretStr("pep_pat_abc"))
    routes: dict[str, httpx.Response] = {
        "/api/v1/whoami": httpx.Response(
            200, json={"user_id": "u", "display_name": "U", "timezone": "America/Los_Angeles"}
        ),
        "/api/v1/apps/following/data": httpx.Response(200, json=_doc()),
        "/api/v1/apps/following/actions/episode_transcript": httpx.Response(
            200,
            json={
                "result": {
                    "ok": True,
                    "message": "Saved.",
                    "item_id": EP,
                    "kind": "transcript",
                    "status": "completed",
                    "text": "Welcome to the show. " * 30,
                    "note": None,
                }
            },
        ),
    }
    seen: list[httpx.Request] = []
    web_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return routes.get(request.url.path, httpx.Response(404, json={}))

    def web(request: httpx.Request) -> httpx.Response:
        web_seen.append(request)
        return web_routes.get(request.url.host, httpx.Response(404))(request)

    web_routes: dict = {
        "news.example": lambda r: httpx.Response(
            200, text=PAGE, headers={"content-type": "text/html"}
        )
    }
    real = api_client.ApiClient
    monkeypatch.setattr(
        api_client,
        "ApiClient",
        lambda **kw: real(client=httpx.Client(transport=httpx.MockTransport(handler)), **kw),
    )
    monkeypatch.setattr(article_reader, "_transport", httpx.MockTransport(web))
    monkeypatch.setattr(article_reader, "_resolve", lambda host: ["93.184.215.14"])
    return type(
        "Server", (), {"routes": routes, "seen": seen, "web": web_seen, "web_routes": web_routes}
    )


def _run(capsys, *argv: str) -> tuple[int, str, str]:
    rc = cli.main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def _run(capsys, *argv: str) -> tuple[int, str, str]:
    rc = cli.main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def _numbers(capsys) -> dict[str, int]:
    """``{title: n}`` from ``pepper feed list --json``."""
    rc, out, _ = _run(capsys, "feed", "list", "--json")
    assert rc == 0
    return {html.unescape(i["title"]): i["n"] for i in json.loads(out)["items"]}


# --- the list: the server's order, numbered ------------------------------------


def test_feed_lists_the_servers_order_numbered_in_text_and_json(server, capsys) -> None:
    rc, out, _ = _run(capsys, "feed", "list", "--json")
    items = json.loads(out)["items"]
    assert rc == 0 and [i["n"] for i in items] == [1, 2, 3]
    assert [i["ref"] for i in items] == [TEAM, SITE, POD]  # as sent, not re-ranked
    assert "score" not in items[0] and "why" not in items[0]
    rc, out, _ = _run(capsys, "feed", "list")
    assert out.index("Notre Dame") < out.index("Rock & Roll") < out.index("Ep 1")
    assert " 1. " in out and "+2 more" in out and "Fish & chips" in out
    assert "&#038;" not in out and "&amp;" not in out


def test_feed_saves_identities_not_ranks(server, capsys) -> None:
    _numbers(capsys)
    rows, _ = feed.VIEW.load() or ([], {})
    assert {"ref", "title", "url"} <= set(rows[0])
    assert rows[0]["id"].startswith(rows[0]["ref"])
    assert feed.VIEW.state_path().parent == credentials.credentials_path().parent
    assert feed.VIEW.state_path().name == "feed-view.json"


def test_limit_and_group_are_plain_filters(server, capsys) -> None:
    _, out, _ = _run(capsys, "feed", "list", "--json", "--limit", "1")
    assert [i["ref"] for i in json.loads(out)["items"]] == [TEAM]
    _, out, _ = _run(capsys, "feed", "list", "--json", "--group", "pod")
    assert [i["ref"] for i in json.loads(out)["items"]] == [POD]


def test_group_resolves_against_the_feed_entries_too(server, capsys) -> None:
    # Sports is only in data.feed: not in counts.by_group, not on a followed item
    rc, out, _ = _run(capsys, "feed", "list", "--json", "--group", "spo")
    assert rc == 0 and [i["ref"] for i in json.loads(out)["items"]] == [TEAM]
    rc, _, err = _run(capsys, "feed", "list", "--json", "--group", "nope")
    assert rc == 2 and "Reading, Podcasts, Sports" in err


def test_no_server_ranking_is_one_line_and_never_a_client_ranking(server, capsys) -> None:
    doc = _doc()
    del doc["data"]["feed"]
    server.routes["/api/v1/apps/following/data"] = httpx.Response(200, json=doc)
    rc, out, _ = _run(capsys, "feed", "list")
    assert rc == 0 and "pepper#4852" in out and out.count("\n") == 1
    rc, out, _ = _run(capsys, "feed", "list", "--json")
    body = json.loads(out)
    assert rc == 0 and body["items"] == [] and body["next_before"] is None
    assert "pepper#4852" in body["note"]


def test_an_empty_ranked_list_is_not_the_unranked_note(server, capsys) -> None:
    doc = _doc()
    doc["data"]["feed"] = []
    server.routes["/api/v1/apps/following/data"] = httpx.Response(200, json=doc)
    rc, out, _ = _run(capsys, "feed", "list")
    assert rc == 0 and "nothing in your feed" in out and "4852" not in out


def test_the_list_carries_a_coloured_type_tag() -> None:
    from pepper_cli import following_render

    tags = {g: following_render.group_tag(g) for g in following_render._GROUP_TAG}
    assert "yellow" in tags["Sports"] and "magenta" in tags["Podcasts"]
    assert "blue" in tags["Reading"] and "red" in tags["Shows"] and "cyan" in tags["Events"]


# --- feed N ------------------------------------------------------------------


def test_item_in_full_shows_summary_exact_time_fold_and_more(server, capsys) -> None:
    n = _numbers(capsys)["Rock & Roll"]
    rc, out, _ = _run(capsys, "feed", "show", str(n))
    assert rc == 0
    assert f"{n}. Rock & Roll" in out and "&#038;" not in out
    for needle in ("Example News", "Reading", "https://news.example/", "+2 more", "Fish & chips"):
        assert needle in out
    assert "Sat Oct 17, 6:02 AM PDT" in out  # published_at in the account timezone
    assert "Also from Example News" in out and "Third" in out and "Fourth" in out
    assert "score" not in out


def test_event_time_comes_from_starts_at(server, capsys) -> None:
    n = _numbers(capsys)["Notre Dame at BYU"]
    rc, out, _ = _run(capsys, "feed", "show", str(n))
    assert rc == 0 and "Tomorrow 4:30 PM (Sat Oct 17, 4:30 PM PDT)" in out and "upcoming" in out


def test_item_json_is_the_servers_entry_plus_n_and_local_time(server, capsys) -> None:
    n = _numbers(capsys)["Rock & Roll"]
    rc, out, _ = _run(capsys, "feed", "show", str(n), "--json")
    d = json.loads(out)
    assert rc == 0 and d["n"] == n and d["fold"] == "+2 more"
    assert {m["title"] for m in d["more"]} == {"Third", "Fourth"}
    assert d["when"].startswith("2026-10-17T06:02") and "article" not in d


def test_transcript_calls_the_read_only_action_with_the_episode(server, capsys) -> None:
    n = _numbers(capsys)["Ep 1"]
    rc, out, _ = _run(capsys, "feed", "show", str(n), "--transcript")
    assert rc == 0 and "Transcript" in out and "Welcome to the show." in out
    call = next(r for r in server.seen if r.url.path.endswith("episode_transcript"))
    assert json.loads(call.content) == {"params": {"ref": POD, "item_id": EP}}
    rc, out, _ = _run(capsys, "feed", "show", str(n), "--transcript", "--json")
    assert json.loads(out)["transcript"]["kind"] == "transcript"


def test_a_podcast_entry_without_episode_id_says_it_needs_a_newer_server(server, capsys) -> None:
    doc = _doc()
    next(e for e in doc["data"]["feed"] if e["title"] == "Ep 1").pop("episode_id")
    server.routes["/api/v1/apps/following/data"] = httpx.Response(200, json=doc)
    n = _numbers(capsys)["Ep 1"]
    rc, out, _ = _run(capsys, "feed", "show", str(n), "--transcript")
    assert rc == 0 and "needs a newer server" in out
    assert not any(r.url.path.endswith("episode_transcript") for r in server.seen)


def test_a_sports_entry_renders_a_game_block(server, capsys) -> None:
    n = _numbers(capsys)["Notre Dame at BYU"]
    rc, out, _ = _run(capsys, "feed", "show", str(n))
    assert rc == 0 and "Game" in out
    for text in (
        "NCAAF",
        "4:30 PM",
        "ND 5-1 · BYU 6-0",
        "Two unbeatens meet in Provo.",
        "Ranked matchup",
    ):
        assert text in out
    assert "venue" not in out and "broadcast" not in out


def test_an_entry_with_no_game_fields_has_no_game_block(server, capsys) -> None:
    n = _numbers(capsys)["Rock & Roll"]
    rc, out, _ = _run(capsys, "feed", "show", str(n))
    assert rc == 0 and "Game" not in out


def test_transcript_not_ready_is_labelled_show_notes(server, capsys) -> None:
    server.routes["/api/v1/apps/following/actions/episode_transcript"] = httpx.Response(
        200,
        json={
            "result": {
                "kind": "notes",
                "status": "pending",
                "text": "Publisher blurb.",
                "note": "No transcript yet.",
            }
        },
    )
    n = _numbers(capsys)["Ep 1"]
    rc, out, _ = _run(capsys, "feed", "show", str(n), "--transcript")
    assert (
        rc == 0 and "Show notes" in out and "no transcript yet" in out and "Publisher blurb." in out
    )


@pytest.mark.parametrize(
    "body", [{}, {"result": None}, {"result": {}}, {"result": {"kind": "notes"}}]
)
def test_an_empty_transcript_answer_says_the_server_returned_none(server, capsys, body) -> None:
    server.routes["/api/v1/apps/following/actions/episode_transcript"] = httpx.Response(
        200, json=body
    )
    n = _numbers(capsys)["Ep 1"]
    rc, out, _ = _run(capsys, "feed", "show", str(n), "--transcript")
    assert rc == 0 and "server returned no transcript" in out and "no transcript yet" not in out


def test_an_empty_transcript_answer_keeps_the_servers_kind_and_status(server, capsys) -> None:
    server.routes["/api/v1/apps/following/actions/episode_transcript"] = httpx.Response(
        200, json={"result": {"kind": "notes", "status": "pending"}}
    )
    n = _numbers(capsys)["Ep 1"]
    rc, out, _ = _run(capsys, "feed", "show", str(n), "--transcript", "--json")
    transcript = json.loads(out)["transcript"]
    assert rc == 0 and transcript["kind"] == "notes" and transcript["status"] == "pending"
    assert "no transcript" in transcript["error"]


def test_transcript_on_a_non_podcast_is_a_line_not_a_failure(server, capsys) -> None:
    n = _numbers(capsys)["Rock & Roll"]
    rc, out, _ = _run(capsys, "feed", "show", str(n), "--transcript")
    assert rc == 0 and "no podcast episode" in out
    assert not any(r.url.path.endswith("episode_transcript") for r in server.seen)


def test_read_fetches_the_page_without_pepper_credentials(server, capsys) -> None:
    n = _numbers(capsys)["Rock & Roll"]
    rc, out, _ = _run(capsys, "feed", "show", str(n), "--read")
    assert rc == 0
    assert "Rock Page" in out and "Ann Author" in out
    assert "#12" in out and "#13" not in out  # the first twelve paragraphs
    assert "3 more paragraphs" in out and "--full" in out
    (req,) = server.web
    assert req.url.host == "news.example"
    assert "authorization" not in req.headers and "pep_pat_abc" not in str(req.headers)


def test_read_full_and_json(server, capsys) -> None:
    n = _numbers(capsys)["Rock & Roll"]
    rc, out, _ = _run(capsys, "feed", "show", str(n), "--read", "--full")
    assert rc == 0 and "#15" in out and "more paragraphs" not in out
    rc, out, _ = _run(capsys, "feed", "show", str(n), "--read", "--json")
    art = json.loads(out)["article"]
    assert len(art["paragraphs"]) == 12 and art["paragraph_count"] == 15
    assert art["title"] == "Rock Page" and art["author"] == "Ann Author"


def test_read_degrades_to_one_line_when_the_site_refuses(server, capsys) -> None:
    server.web_routes["news.example"] = lambda r: httpx.Response(403)
    n = _numbers(capsys)["Rock & Roll"]
    rc, out, _ = _run(capsys, "feed", "show", str(n), "--read")
    assert rc == 0 and "paywall or bot protection" in out
    rc, out, _ = _run(capsys, "feed", "show", str(n), "--read", "--json")
    assert "403" not in out and json.loads(out)["article"]["error"]


def test_read_on_an_item_without_a_link(server, capsys) -> None:
    n = _numbers(capsys)["Ep 1"]
    rc, out, _ = _run(capsys, "feed", "show", str(n), "--read")
    assert rc == 0 and "no link to read" in out and not server.web


# --- identity, not rank ------------------------------------------------------


def test_item_is_found_again_after_the_server_reorders(server, capsys) -> None:
    n = _numbers(capsys)["Ep 1"]
    doc = _doc()
    doc["data"]["feed"].reverse()
    server.routes["/api/v1/apps/following/data"] = httpx.Response(200, json=doc)
    rc, out, _ = _run(capsys, "feed", "show", str(n))
    assert rc == 0 and "Ep 1" in out and "The Show" in out


def test_item_that_left_the_feed_is_reported_gone(server, capsys) -> None:
    n = _numbers(capsys)["Ep 1"]
    doc = _doc()
    doc["data"]["feed"] = [e for e in doc["data"]["feed"] if e["ref"] != POD]
    server.routes["/api/v1/apps/following/data"] = httpx.Response(200, json=doc)
    rc, out, err = _run(capsys, "feed", "show", str(n))
    assert rc == 2 and out == ""
    assert "is gone" in err and "Ep 1" in err and "pepper feed list" in err
    rc, out, err = _run(capsys, "feed", "show", str(n), "--json")
    assert rc == 2 and out == "" and json.loads(err)["error"]["code"] == "not_found"


def test_an_item_the_server_folded_is_still_found(server, capsys) -> None:
    n = _numbers(capsys)["Rock & Roll"]
    doc = _doc()
    rock = doc["data"]["feed"][1]
    rock["more"], rock["fold"] = [], None
    doc["data"]["feed"][1] = rock | {"title": "Newer", "url": "https://news.example/Newer"}
    doc["data"]["feed"][1]["more"] = [_entry("Rock &#038; Roll") | {"more": [], "fold": None}]
    server.routes["/api/v1/apps/following/data"] = httpx.Response(200, json=doc)
    rc, out, _ = _run(capsys, "feed", "show", str(n))
    assert rc == 0 and "Rock & Roll" in out


# --- usage -------------------------------------------------------------------


def test_no_list_yet_and_out_of_range(server, capsys) -> None:
    rc, _, err = _run(capsys, "feed", "show", "1")
    assert rc == 2 and "run 'pepper feed list' first" in err
    _numbers(capsys)
    rc, _, err = _run(capsys, "feed", "show", "99")
    assert rc == 2 and "no feed item 99" in err


@pytest.mark.parametrize(
    "argv",
    [
        ["feed", "show", "abc"],
        ["feed", "show", "1", "--full"],
        ["feed", "list", "--limit", "0"],
        ["feed", "list", "--group", "nope"],
    ],
)
def test_usage_errors_exit_2_and_print_nothing(server, capsys, argv) -> None:
    rc, out, err = _run(capsys, *argv)
    assert rc == 2 and out == "" and err


@pytest.mark.parametrize(
    "argv",
    [
        ["feed"],  # a noun needs a verb
        ["feed", "1"],  # the old `feed N` spelling
        ["feed", "show"],  # N is required
        ["feed", "show", "--open"],  # --open goes with N
        ["feed", "list", "--open"],  # --open belongs to `show`
    ],
)
def test_argparse_rejects_the_old_and_incomplete_spellings(server, capsys, argv) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
    assert capsys.readouterr().out == ""


def test_server_error_is_reported(server, capsys) -> None:
    server.routes["/api/v1/apps/following/data"] = httpx.Response(500, json={"detail": "boom"})
    rc, _, err = _run(capsys, "feed", "list")
    assert rc == 1 and "boom" in err


def test_a_failed_feed_save_drops_the_old_numbers(server, capsys, monkeypatch) -> None:
    _numbers(capsys)

    def refuse(*a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(numbered_view.tempfile, "mkstemp", refuse)
    rc, out, _ = _run(capsys, "feed", "list", "--json")
    assert rc == 0 and "numbers were not saved" in json.loads(out)["note"]
    rc, _, err = _run(capsys, "feed", "show", "1")
    assert rc == 2 and "run 'pepper feed list' first" in err
    monkeypatch.setattr(Path, "unlink", refuse)
    feed.VIEW.state_path().write_text('{"rows": [], "meta": {}}')
    rc, out, err = _run(capsys, "feed", "list")
    assert rc == 1 and out == "" and "could not be removed" in err


# --- --open and clickable titles -------------------------------------------------


@pytest.fixture
def browser(monkeypatch):
    """A desktop session whose ``webbrowser.open`` is recorded (and may be made to fail)."""
    monkeypatch.setenv("DISPLAY", ":0")
    for var in ("SSH_CONNECTION", "SSH_TTY", "BROWSER"):
        monkeypatch.delenv(var, raising=False)
    calls: list[str] = []
    result = {"ok": True}

    def fake_open(url: str) -> bool:
        calls.append(url)
        return result["ok"]

    monkeypatch.setattr(feed.webbrowser, "open", fake_open)
    return type("Browser", (), {"calls": calls, "result": result})


_ROCK = "https://news.example/rock-and-roll?a=1&b=2"


def test_open_calls_webbrowser_with_the_url_and_prints_it(server, browser, capsys) -> None:
    server.routes["/api/v1/apps/following/data"] = httpx.Response(200, json=_doc_with_url(_ROCK))
    n = _numbers(capsys)["Rock & Roll"]
    rc, out, err = _run(capsys, "feed", "show", str(n), "--open")
    assert rc == 0
    assert browser.calls == [_ROCK]
    assert _ROCK in out  # the detail's url line
    assert f"opened {_ROCK}" in err


def test_open_keeps_json_on_stdout_and_still_opens(server, browser, capsys) -> None:
    n = _numbers(capsys)["Rock & Roll"]
    rc, out, err = _run(capsys, "feed", "show", str(n), "--open", "--json")
    assert rc == 0
    assert json.loads(out)["url"].startswith("https://news.example/")
    assert len(browser.calls) == 1
    assert "https://news.example/" in err


def test_open_without_a_usable_browser_prints_the_url_and_exits_zero(
    server, browser, capsys
) -> None:
    browser.result["ok"] = False
    server.routes["/api/v1/apps/following/data"] = httpx.Response(200, json=_doc_with_url(_ROCK))
    n = _numbers(capsys)["Rock & Roll"]
    rc, _, err = _run(capsys, "feed", "show", str(n), "--open")
    assert rc == 0
    assert "no browser could be opened" in err
    assert _ROCK in err.replace("\n", "")


def test_no_display_on_linux_launches_no_browser(browser, monkeypatch) -> None:
    # open_browser alone: patching sys.platform under the whole CLI breaks it on Windows
    for var in ("DISPLAY", "WAYLAND_DISPLAY", "BROWSER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(feed.sys, "platform", "linux")
    assert feed.open_browser("https://news.example/a") is False
    assert browser.calls == []


@pytest.mark.parametrize("var", ["SSH_CONNECTION", "SSH_TTY"])
def test_open_over_ssh_with_a_display_never_launches_a_browser(
    server, browser, monkeypatch, capsys, var
) -> None:
    # the rule is the same on every platform, so it runs on whichever one CI is
    monkeypatch.setenv(var, "10.0.0.2 51000 10.0.0.1 22")  # ssh -X: DISPLAY is set as well
    n = _numbers(capsys)["Rock & Roll"]
    rc, _, err = _run(capsys, "feed", "show", str(n), "--open")
    assert rc == 0
    assert browser.calls == []
    assert "no browser could be opened" in err


def test_open_over_ssh_honours_an_explicit_browser(server, browser, monkeypatch, capsys) -> None:
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.2 51000 10.0.0.1 22")
    monkeypatch.delenv("DISPLAY")
    monkeypatch.setenv("BROWSER", "/remote-cli/helpers/browser.sh")
    n = _numbers(capsys)["Rock & Roll"]
    rc, _, err = _run(capsys, "feed", "show", str(n), "--open")
    assert rc == 0
    assert len(browser.calls) == 1
    assert "opened " in err


def test_open_treats_a_webbrowser_error_as_no_browser(server, browser, monkeypatch, capsys) -> None:
    def boom(url: str) -> bool:
        raise feed.webbrowser.Error("no runnable browser")

    monkeypatch.setattr(feed.webbrowser, "open", boom)
    n = _numbers(capsys)["Rock & Roll"]
    rc, _, err = _run(capsys, "feed", "show", str(n), "--open")
    assert rc == 0
    assert "no browser could be opened" in err


def test_open_on_an_item_without_a_url_exits_2(server, browser, capsys) -> None:
    n = _numbers(capsys)["Ep 1"]
    rc, out, err = _run(capsys, "feed", "show", str(n), "--open")
    assert rc == 2
    assert out == ""
    assert "no web link" in err
    assert browser.calls == []


def test_open_refuses_a_non_http_url(server, browser, capsys) -> None:
    server.routes["/api/v1/apps/following/data"] = httpx.Response(
        200, json=_doc_with_url("file:///etc/passwd")
    )
    n = _numbers(capsys)["Rock & Roll"]
    rc, _, err = _run(capsys, "feed", "show", str(n), "--open")
    assert rc == 2
    assert browser.calls == []
    assert "no web link" in err


def _doc_with_url(url: str) -> dict:
    doc = _doc()
    doc["data"]["feed"][1]["url"] = url
    return doc


def _link_targets(out: str) -> list[str]:
    """The URLs of the OSC 8 hyperlinks opened in ``out`` (rich writes ``8;id=N;URL``)."""
    return re.findall(r"\x1b\]8;id=\d+;([^\x1b]+)\x1b\\", out)


def _terminal(monkeypatch, *, tty: bool):
    import io

    from rich.console import Console

    from pepper_cli import following_render

    buf = io.StringIO()
    console = Console(
        file=buf,
        force_terminal=tty,
        width=120,
        color_system="standard" if tty else None,
        legacy_windows=False,  # a legacy Windows console never gets OSC 8 links
    )
    monkeypatch.setattr(following_render, "_console", console)
    return buf


def test_titles_are_hyperlinks_on_a_terminal_only(monkeypatch) -> None:
    from pepper_cli import feed_detail_render, following_render

    entry = _entry("Linked")
    buf = _terminal(monkeypatch, tty=True)
    following_render.render_feed([entry])
    feed_detail_render.render_detail({"n": 1, **entry})
    out = buf.getvalue()
    assert _link_targets(out) == ["https://news.example/Linked"] * 2  # list row and detail
    plain = re.sub(r"\x1b\[[0-9;]*m|\x1b\]8;[^\x1b]*\x1b\\", "", out)
    assert plain.count("https://news.example/Linked\n") == 2  # the plain url lines stay

    buf = _terminal(monkeypatch, tty=False)
    following_render.render_feed([entry])
    feed_detail_render.render_detail({"n": 1, **entry})
    assert "\x1b" not in buf.getvalue()


def test_a_non_http_url_gets_no_link(monkeypatch) -> None:
    from pepper_cli import following_render

    buf = _terminal(monkeypatch, tty=True)
    following_render.render_feed(
        [_entry("A", url="javascript:alert(1)"), _entry("B", url="file:///etc/passwd")]
    )
    assert _link_targets(buf.getvalue()) == []
    assert "\x1b]" not in buf.getvalue()


def test_a_url_cannot_break_out_of_the_link_markup(monkeypatch) -> None:
    from pepper_cli import following_render

    tricky = "https://x.example/a]b[bold]c\\e'\"[/link]"
    assert following_render.web_link(tricky) == tricky  # passed through, not encoded
    buf = _terminal(monkeypatch, tty=True)
    following_render.render_feed([_entry("T", url=tricky)])
    out = buf.getvalue()
    assert _link_targets(out) == [tricky]  # one link, the server's url exactly
    assert out.count("\x1b]") == 2  # its open and its close


@pytest.mark.parametrize(
    "url",
    [
        "https://x.example/a\x1b]8;;https://evil.example\x07",
        "https://x.example/a\x07b",
        "https://x.example/a b",
        "https://x.example/\u202eexe.txt",
        "https://x.example/a\x9cb",
        "https:///no-host",
        "",
    ],
)
def test_a_url_with_control_characters_or_no_host_is_no_link(url) -> None:
    from pepper_cli import following_render

    assert following_render.web_link(url) is None


_IPV6 = "https://[2606:4700:4700::1111]/a?b=1"


def test_an_ipv6_title_link_keeps_its_brackets(monkeypatch) -> None:
    from pepper_cli import following_render

    assert following_render.web_link(_IPV6) == _IPV6
    buf = _terminal(monkeypatch, tty=True)
    following_render.render_feed([_entry("V6", url=_IPV6)])
    assert _link_targets(buf.getvalue()) == [_IPV6]


def test_open_passes_an_ipv6_link_unchanged(server, browser, capsys) -> None:
    url = _IPV6
    server.routes["/api/v1/apps/following/data"] = httpx.Response(200, json=_doc_with_url(url))
    n = _numbers(capsys)["Rock & Roll"]
    rc, _, _ = _run(capsys, "feed", "show", str(n), "--open")
    assert rc == 0
    assert browser.calls == [url]


def test_a_link_is_not_html_decoded(server, browser, capsys) -> None:
    url = "https://x.example/p?x=1&copy=2&amp;y=3"
    server.routes["/api/v1/apps/following/data"] = httpx.Response(200, json=_doc_with_url(url))
    n = _numbers(capsys)["Rock & Roll"]
    rc, _, err = _run(capsys, "feed", "show", str(n), "--open")
    assert rc == 0
    assert browser.calls == [url]
    assert f"opened {url}" in err


def test_feed_json_has_no_hyperlink_escapes(server, capsys) -> None:
    rc, out, _ = _run(capsys, "feed", "list", "--json")
    assert rc == 0
    assert "\x1b" not in out


def test_a_malformed_url_is_no_link_rather_than_a_crash(monkeypatch) -> None:
    from pepper_cli import feed_detail_render, following_render

    bad = "https://[broken/a"
    assert following_render.web_link(bad) is None
    buf = _terminal(monkeypatch, tty=True)
    following_render.render_feed([_entry("Broken", url=bad)])
    feed_detail_render.render_detail({"n": 1, **_entry("Broken", url=bad)})
    out = buf.getvalue()
    assert out.count("Broken") == 2
    assert _link_targets(out) == []


def test_open_on_a_malformed_url_exits_2_with_a_json_error(server, browser, capsys) -> None:
    server.routes["/api/v1/apps/following/data"] = httpx.Response(
        200, json=_doc_with_url("https://[broken/a")
    )
    n = _numbers(capsys)["Rock & Roll"]
    rc, out, err = _run(capsys, "feed", "show", str(n), "--open", "--json")
    assert rc == 2
    assert browser.calls == []
    assert out == ""
    error = json.loads(err.strip().splitlines()[-1])
    assert "not_found" in json.dumps(error) and "no web link" in json.dumps(error)
